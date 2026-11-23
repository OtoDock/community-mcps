# Nextcloud

Files, calendar, contacts, notes, deck, Talk, mail and the other Nextcloud apps against the user's own Nextcloud account, via [`nextcloud-mcp-server`](https://pypi.org/project/nextcloud-mcp-server/).

| Field | Value |
|-------|-------|
| Manifest name | `nextcloud` |
| Runtime | Python (stdio) |
| Upstream | `pypi:nextcloud-mcp-server[documents]` (unpinned: installs the latest release) |
| Credentials | **Per-user** (`NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`) |
| Per-tool cost | None |
| Upstream project | [cbcoutinho](https://github.com/cbcoutinho/nextcloud-mcp-server) |
| Icon | none: Nextcloud's trademark policy (https://nextcloud.com/trademarks/) allows compatibility references for non-commercial use only and requires permission for commercial services |

**Upstream.** The server is AGPL-3.0 and runs unmodified as its own process. The `documents` extra reads PDF and Office files as text.

## What it does

Some 180 tools over the user's Nextcloud:

- Files: list, search, read with text extraction from PDF and Office documents, write text, move, copy, versions, trash, tags, comments.
- Shares.
- The apps the server runs: calendar and tasks, contacts, notes, deck, Talk, mail, tables, collectives, news, cookbook.

Tools for an app the server does not run are hidden.

Files are read and written inline in the tool result. There is no download to or upload from a local path. Text files round-trip. Binary documents are read as extracted text. An edited DOCX/XLSX/PPTX goes back to the user as a file to upload (the skill says so).

## Credential flow

- Each user supplies their own `NEXTCLOUD_HOST` (the server URL), `NEXTCLOUD_USERNAME` and an **app password** in their per-user MCP settings. Enter the URL without a trailing `/` (the server refuses to start with one).
- Create the app password in Nextcloud under Settings > Security > Devices & sessions. With 2FA on, the account password is rejected. An app password is the right credential either way.

## Install layout

- `manifest.json`: MCP descriptor.
- `skills/nextcloud.md`: usage skill (reading and writing files, the binary-document limits, safety rules).
- The platform installs the upstream PyPI package (manifest `source`) into a venv at install time. Nothing else is committed here.

## Operator notes

- This entry replaced `npm:nextcloud-mcp-server` (14 tools, no release since 2025-09). An install of that source is offered the switch on the MCP Servers page. Each user's stored URL moves from `NEXTCLOUD_URL` to `NEXTCLOUD_HOST`, and the username and password carry.
- The old server could download a file into the workspace and upload one back. This one cannot (see "What it does").
- Upstream releases often (pre-1.0) and the weekly update follows. Upstream's semantic search, OCR and observability extras are not installed (only `documents`).
