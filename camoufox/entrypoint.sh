#!/bin/bash
set -e

# Make the /screenshots mount writable by the non-root browser, then drop
# privileges. The mount source is created root-owned by Docker (T1 host bind /
# T2 named volume / T3 pool storage), so only a root entrypoint can chown it —
# this is the single, mount-type-agnostic write fix for every topology. The
# browser itself MUST run non-root (camoufox crashes as root on Linux), so we
# re-exec this same script as `camoufox` via gosu; the chown is the only thing
# that runs privileged. The `id -u == 0` guard makes the re-exec a no-op.
if [ "$(id -u)" = "0" ]; then
  chown camoufox:camoufox /screenshots 2>/dev/null || true
  # Shared healthprobe sid-cache dir, writable by BOTH probe callers (compose
  # healthcheck runs as root, the keepalive daemon + watchdog below as
  # camoufox). Deliberately NON-sticky — unlike /tmp — so either user can
  # atomically replace a cache file the other created; see healthprobe.py.
  mkdir -p /run/probe && chown camoufox:camoufox /run/probe
  exec gosu camoufox "$0" "$@"
fi

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

# Session keepalive: playwright-mcp heartbeats every streamable session
# (server→client ping every 3s, unanswered 5s → session closed — see
# healthprobe.py), so a fire-and-forget probe's session dies seconds after
# initialize and every probe run leaks a browser context (~1-2MB each,
# 4.3GB/36h observed). This daemon is the minimal PROPER client half: it
# holds the ONE cached probe session's SSE GET open and answers the pings,
# so the healthcheck + watchdog probes below reuse that session forever and
# create no contexts at all.
python3 /app/healthprobe.py --keepalive &
KEEPALIVE_PID=$!

# Clean shutdown: on container stop/restart, kill all children.
trap 'kill "$MCP_PID" "$SIDECAR_PID" "$KEEPALIVE_PID" 2>/dev/null; exit 0' TERM INT

# Watchdog / auto-recycle. The single long-lived camoufox Firefox can wedge
# after long uptime — the HTTP server stays up and answers, but page.goto hangs
# forever (observed after ~4 days). A plain liveness check misses that, so we
# drive a REAL about:blank navigate via healthprobe.py. On a sustained wedge
# (2 consecutive failures ~ 4 min) we kill the MCP and exit non-zero so the
# compose `restart: always` policy brings up a FRESH browser. about:blank needs
# no network and a healthy navigate returns in <1s, so a 2-strike failure is a
# real wedge, not load.
#
# The loop also recycles PROACTIVELY on high memory: every probe (ours + the
# compose healthcheck's) costs one browser context — unavoidable, see
# healthprobe.py — and camoufox's Firefox permanently leaks ~1-2MB per context
# cycle, ~2.9GB/day at these cadences. Recycling at 2.5GiB (cgroup accounting,
# same counter the compose mem_limit:3g OOM-kills on) turns an eventual
# mid-action OOM kill into a clean restart with headroom to spare.
MEM_RECYCLE_BYTES=$((2560 * 1024 * 1024))
fails=0
while true; do
  sleep 120
  if ! kill -0 "$MCP_PID" 2>/dev/null; then
    echo "[watchdog] MCP server exited — recycling container." >&2
    kill "$SIDECAR_PID" "$KEEPALIVE_PID" 2>/dev/null
    exit 1
  fi
  if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "[watchdog] stream sidecar exited — recycling container." >&2
    kill "$MCP_PID" "$KEEPALIVE_PID" 2>/dev/null
    exit 1
  fi
  if ! kill -0 "$KEEPALIVE_PID" 2>/dev/null; then
    # Without the keepalive every probe run creates + leaks a context again —
    # recycle rather than degrade silently (consistent with the other children).
    echo "[watchdog] keepalive daemon exited — recycling container." >&2
    kill "$MCP_PID" "$SIDECAR_PID" 2>/dev/null
    exit 1
  fi
  mem_now=$(cat /sys/fs/cgroup/memory.current 2>/dev/null \
    || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0)
  if [ "$mem_now" -gt "$MEM_RECYCLE_BYTES" ]; then
    echo "[watchdog] memory ${mem_now} > ${MEM_RECYCLE_BYTES} — recycling before the mem_limit OOM does it mid-action." >&2
    kill "$MCP_PID" "$SIDECAR_PID" "$KEEPALIVE_PID" 2>/dev/null
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
      kill "$MCP_PID" "$SIDECAR_PID" "$KEEPALIVE_PID" 2>/dev/null
      exit 1
    fi
  fi
done
