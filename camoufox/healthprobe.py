#!/usr/bin/env python3
"""camoufox health probe — does a REAL browser navigate via the MCP server on
:8931, reusing ONE long-lived MCP session. Exit 0 = healthy, 1 = wedged/
unreachable.

A plain TCP/HTTP check is not enough: the failure mode we've seen is "the HTTP
server is up and answers, but the long-lived Firefox has wedged and page.goto
hangs forever." So we drive an actual `browser_navigate` to the server's OWN
http port (127.0.0.1:8930 — full TCP/HTTP/renderer path, still no DNS or
external dependency) and require it to return within a bound. about:blank was
not enough: a Firefox wedged for REAL page loads (observed 2026-07-07 after a
day of context churn — every agent navigation timed out) kept passing
about:blank probes, so the watchdog never recycled it.

Why ONE session (and how, this time for real — measured + read from source,
2026-07-06): the server runs --isolated, so every `initialize` creates a fresh
browser context, and camoufox's Firefox permanently leaks ~1-2 MB per context
create→destroy cycle (4.3 GB RSS after 36 h of probe-only traffic observed).
Sessions do NOT age out — playwright-mcp's sdk/server.js starts a HEARTBEAT
per streamable session: a server→client `ping` every 3 s that must be answered
within 5 s, else `server.close()`. Delivering that ping requires an open
server→client channel (the standalone SSE GET), so a fire-and-forget client's
sessions die ~5-8 s after initialize NO MATTER WHAT it does with POSTs (a
2.5 s navigate keep-alive still lost its session every ~5.6 s). The fix is the
``--keepalive`` mode below: a minimal PROPER client that holds the GET stream
open and answers pings, keeping the ONE cached session alive indefinitely
(validated: 100 s idle, 30 pings answered, session alive). The one-shot
verdict callers (compose healthcheck, entrypoint watchdog) then reuse the
cached session id — navigating an existing context creates and leaks nothing.

The sid cache lives in /run/probe (container-lifetime — a recycle starts
clean), NOT /tmp: the callers run as different users (healthcheck = root,
watchdog + keepalive = camoufox) and /tmp's sticky bit lets whoever creates
the file first lock the other out of replacing it (a root-created cache once
turned every camoufox-user run into a fresh leaked context). The entrypoint
pre-creates /run/probe owned by camoufox (non-sticky); writes are atomic
os.replace and WARN on failure — a silent cache-write failure is exactly how
the /tmp variant regressed. Timeouts/connection failures never re-init: the
server itself is unreachable or hung, a fresh session would hang the same way,
and the healthcheck's time budget is too small for both.

Stdlib-only (urllib) so it needs nothing added to the image.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8931/mcp/"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
TIMEOUT = 25  # seconds per request; a healthy local-URL navigate returns in <1s
SID_CACHE = "/run/probe/healthprobe.mcp-session-id"


def _post(body, headers, timeout=TIMEOUT):
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(), headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.status, resp.headers.get("mcp-session-id"), resp.read().decode("utf-8", "replace")


def _navigate(sid):
    """local-URL navigate on session ``sid`` → (ok, why). HTTPError raises
    through (caller decides: cached session → re-init; fresh session → fail)."""
    h = dict(HEADERS)
    h["mcp-session-id"] = sid
    status, _, raw = _post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "browser_navigate", "arguments": {"url": "http://127.0.0.1:8930/"}},
    }, h)
    if status != 200:
        return False, f"navigate bad status={status}"
    if "TimeoutError" in raw or '"isError":true' in raw.replace(" ", ""):
        return False, "navigate returned an error"
    return True, ""


def _read_cached_sid():
    try:
        with open(SID_CACHE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_cached_sid(sid):
    # Atomic replace: the temp file is ours (per-pid name), and /run/probe is
    # non-sticky so either caller (root healthcheck / camoufox keepalive) can
    # replace a file the other created.
    tmp = f"{SID_CACHE}.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(sid)
        os.replace(tmp, SID_CACHE)
    except OSError as e:
        # Best-effort (the verdict never depends on the cache) but NEVER silent:
        # an unwritable cache means every future run re-inits a fresh browser
        # context — the leak this probe exists to prevent.
        print(f"warning: sid cache write failed: {e}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _init_session():
    """initialize + initialized-ack → (sid|None, status). Caches the sid on
    success. Shared by the one-shot verdict and the keepalive loop. Sends NO
    ``roots`` capability, so the server never issues ``roots/list`` — pings
    are the only server→client traffic the keepalive must answer."""
    status, sid, _ = _post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "healthprobe", "version": "1"}},
    }, HEADERS)
    if status != 200 or not sid:
        return None, status
    h2 = dict(HEADERS)
    h2["mcp-session-id"] = sid
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, h2)
    except Exception:
        pass  # notification ack is best-effort
    _write_cached_sid(sid)
    return sid, status


def main():
    try:
        sid = _read_cached_sid()
        if sid:
            try:
                ok, _why = _navigate(sid)
                if ok:
                    return 0  # steady state: rode the keepalive-held session
                # Session answered but can't navigate (dead context, tool
                # error) — not a wedge verdict yet; retry on a fresh session.
            except urllib.error.HTTPError:
                pass  # session id rejected (keepalive down / server restart) → re-init
            # URLError/timeout/connection-reset fall to the outer handler: the
            # server is unreachable or hung, so a re-init would hang the same way.

        sid, status = _init_session()
        if not sid:
            print(f"init failed (status={status})")
            return 1

        ok, why = _navigate(sid)
        if not ok:
            print(why)
            return 1
        return 0
    except Exception as e:  # timeout / connection reset / HTTP error = wedged
        print(f"probe failed: {type(e).__name__}: {e}")
        return 1


def keepalive():
    """Minimal proper MCP client half: hold the ONE cached session's standalone
    SSE GET open and answer the server's heartbeat pings (3 s cadence, 5 s
    deadline — see module docstring), so the session — and its single browser
    context — lives for the container's lifetime. NOT a health verdict: errors
    here mean "re-establish and carry on"; the watchdog and compose healthcheck
    own the wedge decision. Re-inits are logged to stderr — in steady state
    this establishes ONE session per container lifetime, so a chatty log means
    sessions are dying under us again (the regression this design prevents)."""
    while True:
        sid = _read_cached_sid()
        if sid is None:
            try:
                sid, status = _init_session()
            except Exception as e:
                print(f"keepalive: init error: {type(e).__name__}: {e}", file=sys.stderr)
                sid = None
            if sid is None:
                time.sleep(2)
                continue
            print(f"keepalive: established session {sid[:8]}", file=sys.stderr)
        try:
            req = urllib.request.Request(
                BASE, headers={"Accept": "text/event-stream", "mcp-session-id": sid},
                method="GET")
            # Pings arrive every ~3 s, so a 30 s read timeout only trips when
            # the stream is genuinely dead — then we reopen (or re-init).
            stream = urllib.request.urlopen(req, timeout=30)
            h_answer = dict(HEADERS)
            h_answer["mcp-session-id"] = sid
            while True:
                line = stream.readline()
                if not line:
                    break  # server ended the stream — reopen with the same sid
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue  # event:/id:/keepalive-comment lines
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("method") == "ping" and "id" in msg:
                    _post({"jsonrpc": "2.0", "id": msg["id"], "result": {}},
                          h_answer, timeout=10)
        except urllib.error.HTTPError:
            # GET (or a ping answer) rejected — the session is gone; re-init.
            print(f"keepalive: session {sid[:8]} rejected — re-establishing",
                  file=sys.stderr)
            try:
                os.unlink(SID_CACHE)
            except OSError:
                pass
        except Exception as e:
            # Unreachable/hung server: keep trying — the watchdog decides wedges.
            print(f"keepalive: stream error: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--keepalive":
        keepalive()
    sys.exit(main())
