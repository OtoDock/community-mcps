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

# Start playwright-mcp connecting to the local camoufox WebSocket
exec npx @playwright/mcp@0.0.55 \
  --config /app/mcp-config.json \
  --port 8931 \
  --host 0.0.0.0 \
  --isolated \
  --output-dir /screenshots \
  --caps vision \
  --viewport-size "1920x1080"
