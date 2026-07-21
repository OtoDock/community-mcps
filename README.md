# OtoDock Community MCPs

The catalog of [Model Context Protocol](https://modelcontextprotocol.io/) servers (MCPs) that an [OtoDock](https://github.com/OtoDock) instance can install with one click. Each MCP gives agents a new capability — drive a browser, work in Notion, manage GitHub, read and send Gmail, and so on.

This repo is the **source of truth** for what the OtoDock platform offers under the "Community" category. Operators don't clone it directly — the platform pulls `registry.json` from this repo's `main` branch and downloads individual MCP folders on demand.

## What's in here

```
.
├── registry.json            ← generated index, consumed by the platform UI
├── camoufox/                ← one folder per MCP
│   ├── manifest.json        ← required, defines how to install and run
│   ├── README.md            ← required, shown in the install dialog
│   ├── (source / Dockerfile / package.json / patches / skills)
│   └── icon.png             ← optional, 256×256
├── github-mcp/
├── notion-mcp/
├── scripts/
│   └── generate-registry.py ← regenerates registry.json from manifests
├── CONTRIBUTING.md          ← how to add or update an MCP
└── LICENSE
```

Each MCP folder is **self-contained**. No shared `node_modules/`, no shared `venv/`. The platform's installer creates those at runtime in the live install directory.

## Current catalog

| MCP | Runtime | Upstream | Use case |
|-----|---------|----------|----------|
| [camoufox](./camoufox/) | docker | Camoufox + `@playwright/mcp` | Anti-detect browser automation |
| [github-mcp](./github-mcp/) | docker | official GitHub MCP (Dockerized) | Repos, issues, PRs, Actions, code search |
| [ha-mcp](./ha-mcp/) | python | `pypi:ha-mcp` | Home Assistant smart-home control |
| [nextcloud](./nextcloud/) | node | `nextcloud-mcp-server` | Nextcloud files, Notes, Calendar |
| [notion-mcp](./notion-mcp/) | node | `@notionhq/notion-mcp-server` | Notion pages, databases, search |
| [prometheus](./prometheus/) | node | `prometheus-mcp` | Metrics and monitoring queries |
| [unifi-network](./unifi-network/) | python | `pypi:unifi-network-mcp` | UniFi network management and monitoring |
| [uptime-kuma](./uptime-kuma/) | node | `@davidfuchs/mcp-uptime-kuma` | Uptime monitoring (Kuma v2) |
| [video-tools](./video-tools/) | docker | OtoDock (FFmpeg + libass) | Agent-driven video editing: timelines, transitions, captions, grading |

For the schema of `registry.json` and every `manifest.json` field, see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## How OtoDock uses this repo

1. The platform UI's **Browse Community MCPs** page fetches `registry.json` from `https://raw.githubusercontent.com/OtoDock/community-mcps/main/registry.json`.
2. Each card shows the MCP's label, description, runtime, version, tags, and an Install / Request button.
3. When an admin clicks **Install**, the platform downloads the MCP's folder, runs the appropriate installer (npm / pip / docker-compose), and lights the MCP up under `Admin → MCPs`.
4. Managers can request an MCP install for one of their agents; the request goes to admins for approval.

## Contributing

We welcome new MCPs and bug fixes. The bar:

- A `manifest.json` that follows our schema (see `CONTRIBUTING.md`).
- A short `README.md` explaining what the MCP does, what credentials it needs, and any operator gotchas.
- No bundled secrets, no committed `node_modules/`, no committed `venv/`, no committed keys.

Open a PR; CI runs `scripts/generate-registry.py --check` plus a manifest JSON-schema validator. Maintainers merge after a review pass.

## License

Apache 2.0 — see [`LICENSE`](./LICENSE). Individual MCPs may carry their own upstream license; each MCP entry in `registry.json` declares it.
