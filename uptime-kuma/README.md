# Uptime Kuma

Uptime monitoring dashboard integration via [`@davidfuchs/mcp-uptime-kuma`](https://www.npmjs.com/package/@davidfuchs/mcp-uptime-kuma).

| Field | Value |
|-------|-------|
| Manifest name | `uptime-kuma` |
| Runtime | Node (stdio) |
| Upstream | `npm:@davidfuchs/mcp-uptime-kuma` (unpinned: installs the latest release; needs Node 22) |
| Credentials (per instance) | `UPTIME_KUMA_URL`, and `UPTIME_KUMA_USERNAME` + `UPTIME_KUMA_PASSWORD` or `UPTIME_KUMA_JWT_TOKEN` (none for an instance without login) |
| Per-tool cost | None |
| Assignment mode | `explicit` |
| Requires | Uptime Kuma **v2** (current `master` / preview tag) |
| Upstream project | [DavidFuchs](https://github.com/DavidFuchs/mcp-uptime-kuma) |
| Icon | the Uptime Kuma icon (public/icon.svg of https://github.com/louislam/uptime-kuma, MIT), unaltered |

## What it does

Reads monitor status, creates and edits monitors, manages maintenance windows, and inspects incidents on an Uptime Kuma instance. Useful for ops agents that need to acknowledge alerts or set up new monitors during deploys.

## Install layout

- `manifest.json` — MCP descriptor.
- The platform installs the upstream npm package (manifest `source`) at install time; `package.json` / `node_modules/` are generated in the install dir, not committed here.

## Operator notes

- The upstream MCP targets Uptime Kuma's **v2** API which is currently shipped only on the preview/master image (`louislam/uptime-kuma:beta` at time of writing). Stable v1 (`louislam/uptime-kuma:1`) doesn't expose the needed endpoints; the MCP will return clear errors.
- Sign-in: a username and password, or a JWT token, which wins when both are set and is the way in when the account has 2FA (a stored one-time 2FA code would be stale within a minute, so the instance form does not offer one). Get the token once with `npx -p @davidfuchs/mcp-uptime-kuma@latest mcp-uptime-kuma-get-jwt <url> <user> <password>` and paste it. An instance with login disabled needs only the URL.
- Use a dedicated low-privilege Uptime Kuma user; the MCP's destructive tools (delete monitor, reset incident) can otherwise wreak havoc.
