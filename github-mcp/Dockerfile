# Multi-stage build:
#   Stage 1 — clone + compile github/github-mcp-server (Go stdio binary).
#   Stage 2 — Python slim with the binary plus the streamable-HTTP sidecar.
#
# The default ref is `main`. Pin to a specific tag/commit in production via
# `docker build --build-arg GITHUB_MCP_REF=v1.2.3 .` — that gives reproducible
# behavior across rebuilds. Releases are at
# https://github.com/github/github-mcp-server/releases

# ---------- Stage 1: build github-mcp-server ----------
# Track the GO_VERSION arg to whatever go.mod in upstream HEAD requires.
# Bump it (or pin GITHUB_MCP_REF below to an older tag) when upstream's
# go.mod version moves past our base image.
FROM golang:1.25-alpine AS builder

RUN apk add --no-cache git

ARG GITHUB_MCP_REF=main
ARG GITHUB_MCP_REPO=https://github.com/github/github-mcp-server.git

WORKDIR /src
RUN git clone --depth 1 --branch "${GITHUB_MCP_REF}" "${GITHUB_MCP_REPO}" . \
    || (git clone "${GITHUB_MCP_REPO}" . && git checkout "${GITHUB_MCP_REF}")
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" \
    -o /out/github-mcp-server ./cmd/github-mcp-server

# ---------- Stage 2: sidecar runtime ----------
FROM python:3.12-slim

COPY --from=builder /out/github-mcp-server /usr/local/bin/github-mcp-server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sidecar.py .

RUN useradd -m -s /bin/bash mcp
USER mcp

EXPOSE 8935
# uvicorn binds to ${MCP_PORT} (the platform injects it from manifest.port).
# Fallback 8935 matches the manifest default so local `docker run` works
# without env wiring.
CMD ["sh", "-c", "exec uvicorn sidecar:app --host 0.0.0.0 --port ${MCP_PORT:-8935}"]
