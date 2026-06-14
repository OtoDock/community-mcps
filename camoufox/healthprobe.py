#!/usr/bin/env python3
"""camoufox health probe — does a REAL browser navigate via the MCP server on
:8931. Exit 0 = healthy, 1 = wedged/unreachable.

A plain TCP/HTTP check is not enough: the failure mode we've seen is "the HTTP
server is up and answers, but the long-lived Firefox has wedged and page.goto
hangs forever." So we drive an actual `browser_navigate` to about:blank (no
network/DNS/anti-detect work needed) and require it to return within a bound.

Stdlib-only (urllib) so it needs nothing added to the image. Used by both the
docker-compose healthcheck (observability) and the entrypoint watchdog (recycle).
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8931/mcp/"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
TIMEOUT = 25  # seconds per request; a healthy about:blank navigate returns in <1s


def _post(body, headers):
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(), headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    return resp.status, resp.headers.get("mcp-session-id"), resp.read().decode("utf-8", "replace")


def main():
    try:
        status, sid, _ = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "healthprobe", "version": "1"}},
        }, HEADERS)
        if status != 200 or not sid:
            print(f"init failed (status={status}, sid={sid})")
            return 1
        h2 = dict(HEADERS)
        h2["mcp-session-id"] = sid
        try:
            _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, h2)
        except Exception:
            pass  # notification ack is best-effort

        status, _, raw = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "browser_navigate", "arguments": {"url": "about:blank"}},
        }, h2)
        if status != 200:
            print(f"navigate bad status={status}")
            return 1
        if "TimeoutError" in raw or '"isError":true' in raw.replace(" ", ""):
            print("navigate returned an error")
            return 1
        return 0
    except Exception as e:  # timeout / connection reset / HTTP 404 (session lost) = wedged
        print(f"probe failed: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
