"""GitHub MCP sidecar — streamable-HTTP wrap of github-mcp-server stdio.

`github/github-mcp-server` (Go) only exposes stdio transport, and reads its
auth token from the env var ``GITHUB_PERSONAL_ACCESS_TOKEN``. OtoDock's
framework injects per-user auth via the inbound HTTP request's
``Authorization: Bearer …`` header. supergateway and mcp-proxy can both
wrap stdio in HTTP but they only set env at startup, so neither can do
per-session bearer→env. This sidecar does it.

Architecture
============

- One ``github-mcp-server stdio`` subprocess per MCP session
  (identified by the streamable-HTTP ``Mcp-Session-Id`` header).
- The bearer is captured the FIRST time we see a session_id; it becomes the
  subprocess's ``GITHUB_PERSONAL_ACCESS_TOKEN`` env. Subsequent requests in
  the same session reuse the subprocess.
- If a request arrives with the same session id but a different bearer
  (token refresh on the platform side), the old subprocess is killed and a
  fresh one is spawned with the new env. This is rare in practice — the
  platform's per-chat-session bearer is stable for the chat's lifetime.
- An idle reaper kills subprocesses that haven't seen a request in
  ``GITHUB_MCP_IDLE_TTL`` seconds (default 600s).

The sidecar enforces ``Authorization: Bearer …`` presence; missing/empty
bearer returns 401 before any subprocess work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response

logger = logging.getLogger("github-mcp-sidecar")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

GITHUB_MCP_BINARY = os.environ.get(
    "GITHUB_MCP_BINARY", "/usr/local/bin/github-mcp-server"
)
IDLE_TTL_SECONDS = int(os.environ.get("GITHUB_MCP_IDLE_TTL", "600"))
SESSION_HEADER = "mcp-session-id"


_MCP_PROTOCOL_VERSION = "2024-11-05"

# Sentinel for _read(expect_id=...): the default returns the next JSON frame
# without id-correlation (used where no specific response is being awaited).
_NO_EXPECT_ID = object()


class StdioSession:
    """One ``github-mcp-server stdio`` subprocess for one MCP session."""

    def __init__(self, session_id: str, bearer: str) -> None:
        self.session_id = session_id
        self.bearer = bearer
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.initialized = False
        self.last_used = time.monotonic()
        self._lock = asyncio.Lock()
        self._stderr_drain: Optional[asyncio.Task] = None

    async def start(self) -> None:
        env = {
            # Minimal env — strip everything except PATH so the child
            # doesn't inherit our uvicorn config or any platform secrets.
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "GITHUB_PERSONAL_ACCESS_TOKEN": self.bearer,
        }
        gh_host = os.environ.get("GITHUB_HOST", "")
        if gh_host:
            env["GITHUB_HOST"] = gh_host
        self.proc = await asyncio.create_subprocess_exec(
            GITHUB_MCP_BINARY, "stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # github-mcp-server's `tools/list` response with 100+ tools is
            # well over the asyncio default 64KB readline limit. Bump to
            # 10 MB; a single MCP frame should never approach that.
            limit=10 * 1024 * 1024,
        )
        logger.info("session %s: spawned pid=%d", self.session_id, self.proc.pid)
        # Drain stderr in the background — github-mcp-server logs heavily to
        # stderr; if we leave the pipe unread, its buffer fills up and the
        # child blocks on its next write.
        self._stderr_drain = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                logger.debug(
                    "session %s stderr: %s",
                    self.session_id, line.decode(errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stderr drain failed")

    async def _write(self, payload: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        line = json.dumps(payload).encode() + b"\n"
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def _read(self, expect_id=_NO_EXPECT_ID) -> dict:
        """Read one JSON-RPC frame from stdout, skipping any non-JSON noise.

        Some MCP servers (including github-mcp-server) print log lines
        to stdout interleaved with the JSON-RPC stream. The MCP spec
        forbids it, but real implementations do it anyway. We tolerate
        it by scanning until we find a line that parses as JSON.

        When ``expect_id`` is given, keep scanning until a frame whose
        ``id`` matches it — discarding JSON-RPC notifications (no ``id``)
        and any out-of-order frames in between — so a stray server-emitted
        notification can never be mis-returned as a request's response.
        """
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            response_line = await self.proc.stdout.readline()
            if not response_line:
                stderr_chunk = b""
                if self.proc.stderr is not None:
                    try:
                        stderr_chunk = await asyncio.wait_for(
                            self.proc.stderr.read(1024), timeout=0.5
                        )
                    except asyncio.TimeoutError:
                        pass
                raise RuntimeError(
                    f"stdio EOF; stderr: {stderr_chunk.decode(errors='replace')[:500]}"
                )
            stripped = response_line.strip()
            if not stripped:
                continue  # blank line, skip
            try:
                frame = json.loads(stripped)
            except json.JSONDecodeError:
                # Non-JSON log noise on stdout — log + keep scanning.
                logger.debug(
                    "session %s: skipped non-JSON stdout line: %r",
                    self.session_id, stripped[:200],
                )
                continue
            if expect_id is _NO_EXPECT_ID or frame.get("id") == expect_id:
                return frame
            # A notification (no id) or an out-of-order frame arrived before
            # the response we're waiting for — discard it and keep scanning.
            logger.debug(
                "session %s: skipped stdout frame id=%r while awaiting id=%r",
                self.session_id, frame.get("id"), expect_id,
            )

    async def _internal_handshake(self) -> None:
        """Send initialize + notifications/initialized internally.

        Stateless clients (Claude CLI today) don't echo the
        ``Mcp-Session-Id`` we mint, so every inbound request lands on a
        fresh subprocess. Without this handshake the underlying
        github-mcp-server stays uninitialized and silently returns no
        tools. We do the handshake the first time we use a subprocess so
        the client's actual request lands on a ready server.
        """
        await self._write({
            "jsonrpc": "2.0",
            "id": "_sidecar_init",
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "otodock-github-sidecar", "version": "1.0"},
            },
        })
        await self._read(expect_id="_sidecar_init")  # discard initialize response
        # `notifications/*` are JSON-RPC notifications — no id, no response.
        await self._write({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        self.initialized = True

    async def request(self, payload: dict) -> dict:
        """Send one JSON-RPC frame, read one response frame.

        Auto-handshakes on first use unless the incoming message is itself
        an ``initialize`` (in which case the client is driving the
        handshake and we just pass it through). Held under a per-session
        lock so concurrent inbound requests on the same session id can't
        interleave on stdin.
        """
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError("stdio subprocess not alive")
        async with self._lock:
            self.last_used = time.monotonic()
            method = (payload.get("method") or "")
            if not self.initialized:
                if method == "initialize":
                    # Client is driving the handshake — pass through, then
                    # send the `initialized` notification on its behalf
                    # (stateless clients sometimes omit it).
                    await self._write(payload)
                    response = await self._read(expect_id=payload.get("id"))
                    await self._write({
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    })
                    self.initialized = True
                    return response
                # Non-initialize first message → handshake silently then route.
                await self._internal_handshake()
            await self._write(payload)
            # JSON-RPC notifications have no `id` — no response is sent.
            if "id" not in payload:
                return {}
            return await self._read(expect_id=payload.get("id"))

    async def close(self) -> None:
        if self._stderr_drain is not None and not self._stderr_drain.done():
            self._stderr_drain.cancel()
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        logger.info("session %s: closed pid=%d", self.session_id, self.proc.pid)


SESSIONS: dict[str, StdioSession] = {}
SESSIONS_LOCK = asyncio.Lock()


async def _reaper() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            now = time.monotonic()
            async with SESSIONS_LOCK:
                stale = [
                    sid for sid, s in SESSIONS.items()
                    if now - s.last_used > IDLE_TTL_SECONDS
                ]
                for sid in stale:
                    sess = SESSIONS.pop(sid)
                    await sess.close()
                    logger.info("reaper: evicted idle session %s", sid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper_task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except asyncio.CancelledError:
            pass
        async with SESSIONS_LOCK:
            for sess in list(SESSIONS.values()):
                await sess.close()
            SESSIONS.clear()


app = FastAPI(lifespan=lifespan, title="github-mcp-sidecar")


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer header")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Empty bearer token")
    return token


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    bearer = _extract_bearer(request)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body")

    session_id = request.headers.get(SESSION_HEADER, "") or str(uuid.uuid4())

    async with SESSIONS_LOCK:
        sess = SESSIONS.get(session_id)
        if sess is None or sess.bearer != bearer:
            if sess is not None:
                # Same session id, different bearer → token refresh upstream.
                # Tear down and restart.
                await sess.close()
                logger.info(
                    "session %s: bearer rotated, restarting subprocess", session_id,
                )
            sess = StdioSession(session_id, bearer)
            try:
                await sess.start()
            except FileNotFoundError as e:
                raise HTTPException(
                    500, f"github-mcp-server binary not found: {e}"
                )
            SESSIONS[session_id] = sess

    try:
        response_payload = await sess.request(payload)
    except RuntimeError as e:
        # Subprocess died mid-request — purge so the next call can recover.
        async with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
        raise HTTPException(502, f"upstream stdio error: {e}")

    # JSON-RPC notifications (no ``id`` — e.g. ``notifications/initialized``)
    # expect NO response body. The streamable-HTTP transport mandates 202
    # Accepted with an empty body; returning 200 with ``{}`` breaks strict
    # clients — Codex's Rust ``rmcp`` transport tries to parse the ``{}`` as a
    # JsonRpcMessage, fails ("data did not match any variant of untagged enum
    # JsonRpcMessage, when send initialized notification"), and tears the whole
    # github-mcp session down. (Claude's SDK tolerated the 200/{}; Codex does not.)
    if isinstance(payload, dict) and "id" not in payload:
        return Response(status_code=202, headers={SESSION_HEADER: session_id})

    return Response(
        content=json.dumps(response_payload),
        media_type="application/json",
        headers={SESSION_HEADER: session_id},
    )


@app.delete("/mcp")
async def mcp_close(request: Request) -> Response:
    """MCP session close — tear down the stdio subprocess."""
    session_id = request.headers.get(SESSION_HEADER, "")
    if not session_id:
        return Response(status_code=204)
    async with SESSIONS_LOCK:
        sess = SESSIONS.pop(session_id, None)
    if sess is not None:
        await sess.close()
    return Response(status_code=204)


@app.get("/health")
async def health() -> dict:
    """Lightweight health probe used by Docker + the platform's MCP polling."""
    return {"status": "ok", "sessions": len(SESSIONS)}
