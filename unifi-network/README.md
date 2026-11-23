# UniFi Network

Network management, firewall, monitoring, DPI, VPN, and client tracking for UniFi Dream Machine / Dream Router / Dream Wall, via [`unifi-network-mcp`](https://pypi.org/project/unifi-network-mcp/).

| Field | Value |
|-------|-------|
| Manifest name | `unifi-network` |
| Runtime | Python (stdio) |
| Upstream | `pypi:unifi-network-mcp`, the Network server of [sirkirby/unifi-mcp](https://github.com/sirkirby/unifi-mcp). The package is unpinned, so the install takes its latest release. |
| Credentials (per instance) | `UNIFI_HOST`, `UNIFI_USERNAME`, `UNIFI_PASSWORD`, optional `UNIFI_PORT`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL` |
| Per-tool cost | None |
| Assignment mode | `explicit` |
| Upstream project | [sirkirby](https://github.com/sirkirby/unifi-mcp) |
| Icon | none: Ubiquiti's trademark guidelines require written permission for any use of the UniFi logo and forbid placing it next to third-party products |

## What it does

Exposes the full UniFi Network controller API: list devices, see connected clients, inspect/modify firewall rules, manage VPN tunnels, run speed tests, query DPI stats, restart devices, adjust port-forwarding.

Tool registration is set to `eager` (see `manifest.json::env::UNIFI_TOOL_REGISTRATION_MODE`), so every tool is available immediately. The LLM needs eager registration to discover the firewall/VPN tools without first running a discovery RPC.

## Install layout

- `manifest.json`: the MCP descriptor.

## Operator notes

- Create a **local UniFi account** dedicated to the MCP. Don't reuse your UI.com SSO login: UniFi's API can't refresh Ubiquiti cloud SSO tokens reliably.
- Give the account the **"Limited Admin"** role with the permissions you need. Full admin is rarely required.
- The default `UNIFI_VERIFY_SSL=false` fits a self-signed cert (typical on a fresh UDM). For a CA-signed cert behind a domain, set it to `true`.
- `UNIFI_SITE=default` matches a single-site install. Multi-site setups expose the site identifier in the UniFi UI's URL.

> This Python port replaced an earlier Node implementation, which was abandoned. The port has some two hundred tools against the predecessor's 3 and is actively maintained. The repository moved to `sirkirby/unifi-mcp`, a monorepo where the Network server lives under `apps/network`. The PyPI package kept its name.
