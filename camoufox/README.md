# Browser (Camoufox)

Anti-detect browser automation: [Camoufox](https://github.com/daijro/camoufox), a Firefox build that resists fingerprinting, driven through Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp). OtoDock wrote the wrapper (the image, the launcher and the skill); the browser and the MCP server are those two projects.

| Field | Value |
|-------|-------|
| Manifest name | `camoufox` |
| Server name (MCP) | `playwright` |
| Runtime | Docker |
| Upstream | [`@playwright/mcp@0.0.68`](https://www.npmjs.com/package/@playwright/mcp) ([microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)) + [`camoufox`](https://camoufox.com/) ([daijro/camoufox](https://github.com/daijro/camoufox)) |
| Transport | HTTP (`/mcp`) on port `8931` |
| Credentials | None |
| Per-tool cost | None |
| Upstream project | [OtoDock](https://github.com/OtoDock/community-mcps/tree/main/camoufox) |
| Icon | the OtoDock mark (OtoDock wrote this wrapper); Camoufox and Playwright MCP marks are not used |

## What it does

Provides a browser the agent can drive: navigate, click, fill, screenshot, scrape. Camoufox is a Firefox build that suppresses common automation fingerprints (canvas, WebGL, audio, fonts, headers) so the agent can interact with sites that ordinarily block headless browsers.

Screenshots are written into the user's hidden `.screenshots/` workspace folder, kept to the most recent 15.

## Install layout

This folder contains:

- `manifest.json` — the MCP descriptor consumed by the platform.
- `Dockerfile` — builds Python 3.13 + Node + Camoufox + `@playwright/mcp`.
- `docker-compose.yml` — runs the container with shared memory tuned for Firefox.
- `entrypoint.sh` + `launch_server.py` — start an Xvfb display and run Camoufox via its Playwright `launchServer` integration.
- `camoufox-mcp-config.json` — the Playwright MCP config template.
- `skills/web-browsing.md` — best-practice skill loaded into agent prompts when this MCP is enabled.

## Operator notes

- First boot of the container takes minutes — the Camoufox Firefox binary and the Playwright runtime are downloaded into the image.
- The container exposes port `8931`; the platform auto-discovers it via the manifest's `health_endpoint`.
- The container writes screenshots to a shared `/screenshots` dir (swept of orphans by the sidecar); the platform relocates each into the user's hidden `.screenshots/` workspace folder, bounded by the manifest's `outputs[*].keep_recent` cap (newest 15). The repo's `.gitignore` excludes runtime PNG/JPG/CSV files.
