# UniFi Network

Network management, firewall, monitoring, DPI, VPN, and client tracking for UniFi Dream Machine / Dream Router / Dream Wall, via [`unifi-network-mcp`](https://pypi.org/project/unifi-network-mcp/).

| Field | Value |
|-------|-------|
| Manifest name | `unifi-network` |
| Runtime | Python (stdio) |
| Upstream | `pypi:unifi-network-mcp@0.14.2` ([sirkirby/unifi-network-mcp](https://github.com/sirkirby/unifi-network-mcp)) |
| Credentials (per instance) | `UNIFI_HOST`, `UNIFI_USERNAME`, `UNIFI_PASSWORD`, optional `UNIFI_PORT`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL` |
| Per-tool cost | None |
| Assignment mode | `explicit` |
| Tool count | ~156 |

## What it does

Exposes the full UniFi Network controller API: list devices, see connected clients, inspect/modify firewall rules, manage VPN tunnels, run speed tests, query DPI stats, restart devices, adjust port-forwarding.

Tool registration is set to `eager` (see `manifest.json::env::UNIFI_TOOL_REGISTRATION_MODE`) so all 156 tools are available immediately — this is required for the LLM to discover the firewall/VPN tools without first running a discovery RPC.

## Install layout

- `manifest.json` — MCP descriptor.

## Operator notes

- Create a **local UniFi account** dedicated to the MCP — don't reuse your UI.com SSO login (UniFi's API can't refresh Ubiquiti cloud SSO tokens reliably).
- Give the account the **"Limited Admin"** role with the permissions you need; full admin is rarely required.
- For self-signed certs (typical on a fresh UDM), set `UNIFI_VERIFY_SSL=false`. For a CA-signed cert behind a domain, leave it `true`.
- `UNIFI_SITE=default` matches a single-site install. Multi-site setups expose the site identifier in the UniFi UI's URL.

> Replaced from an earlier (Node, abandoned) implementation in favor of this Python port — it has 156 tools vs. the predecessor's 3 and is actively maintained.
