# Nextcloud

WebDAV file storage, Notes, and Calendar access against a Nextcloud server, via [`nextcloud-mcp-server`](https://www.npmjs.com/package/nextcloud-mcp-server).

| Field | Value |
|-------|-------|
| Manifest name | `nextcloud` |
| Runtime | Node (stdio) |
| Upstream | `npm:nextcloud-mcp-server@1.1.0` |
| Credentials | **Per-user** (`NEXTCLOUD_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`) |
| Per-tool cost | None |

## What it does

Lets agents read/write files in the user's Nextcloud, browse the Files tree, fetch/edit Notes (Nextcloud Notes app), and inspect calendar entries (CalDAV).

## Credential flow

- Each user supplies their own `NEXTCLOUD_URL`, `NEXTCLOUD_USERNAME`, and `NEXTCLOUD_PASSWORD` (or App Password) in their per-user MCP settings.

For Nextcloud servers with 2FA enabled, users **must** generate an [App Password](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html#app-passwords) — the regular login password will be rejected by the WebDAV endpoint.

## Install layout

- `manifest.json` — MCP descriptor.
- The platform installs the upstream npm package (manifest `source`) at install time; `package.json` / `node_modules/` are generated in the install dir, not committed here.
- `skills/nextcloud.md` — usage skill (file reading/editing workflow, safety rules).
