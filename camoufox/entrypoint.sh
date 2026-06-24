#!/bin/bash
set -e

# Clean stale Xvfb lock files
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start virtual display
Xvfb :99 -screen 0 2560x1440x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99

# Wait for Xvfb readiness
for i in $(seq 1 10); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.5
done

# Start camoufox browser WebSocket server in background
python3 /app/launch_server.py &

# Wait for camoufox WebSocket to be ready (up to 30s)
for i in $(seq 1 60); do
  python3 -c "import socket; s=socket.create_connection(('localhost', 3000)); s.close()" 2>/dev/null && break
  sleep 0.5
done

echo "Camoufox ready, starting MCP server..."

# Start playwright-mcp on an INTERNAL port (127.0.0.1:8930). The stream sidecar
# (below) is the public listener on 8931 and forwards to it — see
# stream_sidecar.py. --isolated gives each MCP session its own browser context.
npx @playwright/mcp@0.0.68 \
  --config /app/mcp-config.json \
  --port 8930 \
  --host 127.0.0.1 \
  --allowed-hosts '*' \
  --isolated \
  --output-dir /screenshots \
  --caps vision \
  --viewport-size "1920x1080" &
MCP_PID=$!

# Wait for playwright-mcp to accept connections on the internal port.
for i in $(seq 1 60); do
  python3 -c "import socket; s=socket.create_connection(('127.0.0.1', 8930)); s.close()" 2>/dev/null && break
  sleep 0.5
done

# Start the generic session-lifecycle sidecar (public :8931 → 127.0.0.1:8930).
# It maps the proxy-injected ?session_id=<oto> → playwright's mcp-session-id,
# idle-GCs abandoned sessions, and exposes /internal/close-session so the OtoDock
# proxy can tear a browser context down the instant it kills the agent session.
# Everything else streams straight through. The sidecar is the shared
# mcps/_shared/stream_sidecar.py (mounted at build time via BuildKit).
python3 /app/stream_sidecar.py &
SIDECAR_PID=$!

# Clean shutdown: on container stop/restart, kill both children.
trap 'kill "$MCP_PID" "$SIDECAR_PID" 2>/dev/null; exit 0' TERM INT

# Watchdog / auto-recycle. The single long-lived camoufox Firefox can wedge
# after long uptime — the HTTP server stays up and answers, but page.goto hangs
# forever (observed after ~4 days). A plain liveness check misses that, so we
# drive a REAL about:blank navigate via healthprobe.py. On a sustained wedge
# (2 consecutive failures ~ 4 min) we kill the MCP and exit non-zero so the
# compose `restart: always` policy brings up a FRESH browser. about:blank needs
# no network and a healthy navigate returns in <1s, so a 2-strike failure is a
# real wedge, not load.
fails=0
while true; do
  sleep 120
  if ! kill -0 "$MCP_PID" 2>/dev/null; then
    echo "[watchdog] MCP server exited — recycling container." >&2
    kill "$SIDECAR_PID" 2>/dev/null
    exit 1
  fi
  if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "[watchdog] stream sidecar exited — recycling container." >&2
    kill "$MCP_PID" 2>/dev/null
    exit 1
  fi
  # Probe drives a REAL about:blank navigate via the sidecar → playwright-mcp.
  if python3 /app/healthprobe.py >/dev/null 2>&1; then
    fails=0
  else
    fails=$((fails + 1))
    echo "[watchdog] navigate probe failed (${fails}/2)." >&2
    if [ "$fails" -ge 2 ]; then
      echo "[watchdog] camoufox wedged — killing MCP+sidecar to recycle." >&2
      kill "$MCP_PID" "$SIDECAR_PID" 2>/dev/null
      exit 1
    fi
  fi
done
