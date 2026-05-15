# Contributing to OtoDock Community MCPs

Thanks for wanting to add an MCP! This guide covers the layout, schemas, and contracts every entry must follow so the OtoDock platform can install and run it safely across local-sandboxed and remote-satellite deployments.

## Quick start

1. Fork this repo.
2. Create a new folder: `your-mcp-name/`.
3. Write a `manifest.json` (schema below).
4. Write a `README.md` (template at the end of this file).
5. Add your install assets — `Dockerfile`, `package.json`, `requirements.txt`, etc.
6. Run `python scripts/generate-registry.py` to refresh `registry.json`.
7. Open a PR.

CI runs `scripts/generate-registry.py --check` plus schema validation. The maintainer review checks for the items in the **PR review checklist** at the end of this file.

## File layout — one folder per MCP

```
your-mcp-name/
├── manifest.json          # required
├── README.md              # required
├── icon.png               # optional, 256×256 PNG
├── package.json           # if runtime=node — pins the upstream npm package
├── Dockerfile             # if runtime=docker
├── docker-compose.yml     # if runtime=docker
├── requirements.in        # if runtime=python and your MCP has its own source
├── requirements.txt       # if runtime=python — lockfile, regenerated on contribution
├── patches/               # optional — patches applied after the upstream package installs
│   └── *.py.patch
└── skills/                # optional — markdown skill files loaded into agent prompts
    └── *.md
```

Each MCP folder is **self-contained**. There is no shared `node_modules/`, no shared `venv/`. The platform's installer creates those at runtime in the live install directory.

### `.gitignore` at the repo root excludes

`*/node_modules/`, `*/venv/`, `*/__pycache__/`, `*/keys/`, `*/config/`, `*/screenshots/`, `*.log`. **Never commit a real secret, key, or credential.**

## `manifest.json` reference

Top-level fields:

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `name` | string | yes | Unique identifier. Matches folder name. Lowercase + hyphens. |
| `label` | string | yes | Display name shown to operators. |
| `description` | string | yes | One-line description for catalog cards. |
| `version` | semver | yes | MCP version. Bumped on any user-visible change. |
| `category` | string | yes | Always `"community"` here. |
| `server` | object | yes | Runtime + transport config (see below). |
| `credentials` | object | no | `{ "type": "none" | "per_user" }` plus per-user fields if applicable. |
| `instances` | object | no | Admin-managed instances (URL+token, multi-host configs, etc). |
| `config` | array | no | Admin-managed key/value config exposed in the dashboard. |
| `env` | object | no | Static env vars set on every launch. |
| `agent_env` | object | no | Per-session env vars with `${session.*}` tokens. |
| `path_env` | object | no | Workspace-relative paths — see "Path conventions" below. |
| `skills` | array | no | Markdown skill files automatically loaded into agent prompts. |
| `outputs` | array | no | Files the MCP writes that the platform should ferry into the workspace. |
| `assignment_mode` | enum | no | `"auto"` (default) or `"explicit"` — `explicit` blocks agent assignment until an admin configures an instance. |
| `exclude_from` | array | no | Agent kinds that should not be allowed to use this MCP (e.g. `["voice"]`). |
| `costs` | object | no | Per-tool cost rules — see "Cost reporting" below. |
| `patched` | boolean | no | `true` if you ship patches against the upstream package. |
| `patch_note` | string | no | One line summary of what the patch does. |
| `deprecated` | boolean | no | If `true`, the catalog shows a deprecation banner. |
| `requires_system_packages` | array | no | OS packages required (apt/dnf names). Surfaces a warning to admins. |
| `platform_min_version` | semver | no | Minimum OtoDock version that supports this MCP. |

### `server` object

| Field | Required | Purpose |
|-------|----------|---------|
| `runtime` | yes | `"python"`, `"node"`, or `"docker"`. |
| `transport` | yes | `"stdio"` for python/node MCPs, `"http"` for Docker MCPs (dual `/sse` and `/mcp`). |
| `command` | conditional | Required for stdio. The binary or script to launch. |
| `args` | conditional | Optional args list (supports `${mcp_dir}` token). |
| `source` | yes | Install source — `npm:<pkg>@<version>`, `pypi:<pkg>@<version>`, or `docker:<image>`. |
| `docker_compose` | conditional | For Docker MCPs — compose file name. |
| `port` | conditional | For Docker MCPs — the HTTP port. |
| `health_endpoint` | conditional | For Docker MCPs — `/mcp` or `/sse`. |
| `url_template` | conditional | For Docker MCPs — `http://localhost:${port}`. |

### `instances` object (admin-managed credentials/config)

```json
"instances": {
  "delivery": "env" | "config_file",
  "fields": [
    {"key": "MY_VAR", "label": "Friendly Label", "input_type": "text|password|url|email|number|ssh_key_select",
     "default": "", "required": true, "secret": false}
  ],
  "max_instances": 0,
  "config_file_arg": "--config-file",
  "config_file_name": "hosts.json",
  "transform": "ssh_hosts"
}
```

- `delivery: env` injects fields as env vars on the MCP process.
- `delivery: config_file` writes a JSON config file the MCP reads (used by e.g. ssh-server).
- `max_instances: 0` means "unlimited".

## Path conventions — shared contract

These rules keep MCPs working across local-sandboxed (bwrap) and remote-satellite execution targets, and across viewer/manager/admin scopes.

### Tool-arg paths

Tool arguments that accept file paths (`attachments[].path`, `save_path`, `local_file`) MUST accept absolute sandbox paths under `/users/{u}/`, `/workspace/`, or `/config/`. These are what the agent has visibility into.

You MAY additionally accept relative paths interpreted against `OTO_WORKSPACE_DIR`, but absolute sandbox paths are the required minimum.

### `OTO_*` env vars (auto-injected, read-only)

Every stdio MCP launch receives:

| Env var | Value |
|---|---|
| `OTO_AGENT_NAME` | agent slug |
| `OTO_USERNAME` | session username (empty for agent-scoped) |
| `OTO_SCOPE` | `"user"` / `"agent"` |
| `OTO_ROLE` | `"viewer"` / `"manager"` / `"admin"` / `""` |
| `OTO_SESSION_ID` | session id |
| `OTO_WORKSPACE_DIR` | sandbox-style workspace path |
| `OTO_USER_ROOT` | sandbox-style user dir (empty for agent-scoped) |
| `OTO_CONFIG_DIR` | sandbox-style config dir (empty for non-manager+) |
| `OTO_SHARED_WORKSPACE` | sandbox-style shared workspace (empty for viewer) |
| `OTO_ALLOWED_ROOTS` | `:`-joined accessible mount roots |

Read these for scope-aware paths instead of inventing your own conventions. Empty values mean "this scope has no value for this concept" — degrade gracefully.

The `OTO_` namespace is **reserved**. Community MCPs MUST NOT define env vars starting with `OTO_`.

### Path allowlists

If your MCP validates filesystem access against an allowlist, declare it via `path_env` multi-value:

```json
"path_env": {
  "YOUR_ALLOWLIST_ENV": {
    "values": [
      {"role": "user_root"},
      {"role": "shared_workspace"},
      {"role": "config"}
    ],
    "join": ":"
  }
}
```

This produces a joined list that exactly mirrors the bwrap mount set:
- viewer → `/users/{u}`
- manager/admin user-scoped → `/users/{u}:/workspace:/config`
- agent-scoped task → `/workspace`

DO NOT bake in `Path.home()`-style defaults — bwrap's `HOME=/tmp` doesn't reflect where the agent's files actually live, and you'll silently reject every legitimate path.

### Output dirs

For MCPs that write files (image generators, document creators, etc.), pick one:

- **Read `OTO_WORKSPACE_DIR` directly** (preferred for new MCPs you author).
- **Or declare `path_env` with `role: workspace`** and a custom env var name (use this when wrapping a third-party package that already reads a specific env var name internally — e.g. workspace-mcp reads `WORKSPACE_MCP_CREDENTIALS_DIR`; the manifest declares the bridge).

Anchor relative paths against the workspace. Reject or re-anchor absolute paths that escape the workspace.

### Credentials (OAuth tokens, API keys, etc.)

Declare `path_env` with `role: credentials_dir` + `subpath` for any MCP that needs a place to read/write per-user credential files:

```json
"path_env": {
  "MY_TOKEN_DIR": {"role": "credentials_dir", "subpath": "my-service-tokens"}
}
```

The platform copies tokens from the central OAuth store into the per-session dir on session start, and writes back any refreshed tokens on session close. NEVER read OAuth tokens from arbitrary host paths.

### Screenshot / temp output dirs

For MCPs that produce per-session ephemera, declare `path_env` with `role: screenshots_session`:

```json
"path_env": {
  "MY_TEMP_OUTPUT": {"role": "screenshots_session"}
}
```

This produces `<workspace>/.screenshots/{session_id}/` and is auto-cleaned at session close.

## Cost reporting contract

MCPs that hit billable APIs (image generation, premium SaaS endpoints, paid LLM proxies) declare per-tool pricing in the manifest. The platform evaluates the rules at `TOOL_RESULT` time and:

1. Adds the cost to the chat/task total visible to the user.
2. Writes one `usage_records` row per (provider, model) so admins see the breakdown.
3. Forwards an `mcp_cost` WS event so the dashboard total updates live.

**MCP authors do nothing in code.** No proxy hook to call, no cost computation in the server, no auth — pricing is data, not code.

```json
"costs": {
  "currency": "USD",
  "provider": "your-mcp-tag",
  "rules": [
    {"tool": "expensive_call", "match": {"tier": "premium"}, "amount": 0.05},
    {"tool": "expensive_call", "amount": 0.01},
    {"tool": "free_call", "amount": 0}
  ]
}
```

Author rules:

1. **Order specific rules first, catch-all last.** Matching is against EXPLICIT args the LLM passed — JSON-schema defaults are NOT substituted. The catch-all (empty `match`) handles the default-args case.
2. **`provider` is one stable tag per MCP.** Choose something users will recognize in the admin breakdown (`"image-gen"`, `"google-maps"`, `"twilio-sms"`).
3. **Use `multiply_by` for batch tools** that bill per-item (`"multiply_by": "num_images"`). Missing/garbage values default to multiplier 1.
4. **`amount` is the raw upstream price**, not your markup.
5. **Don't ship `0` rules just to "report" a free tool.** Omit the `costs` block entirely if the MCP is free.
6. **Update `version` in the manifest when prices change.** Existing `usage_records` rows are immutable; the update applies to future calls only.

Malformed `costs` blocks are rejected at install time with a precise error.

## Environment contract

Community MCPs **MUST NOT** ship `.env` files. The install pipeline rejects archives containing `.env` at any depth (only `.env.example` is permitted; the framework never reads it).

The platform delivers environment values through four declared layers:

| Field | Where it lives | When evaluated | Use for |
|---|---|---|---|
| `env` | `manifest.json` top-level | Every Docker MCP `.env` write OR every stdio session start | Static / platform-derived values. |
| `agent_env` | `manifest.json` top-level | Stdio session start (resolved into the MCP's subprocess env) | Per-session values. Use `${session.*}` tokens. |
| `instances.fields` | DB (`mcp_instances.field_values_enc`, Fernet-encrypted) | Session start, decrypted just before use | Sensitive credentials and per-instance config. |
| `path_env` | `manifest.json` top-level | Session start (resolved per-role per-scope) | Workspace-relative paths. |

**Auto-injected, do NOT declare** in any of the above:
- `PROXY_URL` — proxy callback URL
- `PROXY_API_KEY` — session-scoped JWT (NOT the master key)
- `OTO_*` set including `OTO_SESSION_ID`, `OTO_AGENT_NAME`, `OTO_USERNAME`, `OTO_SCOPE`, `OTO_ROLE`, `OTO_WORKSPACE_DIR`, `OTO_USER_ROOT`, `OTO_CONFIG_DIR`, `OTO_SHARED_WORKSPACE`, `OTO_ALLOWED_ROOTS`

**Token namespaces** for `${...}` references inside manifest values:

- `${platform_root}`, `${proxy_url}`, `${agent_name}`, `${mcp_dir}`, `${port}` — manifest-static.
- `${platform.api_key}`, `${platform.proxy_url_for_docker}`, `${platform.wopi_base_url}`, `${platform.collabora_frame_ancestors}`, `${platform.collabora_service_root}`, `${platform.host_agents_dir}`, `${platform.mcp_port}`, `${platform.oauth_insecure_transport}` — platform config (mainly for Docker MCP `env` blocks).
- `${session.task_owner}`, `${session.task_username}`, `${session.task_scope}`, `${session.chat_id}`, `${session.voice_server_url}` — per-session context (mainly for stdio MCP `agent_env` blocks; resolves to `""` outside a real session).
- `${config:mcp_name:key}` — read a config value from another MCP's DB config.

`${proxy_api_key}` (with underscore) is NOT a valid token and is blocked at resolution time. Use `${platform.api_key}` only in Docker MCP `env` blocks where the container needs the master key (containers serve many sessions, can't use session-scoped JWTs); stdio MCPs never declare `PROXY_API_KEY` because they get the session-scoped JWT auto-injected.

## PR review checklist

Maintainers reviewing community MCP PRs verify:

- [ ] All path-bearing env vars declared via `path_env` (no hardcoded defaults).
- [ ] Tool-arg paths accept absolute sandbox paths.
- [ ] No `OTO_*` env vars defined by the MCP.
- [ ] No reads from `~/.ssh/`, `~/.aws/`, `~/.config/gcloud`, etc. without an explicit user-supplied path.
- [ ] If the MCP has an allowlist, it's wired via `path_env` multi-value.
- [ ] README documents tool-arg path expectations (relative vs absolute).
- [ ] **No `.env` file in the archive** (rejected at install time).
- [ ] All env vars the source code reads are declared in `manifest.json` under `env`, `agent_env`, `path_env`, or `instances` — none undeclared.
- [ ] Source reads `OTO_SESSION_ID`, NOT `CLAUDE_SESSION_ID`.
- [ ] If the MCP hits a billable upstream API, `costs` block is declared. MCP source code MUST NOT POST cost to the proxy directly — pricing lives in the manifest only.
- [ ] `registry.json` regenerated (`python scripts/generate-registry.py`) and committed.

## README template (per MCP)

```markdown
# <Label>

<One-paragraph description.>

| Field | Value |
|-------|-------|
| Manifest name | `<name>` |
| Runtime | `<python|node|docker>` (stdio / http) |
| Upstream | `<source>` |
| Credentials | <`None` | `Per-user (...)` | `Per-instance (...)`> |
| Per-tool cost | <`None` | `Yes, see manifest.costs`> |
| Assignment mode | <`auto` | `explicit`> |

## What it does

<2-4 sentences. Concrete examples of tool use cases.>

## Install layout

<List of files in this folder + what each is for.>

## Operator notes

<Anything an operator needs to know on day 1: how to get an API key, what
firewall rules to open, common gotchas, links to upstream docs.>
```

## Versioning

This repo follows semver tags (`v0.1.0`, `v0.2.0`, …). OtoDock platform releases pin a specific tag in their `VERSIONS.md`; the platform Browse UI reads `registry.json` from that tag. Individual MCP versions inside the registry are independent — `google-maps@1.2.0` lives at one tagged commit; the next platform release might bring `google-maps@1.2.1` via a registry-only bump.
