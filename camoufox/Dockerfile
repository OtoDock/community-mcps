FROM python:3.12-slim

# System deps for the patched Firefox + Xvfb. curl/gnupg/ca-certificates are
# for the NodeSource setup below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    xvfb x11-utils \
    libgtk-3-0 libx11-xcb1 libasound2 libgbm1 libnss3 \
    libxcomposite1 libxdamage1 libxrandr2 libxkbcommon0 \
    libdbus-glib-1-2 libxt6 libpci3 \
    fonts-liberation fonts-noto \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS, PINNED via NodeSource. Debian trixie's `nodejs` package has
# moved to Node 24; this image ran on Node 20 for months. Pinning keeps the
# build reproducible and matches the @playwright/mcp@0.0.68 + Playwright 1.59
# stack below (an unpinned Node 24 broke a rebuild).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install camoufox (Python) + its patched Firefox. PIN Playwright to 1.59.0:
# camoufox 0.4.11 does NOT pin Playwright (requires_dist just says "playwright"),
# so a fresh build otherwise pulls a too-new Playwright. 1.59.0 gives a node
# binary + driver that still ship `browserServerImpl.js` (camoufox's launchServer
# requires it; 1.60+ dropped it). The driver's playwright-core is then OVERWRITTEN
# below with @playwright/mcp's exact build so the connect protocol matches.
RUN pip install --no-cache-dir "playwright==1.59.0" "camoufox[geoip]==0.4.11" \
    && python3 -m camoufox fetch --browserforge

# @playwright/mcp 0.0.68 — fixes the server→client request-correlation bug 0.0.55
# had (which forced the OTO_MCP_SUPPRESS_SERVER_REQUESTS sidecar workaround, now
# removed). It is a thin shim over playwright/lib/mcp, which exists ONLY in the
# nightly playwright build it bundles (playwright-core 1.59.0-alpha-1771104257000):
# the MCP server is welded to that exact build and will NOT run on stable
# playwright. 0.0.72+ need playwright 1.60+ (drops browserServerImpl), so 0.0.68 is
# the ceiling for camoufox 0.4.11.
RUN npm install -g --no-fund --no-audit "@playwright/mcp@0.0.68"

# Align camoufox's launch driver with @playwright/mcp's playwright-core. The MCP
# client connects to camoufox's launchServer over Playwright's wire protocol,
# matched per EXACT build — a stable-1.59 server vs the alpha-1.59 client fails
# mid-call ("Cannot find object to __adopt__"). camoufox's launchServer.js does
# require(`${cwd}/lib/browserServerImpl.js`) where cwd is the Python driver's
# playwright-core, so overwrite that package with @playwright/mcp's bundled alpha
# core: now both ends share ONE identical pinned build (the alpha timestamp is
# frozen → reproducible). The driver's node binary (a sibling dir) is kept.
RUN MCP_CORE="$(npm root -g)/@playwright/mcp/node_modules/playwright-core" \
 && PY_PKG="$(python3 -c 'from camoufox.server import get_nodejs; from pathlib import Path; print(Path(get_nodejs()).parent / "package")')" \
 && test -f "$MCP_CORE/lib/browserServerImpl.js" \
 && rm -rf "$PY_PKG" && cp -r "$MCP_CORE" "$PY_PKG" \
 && node -e "const v=require('$PY_PKG/package.json').version; console.log('camoufox launch core ->', v); if(!v.includes('alpha')) process.exit(1)"

# aiohttp for the stream sidecar (SSE broker + per-session lifecycle).
RUN pip install --no-cache-dir aiohttp

# Xvfb socket directory
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Non-root user (camoufox crashes as root on Linux)
RUN useradd -m camoufox \
    && mkdir -p /home/camoufox/.cache \
    && cp -r /root/.cache/camoufox /home/camoufox/.cache/camoufox \
    && chown -R camoufox:camoufox /home/camoufox/.cache \
    && mkdir -p /screenshots && chown camoufox:camoufox /screenshots

COPY launch_server.py /app/launch_server.py
COPY camoufox-mcp-config.json /app/mcp-config.json
COPY healthprobe.py /app/healthprobe.py
# The streaming sidecar (SSE broker + session lifecycle) is vendored into this
# folder so the MCP builds standalone — no cross-folder build context. It's a
# verbatim copy of oto-dock's mcps/_shared/stream_sidecar.py (the canonical
# source); keep it in sync if that file changes.
COPY stream_sidecar.py /app/stream_sidecar.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER camoufox

EXPOSE 8931
ENTRYPOINT ["/entrypoint.sh"]
