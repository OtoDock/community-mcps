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
  # The health checker's status file (healthprobe.py): written by the checker
  # as camoufox, read by the compose healthcheck as root and by the watchdog
  # below. A container restart keeps its filesystem, so a status left by the
  # previous run (two failures, a day of uptime) is removed before anything
  # reads it.
  mkdir -p /run/probe && chown camoufox:camoufox /run/probe
  rm -f /run/probe/status.json /run/probe/status.json.* /run/probe/healthprobe.mcp-session-id
  exec gosu camoufox "$0" "$@"
fi

STARTED=$(date +%s)

# Clean stale Xvfb lock files
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start virtual display
Xvfb :99 -screen 0 2560x1440x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99

# Wait for Xvfb readiness
for _ in $(seq 1 10); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.5
done

# Start camoufox browser WebSocket server in background
python3 /app/launch_server.py &

# Wait for camoufox WebSocket to be ready (up to 30s)
for _ in $(seq 1 60); do
  python3 -c "import socket; s=socket.create_connection(('localhost', 3000)); s.close()" 2>/dev/null && break
  sleep 0.5
done

echo "Camoufox ready, starting MCP server..."

# Start playwright-mcp on an INTERNAL port (127.0.0.1:8930). The stream sidecar
# (below) is the public listener on 8931 and forwards to it — see
# stream_sidecar.py. --isolated gives each MCP session its own browser context.
playwright-mcp \
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
for _ in $(seq 1 60); do
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

# Wait for the sidecar before the checker's first session.
for _ in $(seq 1 20); do
  python3 -c "import socket; s=socket.create_connection(('127.0.0.1', 8931)); s.close()" 2>/dev/null && break
  sleep 0.5
done

# The health checker (healthprobe.py --checker): one client that holds a
# session the way an agent does and loads a page in it every 2 minutes, opens
# a fresh session every 15 minutes, and writes the result for the compose
# healthcheck and the watchdog below. Nothing else opens sessions for health.
python3 /app/healthprobe.py --checker &
CHECKER_PID=$!

# Clean shutdown: on container stop/restart, kill all children.
trap 'kill "$MCP_PID" "$SIDECAR_PID" "$CHECKER_PID" 2>/dev/null; exit 0' TERM INT

# Watchdog / auto-recycle. The single long-lived camoufox Firefox can wedge
# after long uptime: the MCP server stays up and answers, but no page loads,
# for new sessions or for all (seen after ~4 days of use, once while an older
# probe kept passing). The checker sees it; the watchdog reads
# its verdict and, on a recycle, kills the children and exits non-zero so
# `restart: always` brings up a FRESH browser.
#
# Verdict 2 is the daily restart: once the browser has been up a day and no
# agent session has been open for 30 minutes, it restarts while nobody uses
# it. upstream has no fix for the wedge, and a fresh browser never meets it.
#
# The loop also recycles PROACTIVELY on high memory: camoufox's Firefox keeps
# ~1-2MB per browser context it ever opened (agents' sessions, and the
# checker's ~96 fresh sessions a day, which the daily restart clears).
# Recycling at 2.5GiB of memory the kernel cannot reclaim (cgroup v2 anon +
# shmem) turns an eventual mid-action OOM kill under the compose mem_limit:3g
# into a clean restart with headroom to spare. Page cache is left out: the
# browser maps ~2 GB of fingerprint fonts, and that cache is reclaimed under
# the limit (six parallel sessions peak at the limit with no OOM), so counting
# it would recycle healthy bursts.
MEM_RECYCLE_BYTES=$((2560 * 1024 * 1024))
while true; do
  sleep 120
  if ! kill -0 "$MCP_PID" 2>/dev/null; then
    echo "[watchdog] MCP server exited — recycling container." >&2
    kill "$SIDECAR_PID" "$CHECKER_PID" 2>/dev/null
    exit 1
  fi
  if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "[watchdog] stream sidecar exited — recycling container." >&2
    kill "$MCP_PID" "$CHECKER_PID" 2>/dev/null
    exit 1
  fi
  if ! kill -0 "$CHECKER_PID" 2>/dev/null; then
    # Without the checker nothing would notice a wedge: recycle rather than
    # degrade silently (consistent with the other children).
    echo "[watchdog] health checker exited — recycling container." >&2
    kill "$MCP_PID" "$SIDECAR_PID" 2>/dev/null
    exit 1
  fi
  mem_now=$(awk '$1 == "anon" || $1 == "shmem" { n += $2 } END { if (NR) print n + 0 }' \
    /sys/fs/cgroup/memory.stat 2>/dev/null)
  [ -n "$mem_now" ] || mem_now=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0)
  if [ "$mem_now" -gt "$MEM_RECYCLE_BYTES" ]; then
    echo "[watchdog] memory ${mem_now} > ${MEM_RECYCLE_BYTES} — recycling before the mem_limit OOM does it mid-action." >&2
    kill "$MCP_PID" "$SIDECAR_PID" "$CHECKER_PID" 2>/dev/null
    exit 1
  fi
  # The checker's verdict (it opens no session). The exit code is captured
  # here: under `set -e` a bare non-zero call would end the script before the
  # cleanup below.
  rc=0
  reason=$(python3 /app/healthprobe.py --verdict "$STARTED" 2>&1) || rc=$?
  case "$rc" in
    0) ;;
    2)
      echo "[watchdog] ${reason} — restarting the browser while nobody uses it." >&2
      kill "$MCP_PID" "$SIDECAR_PID" "$CHECKER_PID" 2>/dev/null
      exit 1
      ;;
    *)
      echo "[watchdog] ${reason} — recycling the container." >&2
      kill "$MCP_PID" "$SIDECAR_PID" "$CHECKER_PID" 2>/dev/null
      exit 1
      ;;
  esac
done
