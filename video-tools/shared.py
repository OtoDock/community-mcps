"""Shared utilities for the video-tools MCP server.

Config, per-request session binding, path mapping, proxy-hook helpers.
Mirrors the platform's file-tools MCP: this is a SHARED container serving
every session of the install, so all session state is request-scoped.
"""

import base64
import contextvars
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
# `/agents` is the canonical mount point inside the container (declared in
# docker-compose.yml). Hardcoded — not a config knob.
MOUNT_AGENTS_DIR = "/agents"
# A remote machine's file the proxy pulled for one session, and the slugs an
# answer may name.
HOST_CACHE_SEGMENT = ".remote-host-cache"
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
MCP_PORT = int(os.environ.get("MCP_PORT", "8933"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("video-tools")

# ---------------------------------------------------------------------------
# Per-request session binding (session_id + auth) via contextvars
# ---------------------------------------------------------------------------
#
# The proxy injects, per session, a `?session_id=` URL param AND an
# `Authorization: Bearer <session-JWT>` header. Both are bound per request at
# the transport boundary; the streamable-HTTP transport runs stateless, so the
# values propagate into the tool handler's task group and never bleed across
# concurrent sessions. Empty defaults fail CLOSED ("not session-bound"), never
# another session's identity.

_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_session_id", default=""
)
_auth_header_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_auth", default=""
)
# The session's agent, as the proxy named it in this request's first answer:
# a write in the form the results print is mapped to its sandbox form.
_agent_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_agent", default=""
)


def set_request_context(session_id: str, auth_header: str) -> None:
    """Bind the in-flight request's session_id + Authorization header.

    Called at the transport boundary (server.py handle_sse / mcp_asgi_app)
    BEFORE the MCP SDK dispatches the tool handler.
    """
    _session_id_var.set(session_id or "")
    _auth_header_var.set(auth_header or "")


def _current_session() -> tuple[str, str]:
    """``(session_id, authorization_header)`` for the in-flight request."""
    return _session_id_var.get(), _auth_header_var.get()


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------
#
# Inbound (LLM → video-tools): paths arrive as sandbox-virtual or
# agents-relative strings. We always ask the proxy's /v1/hooks/resolve-path to
# translate them to a canonical agents-relative path, then prepend `/agents/`
# for the mounted container view.
#
# Outbound (video-tools → proxy hooks): we post agents-relative paths.


def _to_agents_relative(container_path: str) -> str:
    """Strip the `/agents/` mount prefix to produce an agents-relative path
    suitable for posting to the proxy hooks."""
    if container_path.startswith(MOUNT_AGENTS_DIR + "/"):
        return container_path[len(MOUNT_AGENTS_DIR) + 1:]
    if container_path == MOUNT_AGENTS_DIR:
        return ""
    return container_path  # already agents-relative or out-of-tree


def _resolve_via_proxy(path: str, writing: bool = False) -> tuple[str | None, str, str]:
    """Ask the proxy to translate a path to an agents-relative path.

    Returns ``(agents_relative, agent, "")`` on success, ``agent`` being the
    slug the proxy names for the session. On failure returns ``(None, "",
    reason)`` where ``reason`` carries the proxy's real verdict (403 policy
    reject / 404 not reachable) so the caller can surface it. ``writing``
    marks a write target: the proxy judges it by the session's write rules.
    """
    session_id, auth = _current_session()
    if not session_id or not PROXY_URL or not auth:
        return None, "", "video-tools is not session-bound (missing session_id/PROXY_URL/auth)"
    try:
        resp = httpx.post(
            f"{PROXY_URL}/v1/hooks/resolve-path",
            json={"session_id": session_id, "path": path, "writing": writing},
            headers={"Authorization": auth},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            agents_rel = data.get("agents_relative", "")
            if agents_rel:
                return agents_rel, str(data.get("agent") or ""), ""
            return None, "", (
                "proxy resolved the path but returned no agents-relative "
                "mapping (it is outside the synced agent tree)"
            )
        detail = ""
        try:
            detail = str(resp.json().get("detail", "")).strip()
        except Exception:
            detail = (resp.text or "")[:200].strip()
        return None, "", f"proxy resolve-path {resp.status_code}: {detail or '(no detail)'}"
    except Exception as e:
        logger.debug(f"resolve-path failed for '{path}': {e}")
        return None, "", f"resolve-path request failed: {e}"


def _session_subtree(slug: str) -> str | None:
    """The one container tree this request may touch: the session's own
    agent folder under the mount, or, for a remote machine's file, the
    session's own host-cache folder. None for anything else."""
    if slug == HOST_CACHE_SEGMENT:
        session_id = _session_id_var.get()
        return f"{MOUNT_AGENTS_DIR}/{HOST_CACHE_SEGMENT}/{session_id}" if session_id else None
    if _SAFE_SLUG_RE.match(slug or ""):
        return f"{MOUNT_AGENTS_DIR}/{slug}"
    return None


def _write_form(path: str) -> str:
    """A write named in the form the tool's own results print
    (``<agent>/users/...``), which the proxy admits for a read only: its
    sandbox form. The agent comes from an earlier answer in this request,
    else from a read answer for ``/workspace`` (asked only when the first
    part could be an agent's name)."""
    if path.startswith("/") or "/" not in path:
        return path
    head, rest = path.split("/", 1)
    agent = _agent_var.get()
    if not agent and _SAFE_SLUG_RE.match(head):
        agent = _resolve_via_proxy("/workspace")[1]
        if agent and agent != HOST_CACHE_SEGMENT:
            _agent_var.set(agent)
    return "/" + rest if agent and head == agent else path


def _unicode_match_on_disk(path: str) -> str:
    """If ``path`` doesn't exist verbatim but a Unicode-normalized variant of
    its basename exists in the parent dir, return the matching on-disk path.

    LLM/JSON transports normalize to NFC while files written by Drive/macOS/
    Slack may be NFD — without this, non-ASCII filenames round-tripped through
    tool results become unreadable.
    """
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return path
    basename = os.path.basename(path)
    if not basename:
        return path
    target_nfc = unicodedata.normalize("NFC", basename)
    try:
        for entry in os.listdir(parent):
            if unicodedata.normalize("NFC", entry) == target_nfc:
                return os.path.join(parent, entry)
    except OSError:
        pass
    return path


def _resolve_path(path: str, writing: bool = False) -> str:
    """Resolve a tool-input path to a container-local path, judged by the
    proxy for the calling session.

    Accepts sandbox-virtual (`/users/alice/workspace/foo`), agents-relative
    (`agent/users/alice/...`), or container-absolute (`/agents/...`) strings.
    Every form goes to the proxy: the mount holds every agent's tree, so a
    prefix says nothing about this session's rights on a file. ``writing``
    marks every path the tool creates or changes. The answer is confined to
    the session's own tree (the agent the proxy names, or the session's own
    host-cache folder), as file-tools confines it.
    """
    if path == MOUNT_AGENTS_DIR:
        path = ""
    elif path.startswith(MOUNT_AGENTS_DIR + "/"):
        path = path[len(MOUNT_AGENTS_DIR) + 1:]
    if writing:
        path = _write_form(path)

    agents_rel, agent, reason = _resolve_via_proxy(path, writing=writing)
    if agents_rel and not agent:
        reason = "the proxy answered without the session's agent"
    elif agents_rel:
        allowed = _session_subtree(agent)
        # Match the name first, then resolve it and confine it. The platform
        # judged its answer with every link already followed, so a matched
        # name that resolves elsewhere (a sibling that differs only in its
        # normal form, or a link put in after the judgement) names a place
        # nobody judged: refused, even inside the same agent.
        matched = _unicode_match_on_disk(MOUNT_AGENTS_DIR + "/" + agents_rel.lstrip("/"))
        resolved = str(Path(matched).resolve())
        if resolved != os.path.normpath(matched):
            reason = "the path names a link the platform did not judge"
        # The separator matters: a bare prefix test admits a sibling such as
        # /agents/proj-a-evil/x.
        elif allowed and (resolved == allowed or resolved.startswith(allowed + "/")):
            if agent != HOST_CACHE_SEGMENT:
                _agent_var.set(agent)
            return resolved
        else:
            reason = "the path leaves this session's agent tree"

    raise ValueError(
        f"Cannot open '{path}': {reason}"
        if reason else
        f"Path could not be resolved: {path}"
    )


def _on_a_computer(container_path: str) -> bool:
    """A remote machine's file the proxy pulled for this session: its
    folder is a cache, never a place the session writes."""
    return container_path.startswith(f"{MOUNT_AGENTS_DIR}/{HOST_CACHE_SEGMENT}/")


def _write_beside(container_path: str) -> str | None:
    """A file the tool writes on its own next to a source (an analysis
    sidecar, a stabilization cache, a default output): judged as a write for
    the session, or None when the session may not write there or the source
    sits on a computer (the tool then keeps the result in its answer or its
    own scratch folder)."""
    if _on_a_computer(container_path):
        return None
    try:
        return _resolve_path(container_path, writing=True)
    except ValueError as exc:
        logger.info("not written beside the source: %s", exc)
        return None


def _read_beside(container_path: str) -> str | None:
    """A file the tool reads on its own next to a source (a stabilization
    cache): judged as a read for the session like any file it names, or None
    when the session may not read it or the source sits on a computer (nothing
    is written beside it there)."""
    if _on_a_computer(container_path):
        return None
    try:
        return _resolve_path(container_path)
    except ValueError as exc:
        logger.info("not read beside the source: %s", exc)
        return None


def _op_type(op: dict) -> str:
    """Extract operation type — LLMs may use 'type', 'op', 'operation', or 'action'."""
    return op.get("type") or op.get("op") or op.get("operation") or op.get("action") or ""


def _normalize_operations(ops) -> list[dict]:
    """Normalize an operations argument to a list of dicts.

    Some LLMs double-encode array parameters as JSON strings. Accept any of:
    list of dicts, list of JSON strings, JSON string of a list, JSON string of
    a single op, or a single dict. Malformed items are dropped so the rest
    still run.
    """
    if ops is None:
        return []
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        return []
    normalized: list[dict] = []
    for op in ops:
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except (json.JSONDecodeError, ValueError):
                continue
        if isinstance(op, dict):
            normalized.append(op)
    return normalized


# ---------------------------------------------------------------------------
# Proxy-hook helpers
# ---------------------------------------------------------------------------


async def _notify_file_written(file_path: str) -> bool:
    """Tell the proxy that we just finished writing a file.

    For remote agent sessions the proxy pushes the file back to the satellite
    so the agent CLI and downstream MCPs see the updated content. No-op for
    local sessions. Fire-and-forget — tool success is independent of sync.
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return False
    agents_rel = _to_agents_relative(file_path)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/file-written",
                json={"session_id": session_id, "path": agents_rel},
                headers={"Authorization": auth},
            )
            if resp.status_code == 200:
                return bool(resp.json().get("ok"))
    except Exception as exc:
        logger.warning(f"file-written notify failed (non-fatal): {exc}")
    return False


async def _push_image_preview(image_bytes: bytes, mime: str, caption: str = ""):
    """Push an inline image preview to the dashboard (user-visible)."""
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    b64 = base64.b64encode(image_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/images",
                json={
                    "session_id": session_id,
                    "images": [{
                        "image_data": b64,
                        "mime_type": mime,
                        "caption": caption,
                    }],
                },
                headers={"Authorization": auth},
            )
    except Exception as exc:
        logger.warning(f"Image preview push failed (non-fatal): {exc}")
