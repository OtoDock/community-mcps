FROM python:3.12-slim

# Node.js 22 from Debian trixie repos (python:3.12-slim is trixie-based)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    xvfb x11-utils \
    libgtk-3-0 libx11-xcb1 libasound2 libgbm1 libnss3 \
    libxcomposite1 libxdamage1 libxrandr2 libxkbcommon0 \
    libdbus-glib-1-2 libxt6 libpci3 \
    fonts-liberation fonts-noto \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install camoufox (Python) — includes Playwright + patched Firefox binary
RUN pip install --no-cache-dir "camoufox[geoip]" \
    && python3 -m camoufox fetch --browserforge

# Install @playwright/mcp v0.0.55 — uses playwright-core 1.58.x matching camoufox
RUN npm install -g --no-fund --no-audit "@playwright/mcp@0.0.55"

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
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER camoufox

EXPOSE 8931
ENTRYPOINT ["/entrypoint.sh"]
