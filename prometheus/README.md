# Prometheus

Metrics and monitoring queries against a Prometheus server, via [`prometheus-mcp`](https://www.npmjs.com/package/prometheus-mcp).

| Field | Value |
|-------|-------|
| Manifest name | `prometheus` |
| Runtime | Node (stdio) |
| Upstream | `npm:prometheus-mcp@1.1.3` |
| Credentials (per instance) | `PROMETHEUS_URL` |
| Per-tool cost | None |
| Assignment mode | `explicit` |

## What it does

Exposes Prometheus's HTTP query API to agents: instant queries (`query`), range queries (`query_range`), label discovery (`label_names`, `label_values`), and series listing. The agent can then ask "show me CPU usage on host X over the last hour" and translate that into PromQL via the tools.

## Install layout

- `manifest.json` — MCP descriptor. Single instance field (`PROMETHEUS_URL`) since most users have one Prometheus.
- `package.json` — pins upstream npm version. `node_modules/` is not committed.

## Operator notes

- Default `PROMETHEUS_URL` is `http://localhost:9090`. For Dockerised Prometheus on the same host as OtoDock, use the container's hostname (`http://prometheus:9090`) or the host gateway.
- If your Prometheus sits behind Basic Auth or an OAuth proxy, this MCP doesn't currently pass credentials — expose an internal-only port or use a service token reverse-proxy.
