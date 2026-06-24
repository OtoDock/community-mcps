# Browser (Camoufox)

Anti-detect browser automation over the Playwright MCP, using the Camoufox patched Firefox build.

| Field | Value |
|-------|-------|
| Manifest name | `camoufox` |
| Server name (MCP) | `playwright` |
| Runtime | Docker |
| Upstream | [`@playwright/mcp@0.0.68`](https://www.npmjs.com/package/@playwright/mcp) + [`camoufox`](https://camoufox.com/) |
| Transport | HTTP (`/mcp` and `/sse`) on port `8931` |
| Credentials | None |
| Per-tool cost | None |

## What it does

Provides a browser the agent can drive: navigate, click, fill, screenshot, scrape. Camoufox is a Firefox build that suppresses common automation fingerprints (canvas, WebGL, audio, fonts, headers) so the agent can interact with sites that ordinarily block headless browsers.

Screenshots are written into the per-session `.screenshots/` workspace folder and garbage-collected at session close.

## Install layout

This folder contains:

- `manifest.json` — the MCP descriptor consumed by the platform.
- `Dockerfile` — builds Python 3.12 + Node + Camoufox + `@playwright/mcp`.
- `docker-compose.yml` — runs the container with shared memory tuned for Firefox.
- `entrypoint.sh` + `launch_server.py` — start an Xvfb display and run Camoufox via its Playwright `launchServer` integration.
- `mcp-config.json` / `camoufox-mcp-config.json` — Playwright MCP config templates.
- `skills/web-browsing.md` — best-practice skill loaded into agent prompts when this MCP is enabled.

## Operator notes

- First boot of the container takes minutes — the Camoufox Firefox binary and the Playwright runtime are downloaded into the image.
- The container exposes port `8931`; the platform auto-discovers it via the manifest's `health_endpoint`.
- Screenshots accumulate under `screenshots/` in the install directory and are GC'd by the framework based on the manifest `outputs[*].gc_after` rule. The repo's `.gitignore` excludes runtime PNG/JPG/CSV files in that directory.
