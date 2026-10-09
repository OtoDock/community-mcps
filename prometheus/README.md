# Prometheus

Metrics and monitoring queries against a Prometheus server (or a compatible one: Mimir, Cortex, Thanos), via [`prometheus-mcp-server`](https://pypi.org/project/prometheus-mcp-server/).

| Field | Value |
|-------|-------|
| Manifest name | `prometheus` |
| Runtime | Python (stdio) |
| Upstream | `pypi:prometheus-mcp-server` (unpinned: installs the latest release; MIT) |
| Credentials (per instance) | `PROMETHEUS_URL`; optional `PROMETHEUS_USERNAME` + `PROMETHEUS_PASSWORD` or `PROMETHEUS_TOKEN`, `ORG_ID`, `PROMETHEUS_URL_SSL_VERIFY` |
| Per-tool cost | None |
| Assignment mode | `explicit` |
| Upstream project | [pab1it0](https://github.com/pab1it0/prometheus-mcp-server) |
| Icon | the Prometheus icon from https://github.com/cncf/artwork (projects/prometheus/icon/color): the Linux Foundation trademark policy permits unaltered project logos to state compatibility |

## What it does

Exposes Prometheus's HTTP query API to agents, read-only: instant and range PromQL queries (`execute_query`, `execute_range_query`), metric discovery (`list_metrics`, `get_metric_metadata`), scrape targets (`get_targets`) and a `health_check`. The agent can then ask "show me CPU usage on host X over the last hour" and translate that into PromQL.

## Install layout

- `manifest.json` — MCP descriptor. The instance holds the server URL and, when the server needs it, the credentials.
- The platform installs the upstream PyPI package (manifest `source`) into a venv at install time; nothing else is committed here.

## Operator notes

- Default `PROMETHEUS_URL` is `http://localhost:9090`. For Dockerised Prometheus on the same host as OtoDock, use the container's hostname (`http://prometheus:9090`) or the host gateway.
- Behind basic auth, fill the username and password; behind a bearer-token proxy (Grafana Cloud, an OAuth proxy), the token, which wins when both are set. `ORG_ID` is the tenant header of Mimir, Cortex and Thanos.
- A self-signed certificate: set `PROMETHEUS_URL_SSL_VERIFY` to `false`.
- This entry replaced `npm:prometheus-mcp` (no release since 2025-07): an install of that source is offered the switch on the MCP Servers page, and its URL carries over.
