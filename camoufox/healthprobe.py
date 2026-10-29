#!/usr/bin/env python3
"""camoufox health checker: one client that behaves like an agent.

The failure this container must catch: the MCP server answers, but the
long-lived browser stops loading pages, for every new session or for all of
them. Only a real page load through the server shows it, and only a client
that keeps its session the way an agent does can load one.

``--checker`` (run by the entrypoint) is the only thing in the container that
opens sessions for health:

* A held session (``?session_id=otodock-checker-held``) loads the server's own
  port every 2 minutes. It opens the standalone GET before its first tool call
  and answers every ping on it: the server heartbeats a session from its first
  tool call (a ping every 3 s, closed when one waits 5 s) and drops a ping that
  finds no GET open.
* Every 15 minutes, and once at start, a fresh session
  (``otodock-checker-fresh``) loads the same page under a time limit and is
  closed with a DELETE. A new session is the step that failed when the browser
  wedged in the field; the held session alone does not show it. After a failed
  fresh check the next one comes 2 minutes later.
* A pass is a JSON-RPC result for the request's id that is not a tool error.
  An empty answer is a failure: a session the server closes mid-call ends its
  stream with no result, and the sidecar ends a call cut at its limit the same
  way. Each request runs under a wall-clock deadline in a worker thread; a
  socket timeout never fires, because the sidecar writes a keepalive comment
  on a quiet stream every 20 s.
* A held session the server dropped is replaced and the load retried in the
  same tick: losing a session is not a browser failure.

The result is written to ``/run/probe/status.json``; the compose healthcheck
(no arguments) and the watchdog (``--verdict``) read it and open nothing. The
entrypoint removes the file at every start, since a container restart keeps its
filesystem. The checker also asks the sidecar how many other sessions are open,
which tells the watchdog when the browser has been idle long enough for its
daily restart.

Stdlib-only so it needs nothing added to the image.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OTO_CHECKER_BASE", "http://localhost:8931")
STATUS_PATH = os.environ.get("OTO_CHECKER_STATUS", "/run/probe/status.json")
PAGE = "http://127.0.0.1:8930/"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

ID_PREFIX = "otodock-checker"
HELD_ID = f"{ID_PREFIX}-held"
FRESH_ID = f"{ID_PREFIX}-fresh"

HELD_EVERY_S = 120
FRESH_EVERY_S = 900
FRESH_RETRY_S = 120
HELD_DEADLINE_S = 30
FRESH_DEADLINE_S = 45
OPEN_DEADLINE_S = 15
DELETE_DEADLINE_S = 10
STALE_S = 300
FAILS_TO_RECYCLE = 2
MAX_AGE_S = int(os.environ.get("OTO_BROWSER_MAX_AGE_S", "86400"))
IDLE_S = int(os.environ.get("OTO_BROWSER_IDLE_S", "1800"))


# --- the pure parts (unit-tested) --------------------------------------------

def _messages(raw):
    """The JSON-RPC messages in a response body: a JSON object or array, or
    the ``data:`` lines of an SSE stream (comments and other fields skipped)."""
    text = raw.strip()
    if not text:
        return []
    if text[0] in "{[":
        try:
            body = json.loads(text)
        except ValueError:
            return []
        return body if isinstance(body, list) else [body]
    out = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict):
            out.append(msg)
    return out


def rpc_result(raw, req_id):
    """(passed, why) for the answer to request ``req_id`` in a response body."""
    for msg in _messages(raw):
        if msg.get("id") != req_id or "method" in msg:
            continue
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            return False, f"JSON-RPC error: {str(err.get('message', msg['error']))[:200]}"
        result = msg.get("result")
        if not isinstance(result, dict):
            return False, "answer without a result"
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            return False, f"tool error: {' '.join(texts).strip()[:200]}"
        return True, ""
    return False, "no answer" if raw.strip() else "empty answer"


def starting_status(now):
    return {"starting": True, "started": now, "held_failures": 0, "fresh_failures": 0,
            "last_check": None, "last_kind": None, "last_ok": None, "error": "",
            "agent_sessions": None, "idle_since": None}


def record(status, kind, passed, error, now):
    """The status after one check of ``kind`` ("held" or "fresh")."""
    new = dict(status)
    new["starting"] = False
    new["last_check"] = now
    new["last_kind"] = kind
    key = f"{kind}_failures"
    if passed:
        new[key] = 0
        new["last_ok"] = now
        new["error"] = ""
    else:
        new[key] = int(status.get(key) or 0) + 1
        new["error"] = f"{kind}: {error}"
    return new


def note_sessions(status, open_count, now):
    """Record how many other sessions the sidecar reports; ``None`` is unknown
    (an older sidecar, or no answer), and an unknown is never idle."""
    new = dict(status)
    new["agent_sessions"] = open_count
    if open_count == 0:
        new["idle_since"] = status.get("idle_since") or now
    else:
        new["idle_since"] = None
    return new


def healthy(status, now):
    """(ok, why) for the compose healthcheck."""
    if status is None:
        return False, "no status yet"
    if status.get("starting"):
        return False, "the checker has not finished its first check"
    age = now - (status.get("last_check") or 0)
    if age > STALE_S:
        return False, f"no check for {age:.0f} s"
    for kind in ("held", "fresh"):
        if int(status.get(f"{kind}_failures") or 0) >= FAILS_TO_RECYCLE:
            return False, status.get("error") or f"{kind} checks failing"
    return True, ""


def span(seconds):
    """A duration in words for the watchdog's log: minutes under two hours."""
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.0f} h"


def verdict(status, now, container_started, live_open=None):
    """(code, why) for the watchdog: 0 fine, 1 recycle, 2 the idle restart.
    ``live_open`` is the sidecar's count asked at verdict time (None: unknown)."""
    if status is None:
        if now - container_started <= STALE_S:
            return 0, "no status yet"
        return 1, f"the checker wrote no status in {now - container_started:.0f} s"
    if status.get("starting"):
        if now - (status.get("started") or container_started) <= STALE_S:
            return 0, "the checker is starting"
        return 1, "the checker has not finished a check in 5 minutes"
    age = now - (status.get("last_check") or 0)
    if age > STALE_S:
        return 1, f"the checker has not checked for {age:.0f} s"
    held = int(status.get("held_failures") or 0)
    fresh = int(status.get("fresh_failures") or 0)
    if held >= FAILS_TO_RECYCLE:
        return 1, f"the browser failed {held} checks in a row in its held session ({status.get('error')})"
    if fresh >= FAILS_TO_RECYCLE:
        return 1, f"new sessions failed {fresh} checks in a row ({status.get('error')})"
    up = now - (status.get("started") or now)
    idle_since = status.get("idle_since")
    if up >= MAX_AGE_S and idle_since and now - idle_since >= IDLE_S and live_open == 0:
        return 2, f"browser up {span(up)} with no agent session for {span(now - idle_since)}"
    return 0, "ok"


# --- the I/O parts ----------------------------------------------------------

class SessionGone(Exception):
    """The server no longer knows the session (it answered 404)."""


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers.get("mcp-session-id"), resp.read().decode("utf-8", "replace")


def _with_deadline(fn, deadline_s):
    """Run ``fn`` in a worker thread: (finished, value or exception). An
    abandoned call ends on its own once the sidecar cuts it (90 s)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001  # handed back to the caller, which judges it
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        return False, None
    return True, box.get("error", box.get("value"))


class Session:
    """One MCP session held like an agent holds it: the standalone GET open
    and every ping answered, until ``close``."""

    def __init__(self, oto):
        self.url = f"{BASE}/mcp/?session_id={oto}"
        self.sid = None
        self.headers = dict(HEADERS)
        self.stop = threading.Event()
        self.gone = threading.Event()
        self._n = 0

    def _next_id(self):
        self._n += 1
        return f"chk-{self._n}"

    def open(self):
        sid, _ = _post(self.url, {
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "otodock-checker", "version": "2"}},
        }, HEADERS, OPEN_DEADLINE_S)
        if not sid:
            raise RuntimeError("initialize returned no session id")
        self.sid = sid
        self.headers["mcp-session-id"] = sid
        # Past this point the server holds the session: any failure closes it,
        # or it would read alive with no event stream and never be closed.
        try:
            _post(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.headers, OPEN_DEADLINE_S)
            ready = threading.Event()
            threading.Thread(target=self._listen, args=(ready,), daemon=True).start()
            if not ready.wait(OPEN_DEADLINE_S) or self.gone.is_set():
                raise RuntimeError("the session's event stream did not open")
        except Exception:
            self.close()
            raise

    def _listen(self, ready):
        try:
            req = urllib.request.Request(
                self.url, headers={"Accept": "text/event-stream", "mcp-session-id": self.sid}, method="GET")
            stream = urllib.request.urlopen(req, timeout=60)
            ready.set()
            while not self.stop.is_set():
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("method") == "ping" and "id" in msg and not self.stop.is_set():
                    _post(self.url, {"jsonrpc": "2.0", "id": msg["id"], "result": {}}, self.headers, 10)
        except Exception:  # noqa: BLE001  # any end of the stream ends the session for us
            pass
        finally:
            self.gone.set()
            ready.set()

    @property
    def alive(self):
        return self.sid is not None and not self.gone.is_set() and not self.stop.is_set()

    def load_page(self, deadline_s):
        """(passed, why); raises SessionGone when the server dropped the session."""
        req_id = self._next_id()
        finished, value = _with_deadline(lambda: _post(self.url, {
            "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": "browser_navigate", "arguments": {"url": PAGE}},
        }, self.headers, deadline_s + 5), deadline_s)
        if not finished:
            return False, f"no answer within {deadline_s} s"
        if isinstance(value, urllib.error.HTTPError):
            if value.code == 404:
                raise SessionGone()
            return False, f"HTTP {value.code}"
        if isinstance(value, Exception):
            return False, f"{type(value).__name__}: {value}"[:200]
        return rpc_result(value[1], req_id)

    def close(self):
        self.stop.set()
        if not self.sid:
            return

        def delete():
            req = urllib.request.Request(self.url, headers={"mcp-session-id": self.sid}, method="DELETE")
            with urllib.request.urlopen(req, timeout=DELETE_DEADLINE_S) as resp:
                resp.read()

        _with_deadline(delete, DELETE_DEADLINE_S)


def sidecar_open_sessions():
    """Sessions open besides the checker's own, or None when unknown."""
    try:
        url = f"{BASE}/internal/sessions?except_prefix={ID_PREFIX}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return int(json.loads(resp.read()).get("open"))
    except (OSError, ValueError, TypeError):
        return None


def read_status():
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_status(status):
    tmp = f"{STATUS_PATH}.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, STATUS_PATH)
    except OSError as e:
        print(f"checker: could not write {STATUS_PATH}: {e}", file=sys.stderr)


class Checker:
    def __init__(self):
        self.held = None
        self.status = starting_status(time.time())
        write_status(self.status)

    def _open_held(self):
        if self.held is not None:
            self.held.stop.set()
        self.held = Session(HELD_ID)
        self.held.open()
        print(f"checker: holding session {self.held.sid[:8]}", file=sys.stderr)

    def held_check(self):
        try:
            if self.held is None or not self.held.alive:
                self._open_held()
            try:
                return self.held.load_page(HELD_DEADLINE_S)
            except SessionGone:
                print("checker: the held session was dropped, opening a new one", file=sys.stderr)
                self._open_held()
                return self.held.load_page(HELD_DEADLINE_S)
        except SessionGone:
            return False, "the server dropped a session it had just opened"
        except Exception as e:  # noqa: BLE001  # every failure to open or load is a failed check
            return False, f"{type(e).__name__}: {e}"[:200]

    def fresh_check(self):
        session = Session(FRESH_ID)
        try:
            session.open()
            return session.load_page(FRESH_DEADLINE_S)
        except SessionGone:
            return False, "the server dropped a session it had just opened"
        except Exception as e:  # noqa: BLE001  # every failure to open or load is a failed check
            return False, f"{type(e).__name__}: {e}"[:200]
        finally:
            session.close()

    def _apply(self, kind, passed, why):
        was = int(self.status.get(f"{kind}_failures") or 0)
        self.status = record(self.status, kind, passed, why, time.time())
        if not passed:
            print(f"checker: {kind} check failed ({was + 1} in a row): {why}", file=sys.stderr)
        elif was:
            print(f"checker: {kind} check passes again after {was} failure(s)", file=sys.stderr)

    def run(self):
        next_held = next_fresh = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_held:
                self._apply("held", *self.held_check())
                self.status = note_sessions(self.status, sidecar_open_sessions(), time.time())
                write_status(self.status)
                next_held = time.monotonic() + HELD_EVERY_S
            if time.monotonic() >= next_fresh:
                passed, why = self.fresh_check()
                self._apply("fresh", passed, why)
                write_status(self.status)
                next_fresh = time.monotonic() + (FRESH_EVERY_S if passed else FRESH_RETRY_S)
            time.sleep(max(1.0, min(next_held, next_fresh) - time.monotonic()))


def main(argv):
    if len(argv) > 1 and argv[1] == "--checker":
        Checker().run()
        return 0
    if len(argv) > 1 and argv[1] == "--verdict":
        started = float(argv[2]) if len(argv) > 2 else time.time()
        now = time.time()
        status = read_status()
        live = None
        if status and status.get("idle_since") and now - (status.get("started") or now) >= MAX_AGE_S:
            live = sidecar_open_sessions()
        code, why = verdict(status, now, started, live)
        print(why)
        return code
    ok, why = healthy(read_status(), time.time())
    if not ok:
        print(why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
