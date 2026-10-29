# Browser (Camoufox)

Anti-detect browser automation: [Camoufox](https://github.com/daijro/camoufox), a Firefox build that resists fingerprinting, driven through Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp). OtoDock wrote the wrapper (the image, the launcher and the skill); the browser and the MCP server are those two projects.

| Field | Value |
|-------|-------|
| Manifest name | `camoufox` |
| Server name (MCP) | `platform` |
| Runtime | Docker |
| Upstream | [`@playwright/mcp@0.0.78`](https://www.npmjs.com/package/@playwright/mcp) ([microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)) + [`camoufox`](https://camoufox.com/) ([daijro/camoufox](https://github.com/daijro/camoufox)) |
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
- `healthprobe.py` — the health checker the container runs, and the reads of its result for the healthcheck and the watchdog.
- `stream_sidecar.py` — the session sidecar in front of the MCP server (the same text as the platform's shared copy).
- `camoufox-mcp-config.json` — the Playwright MCP config template.
- `skills/web-browsing.md` — best-practice skill loaded into agent prompts when this MCP is enabled.

## Operator notes

- The image pins three versions together, the Dockerfile's `ARG`s: camoufox, Python Playwright and `@playwright/mcp` (the browser build follows camoufox's own pin). `@playwright/mcp` is welded to the one nightly Playwright build it bundles, and camoufox supports Playwright below 1.63, so a newer `@playwright/mcp` waits for a camoufox that supports its Playwright.
- First boot of the container takes minutes — the Camoufox Firefox binary and the Playwright runtime are downloaded into the image.
- The container exposes port `8931`; the platform auto-discovers it via the manifest's `health_endpoint`.
- The container writes screenshots to a shared `/screenshots` dir (swept of orphans by the sidecar); the platform relocates each into the user's hidden `.screenshots/` workspace folder, bounded by the manifest's `outputs[*].keep_recent` cap (newest 15). The repo's `.gitignore` excludes runtime PNG/JPG/CSV files.
- **Health.** A checker inside the container holds one session the way an agent does and loads a page in it every 2 minutes, and opens a fresh session every 15 minutes. A pass is a real answer from the browser. The container's health and its watchdog read that result; nothing else opens sessions for health. Two failed checks of either kind in a row restart the container, as does a checker that stops reporting.
- **Daily restart.** Once the browser has been up a day and no agent session has been open for 30 minutes, the container restarts while nobody uses it (about 25 seconds). A long-running browser can stop opening new pages after days of use, and a fresh one never meets that. An agent session that starts inside those seconds cannot reach the browser for that session.
- **Logs.** The checker writes a line when a check fails and when checks pass again; the watchdog writes the reason for every restart (`docker logs <container>`).
