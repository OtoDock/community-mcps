FROM python:3.13-slim-trixie

# ffmpeg (trixie ships 7.x with libass, xfade, lut3d, loudnorm — everything the
# compiler emits). Fonts: caption burning (libass via fontconfig) needs real
# faces in the image — OFL/Apache-licensed set + Noto for multilingual
# coverage (incl. Greek) + color emoji for short-form captions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-inter fonts-roboto-unhinted fonts-liberation fonts-dejavu-core \
    fonts-noto-core fonts-noto-color-emoji fonts-jetbrains-mono \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash mcp

WORKDIR /app
COPY requirements.txt .
# Chromium headless shell for render_motion_clip — installed system-wide
# (root, before USER mcp) so the runtime user can read it. --with-deps pulls
# the browser's shared-library dependencies via apt.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium-headless-shell \
    && chmod -R a+rX /opt/ms-playwright \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY *.py .
# Vendored detection models (YuNet for smart_reframe — MIT, see models/README.md).
COPY models/ models/

# The built-in look library ships as generated .cube LUTs — emitted from the
# recipes in color.py at build time (we own the recipes; nothing third-party
# is redistributed).
RUN python -c "import color; color.emit_builtin_looks('/app/looks')" \
    && chmod -R a+r /app/looks

USER mcp
EXPOSE 8933
CMD ["python", "server.py"]
