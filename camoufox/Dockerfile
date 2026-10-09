FROM python:3.13-slim

# The three pins move together: @playwright/mcp bundles one exact nightly
# Playwright build, and camoufox caps the Playwright it supports. 0.0.78
# bundles 1.62.0-alpha; 0.0.79+ bundle 1.63+, outside camoufox 0.5.8's
# `playwright<1.63`.
ARG PLAYWRIGHT_VERSION=1.62.0
ARG CAMOUFOX_VERSION=0.5.8
ARG PLAYWRIGHT_MCP_VERSION=0.0.78

# System deps for the patched Firefox + Xvfb. curl/gnupg/ca-certificates are
# for the NodeSource setup below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates gosu \
    xvfb x11-utils \
    libgtk-3-0 libx11-xcb1 libasound2 libgbm1 libnss3 \
    libxcomposite1 libxdamage1 libxrandr2 libxkbcommon0 \
    libdbus-glib-1-2 libxt6 libpci3 \
    fonts-liberation fonts-noto \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 LTS, PINNED via NodeSource (Debian trixie's `nodejs` follows the
# newest major). It runs @playwright/mcp only; camoufox's launch server runs on
# the node binary bundled with the Python driver.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (camoufox crashes as root on Linux), created first so the
# browser is fetched straight into its cache: a copy in a later layer would
# store the 2.5 GB browser (mostly its fingerprint fonts) twice.
RUN useradd -m camoufox \
    && mkdir -p /screenshots && chown camoufox:camoufox /screenshots

# Install camoufox (Python) + its patched Firefox, the browser build camoufox
# pins for this release (`browser-pin.json`). `fetch` exits 0 even when the
# download failed, so the build checks that the pinned build is on disk. The
# driver's playwright-core is then OVERWRITTEN below with @playwright/mcp's
# exact build so the connect protocol matches.
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" "camoufox[geoip]==${CAMOUFOX_VERSION}" \
    && HOME=/home/camoufox python3 -m camoufox fetch \
    && python3 -c "import camoufox, glob, json, pathlib, sys; pin = json.loads((pathlib.Path(camoufox.__file__).parent / 'browser-pin.json').read_text()); sys.exit(0 if glob.glob(f'/home/camoufox/.cache/camoufox/browsers/*/{pin[\"version\"]}-{pin[\"build\"]}*') else 'pinned browser missing after fetch')" \
    && chown -R camoufox:camoufox /home/camoufox/.cache

# @playwright/mcp is a thin shim over playwright/lib/mcp, which exists ONLY in
# the nightly playwright build it bundles: the MCP server is welded to that
# exact build and will NOT run on stable playwright.
RUN npm install -g --no-fund --no-audit "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"

# Align camoufox's launch driver with @playwright/mcp's playwright-core. The MCP
# client connects to camoufox's launchServer over Playwright's wire protocol,
# matched per EXACT build — a stable server vs the alpha client fails
# mid-call ("Cannot find object to __adopt__"). camoufox's launchServer.js loads
# the entrypoint (`index.js`) of the Python driver's playwright-core, so
# overwrite that package with @playwright/mcp's bundled alpha core: now both
# ends share ONE identical pinned build (the alpha timestamp is frozen →
# reproducible). The driver's node binary (a sibling dir) is kept.
RUN MCP_CORE="$(npm root -g)/@playwright/mcp/node_modules/playwright-core" \
 && PY_PKG="$(python3 -c 'from camoufox.server import get_nodejs; from pathlib import Path; print(Path(get_nodejs()).parent / "package")')" \
 && test -f "$MCP_CORE/index.js" \
 && rm -rf "$PY_PKG" && cp -r "$MCP_CORE" "$PY_PKG" \
 && node -e "const v=require('$PY_PKG/package.json').version; console.log('camoufox launch core ->', v); if(!v.includes('alpha')) process.exit(1)"

# aiohttp for the stream sidecar (stream pass-through + per-session lifecycle).
RUN pip install --no-cache-dir aiohttp

# Xvfb socket directory
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY launch_server.py /app/launch_server.py
COPY camoufox-mcp-config.json /app/mcp-config.json
COPY healthprobe.py /app/healthprobe.py
# The streaming sidecar (stream pass-through + per-session lifecycle) is vendored
# into this folder so the MCP builds standalone — no cross-folder build context.
# It is kept in sync with the platform's shared sidecar source.
COPY stream_sidecar.py /app/stream_sidecar.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# The container starts as ROOT so the entrypoint can chown the root-owned
# /screenshots mount; it immediately drops to the `camoufox` user via gosu before
# launching anything (camoufox crashes as root). HOME is set explicitly because
# gosu does not, and camoufox/browserforge resolve the patched-Firefox cache via
# ~ → it must point at the camoufox user's home (built at `useradd -m` above).
ENV HOME=/home/camoufox

EXPOSE 8931
ENTRYPOINT ["/entrypoint.sh"]
