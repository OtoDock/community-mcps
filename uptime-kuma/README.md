# Uptime Kuma

Uptime monitoring dashboard integration via [`@davidfuchs/mcp-uptime-kuma`](https://www.npmjs.com/package/@davidfuchs/mcp-uptime-kuma).

| Field | Value |
|-------|-------|
| Manifest name | `uptime-kuma` |
| Runtime | Node (stdio) |
| Upstream | `npm:@davidfuchs/mcp-uptime-kuma@0.7.0` |
| Credentials (per instance) | `UPTIME_KUMA_URL`, `UPTIME_KUMA_USERNAME`, `UPTIME_KUMA_PASSWORD` |
| Per-tool cost | None |
| Assignment mode | `explicit` |
| Requires | Uptime Kuma **v2** (current `master` / preview tag) |

## What it does

Reads monitor status, creates and edits monitors, manages maintenance windows, and inspects incidents on an Uptime Kuma instance. Useful for ops agents that need to acknowledge alerts or set up new monitors during deploys.

## Install layout

- `manifest.json` — MCP descriptor.
- The platform installs the upstream npm package (manifest `source`) at install time; `package.json` / `node_modules/` are generated in the install dir, not committed here.

## Operator notes

- The upstream MCP targets Uptime Kuma's **v2** API which is currently shipped only on the preview/master image (`louislam/uptime-kuma:beta` at time of writing). Stable v1 (`louislam/uptime-kuma:1`) doesn't expose the needed endpoints; the MCP will return clear errors.
- Use a dedicated low-privilege Uptime Kuma user; the MCP's destructive tools (delete monitor, reset incident) can otherwise wreak havoc.
