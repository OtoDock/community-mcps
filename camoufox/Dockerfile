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
# build reproducible and matches the @playwright/mcp@0.0.55 + Playwright 1.58
# stack below (an unpinned Node 24 broke a rebuild).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install camoufox (Python) + its patched Firefox. PIN Playwright to 1.58.0:
# camoufox 0.4.11 does NOT pin Playwright (requires_dist just says "playwright"),
# so a fresh build otherwise pulls the latest Playwright whose driver dropped
# `browserServerImpl.js` that camoufox's launchServer.js requires → the Firefox
# server fails to launch. 1.58.0 also matches @playwright/mcp@0.0.55's bundled
# playwright-core (connect compatibility).
RUN pip install --no-cache-dir "playwright==1.58.0" "camoufox[geoip]==0.4.11" \
    && python3 -m camoufox fetch --browserforge

# Install @playwright/mcp v0.0.55 — uses playwright-core 1.58.x matching camoufox
RUN npm install -g --no-fund --no-audit "@playwright/mcp@0.0.55"

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
