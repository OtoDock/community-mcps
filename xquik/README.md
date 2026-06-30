# Xquik

Xquik provides X data API and automation tools through a hosted MCP server. This entry uses `mcp-remote` to expose the remote server to OtoDock as a local stdio MCP.

| Field | Value |
|-------|-------|
| Manifest name | `xquik` |
| Runtime | Node (stdio) |
| Upstream | `npm:mcp-remote@0.1.38` |
| Credentials (per instance) | `XQUIK_API_KEY` |
| Per-tool cost | None |
| Assignment mode | `explicit` |

## What it does

Connects agents to Xquik's hosted MCP endpoint at `https://xquik.com/mcp`. Operators can use it for X data workflows through Xquik's public API contract after adding a Xquik API key to the instance configuration.

## Install layout

- `manifest.json` - MCP descriptor. It launches the pinned `mcp-remote` package and forwards `XQUIK_API_KEY` as an `X-API-Key` header.
- `package.json` - pins the wrapper package version. `node_modules/` is not committed.

## Operator notes

- Create a Xquik API key from the Xquik dashboard, then store it in the `XQUIK_API_KEY` instance field.
- The entry uses HTTP transport only because Xquik exposes a streamable HTTP MCP endpoint.
- Xquik MCP documentation is available at <https://docs.xquik.com/mcp/overview>.
