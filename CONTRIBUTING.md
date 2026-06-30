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

Build artifacts and **anything that could carry a secret**: `*/node_modules/`, `*/venv/`, `*/.venv/`, `*/__pycache__/`, `*/keys/`, `*/config/`, `*/screenshots/`, `*.log`, and the secret-file patterns `.env`, `*/.env`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`. **Never commit a real secret, key, or credential.** Community MCPs MUST NOT ship a `.env` file at all (see [Environment contract](#environment-contract)).

## `manifest.json` reference

### Top-level fields

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `name` | string | yes | **Canonical slug** — the key the platform looks everything up by (assignments, config, tool namespacing). Lowercase + hyphens. Convention: match the folder name (the platform keys off `name`, not the folder, but keeping them equal avoids confusion). |
| `server_name` | string | no | Override for the `mcpServers` config key / tool namespace prefix. Defaults to `name`. Use only when the tool namespace must differ from the slug. |
| `label` | string | yes | Display name shown to operators. |
| `description` | string | yes | One-line description for catalog cards. |
| `version` | string | yes | **node/python:** leave empty (`""`) — these are unpinned and install the latest published version (the platform records the resolved version into each install's local manifest). **docker/git+:** semver, bumped on any user-visible change. |
| `category` | string | yes | Always `"community"` here. (`core` / `custom` are platform-bundled tiers.) Community MCPs install **disabled** until an admin enables them platform-wide, then opt-in per agent. |
| `server` | object | yes | Runtime + transport config (see [`server` object](#server-object)). |
| `credentials` | object | no | How the MCP authenticates — `type: "none" \| "per_user" \| "infra"`, plus optional `oauth`, `service_account`, `webhooks`. See [Credentials](#credentials). |
| `instances` | object | no | Admin-managed instances (URL + token, multi-host configs). See [Instances](#instances-object-admin-managed-credentialsconfig). |
| `config` | array | no | Admin-managed key/value config exposed in the dashboard. `user_overridable: true` lets each user override a value. |
| `env` | object | no | Static env vars set on every launch (Docker `.env` write, or every stdio session start). |
| `agent_env` | object | no | Per-session env vars resolved at session start. Use `${session.*}` tokens. |
| `path_env` | object | no | Workspace-relative paths declared by role — see [Path conventions](#path-conventions--shared-contract). |
| `skills` | array | no | Markdown skill files auto-loaded into agent prompts. See [Skills](#skills). |
| `agent_context` | array | no | Per-session prompt blocks with `${...}` substitution / out-of-band lookups. See [Dynamic context](#dynamic-context-agent_context). |
| `outputs` | array | no | Files the MCP writes that the platform should ferry into the workspace (screenshots, renders). See [Output relocation](#output-relocation-outputs). |
| `costs` | object | no | Per-tool cost rules — see [Cost reporting](#cost-reporting-contract). |
| `assignment_mode` | enum | no | `"auto"` (default) or `"explicit"` — `explicit` blocks agent assignment until an admin configures an instance (used with `instances`). |
| `exclude_from` | array | no | Session contexts that should NOT load this MCP. Valid values: `"phone"`, `"task"`, `"terminal"`. |
| `tool_filter` | object | no | `{ "arg_name": "--enabled-tools", "env_var_name"?: "..." }` — advertises a runtime tool-restriction CLI flag the admin can drive with a regex. **Docker MCPs only** (no-op on stdio today). |
| `network_targets` | array | no | Internal-LAN hosts this MCP dials (homelab MCPs). Each: `{source, host_key, port_key?, port_default?}`. The agent sandbox is network-isolated; egress to these hosts is carved only when the admin enables the MCP's `_network_access` toggle. See [Internal-network MCPs](#internal-network-mcps). |
| `network_access_default` | bool | no | Default state of the `_network_access` admin toggle (default `true`). |
| `sandbox` | object | no | `{ "mounts": [{host, sandbox, mode}] }` — extra bind-mounts into the bwrap sandbox. `host` honours only the `${mcp_dir}` token; both ends are allowlisted. |
| `placement` | enum | no | `"any"` (default) or `"satellite_only"`. |
| `requires_display` | bool | no | If `true`, the MCP is excluded on satellites known to be headless. |
| `device_capability` | enum | no | `"computer"` / `"browser"` / `"app"` for device-control MCPs (implies remote-only + per-machine grant). See [Device-control & app-connector MCPs](#device-control--app-connector-mcps). |
| `device_high_risk_tools` | array | no | Tool names that still prompt the user even after the device capability is granted. |
| `companion_app` | object | no | App-connector block for MCPs that bridge to a desktop app. |
| `system_requirements` | object | no | OS packages/runtimes the installer **enforces** at preflight. See [System requirements](#system-requirements). |
| `requires_system_packages` | array | no | OS package names shown to admins as a **display-only** warning (the installer does not act on these — use `system_requirements` for enforcement). |
| `patched` | bool | no | `true` if you ship patches against the upstream package. |
| `patch_note` | string | no | One-line summary of what the patch does. |
| `deprecated` | bool | no | If `true`, the catalog shows a deprecation banner. |
| `platform_min_version` | semver | no | Minimum OtoDock version that supports this MCP (catalog metadata). |

> Some manifest fields are platform-internal and **not** for community MCPs: `requires_capability` (gates on a platform feature like audio/phone), `hosted` (OtoDock-managed relay config), and `server.proxy_callbacks` (proxy-callback auth — only the bundled file-tools MCP uses it). They're listed here only so you recognise them; a community MCP that integrates an external service never sets them.

### `server` object

| Field | Required | Purpose |
|-------|----------|---------|
| `runtime` | yes | `"python"`, `"node"`, or `"docker"`. |
| `transport` | yes | `"stdio"` for python/node MCPs; `"http"` for Docker MCPs (dual SSE + **streamable HTTP**, served at `/mcp/`). |
| `command` | conditional | Required for stdio. The binary or script to launch (relative to the MCP folder, or a bare command like `node`). |
| `args` | conditional | Optional args list (supports the `${mcp_dir}` token). |
| `source` | yes | Install source. **node/python are UNPINNED** — `npm:<pkg>` / `pypi:<pkg>` (no `@version`; installs the latest published version, and the platform pins the resolved version into the install's local manifest). Pinned forms: `docker:<image>` and `git+<url>@<ref>#subdirectory=<dir>`. Only these four prefixes are accepted. |
| `version_constraint` | no | (node/python only) Optional **auto-update bound** — a PEP 440 specifier set, e.g. `">=2,<3"` (NOT npm `^`/`~`/`x` ranges). Empty/absent ⇒ unbounded (tracks the absolute latest). When set, an upstream **major** can't auto-apply until you widen it. See [Versioning](#versioning). |
| `port` | conditional | For Docker MCPs — the HTTP port. |
| `url_template` | conditional | For Docker MCPs — **MUST be `http://${docker_mcp_host}:${port}`** (never hardcode `localhost`, which breaks the containerised deployment). The framework appends `/mcp/`. |
| `health_endpoint` | no | For Docker MCPs — an HTTP health-check path (e.g. `/health`). |
| `docker_compose` | conditional | For Docker MCPs — the compose file name. |
| `service_name` | no | For Docker MCPs — the service-DNS name the containerised proxy dials; defaults to `name`. |
| `image` | conditional | For Docker MCPs — a pre-built image reference (e.g. a GHCR image). **Required for the containerised deployment**, which cannot `docker build`; absent ⇒ build-from-context (bare-metal only). |

### Credentials

Pick the `credentials.type` that matches how your MCP authenticates:

- **`"none"`** — no per-user secret. Stateless tools (browser, file converters). If the MCP needs admin-supplied infra config/keys, pair `type: "none"` with an [`instances`](#instances-object-admin-managed-credentialsconfig) block — the dominant pattern for community infra MCPs (Prometheus, Home Assistant, Google Maps).
- **`"per_user"`** — username/password-style credentials each user enters in their Settings (Nextcloud, email). Add a `fields` array describing the inputs. Stored Fernet-encrypted in `user_credentials`; multi-account per user is automatic.
- **`"infra"`** — shared admin-level credentials (configured once in Admin → MCP Servers).

**OAuth MCPs** (Slack, Notion, Linear, GitHub, …) declare a `credentials.oauth` block. The framework is manifest-driven — most providers are manifest-only; quirky ones add a small Python provider subclass on the platform side. Core fields:

```jsonc
"credentials": {
  "type": "per_user",
  "oauth": {
    "provider_id": "yourservice",                 // unique; names the token store, shared across MCPs of the same provider
    "flows": ["authorization_code"],              // or _pkce / device_code / client_credentials / personal_access_token
    "authorization_url": "https://.../authorize",
    "token_url": "https://.../token",
    "userinfo_url": "https://.../me",             // optional; userinfo_email_field/_name_field/_id_field map the response (dotted paths ok)
    "app_credential": "yourservice-oauth-app",    // infra-credential bundle holding CLIENT_ID/CLIENT_SECRET
    "app_credential_fields": [ {"key": "CLIENT_ID", "label": "Client ID", "input_type": "text"},
                               {"key": "CLIENT_SECRET", "label": "Client Secret", "input_type": "password"} ],
    "services": [ {"key": "read", "label": "Read", "description": "...", "scopes": ["read"]} ],
    "token_format": {"schema": "generic_oauth_v1", "filename_pattern": "{account_label}.json"},
    "refresh": {"strategy": "lazy", "min_remaining_seconds": 300},
    "bearer_required": false                       // remote bearer MCPs: true + proposed_hosts:[...]
  }
}
```

- **stdio OAuth MCPs** also declare a `path_env` entry with `role: "credentials_dir"` (below) — the framework copies the bound account's token file in at session start and writes refreshed tokens back at close.
- **remote bearer MCPs** (`server.transport: "http"`, hosted by the vendor) set `bearer_required: true` + `proposed_hosts: ["mcp.vendor.com"]`; the framework injects `Authorization: Bearer <token>` — no file copy.
- **`credentials.service_account: true`** (a sibling of `oauth`, not nested) lets a manager bind one of their own connected accounts as an agent's service identity, so the MCP works in agent-scope sessions (phone/task/trigger). There is no platform service-account tier.
- **`credentials.webhooks`** declares an inbound webhook receiver (signature scheme, subscription mode, event catalog) wired automatically at install — use when the vendor pushes events you want to drive triggers.

The block above is the common case. OAuth also supports the `authorization_code_pkce`, `device_code`, `client_credentials`, and `personal_access_token` flows, plus per-vendor token-shape quirks handled by an optional platform-side provider subclass. If your provider needs a non-standard flow or token shape, describe it in your PR and a maintainer will wire the subclass.

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

- `delivery: "env"` injects the first agent-matching instance's fields as env vars on the MCP process.
- `delivery: "config_file"` writes a JSON config file (all agent-matching instances) the MCP reads via `config_file_arg` (used by e.g. ssh-server).
- `max_instances: 0` means "unlimited".
- `instances` is the standard home for community-MCP **secrets** (API keys, server URLs) — they're stored Fernet-encrypted, never on disk, since `.env` files are banned.

## Path conventions — shared contract

These rules keep MCPs working across local-sandboxed (bwrap) and remote-satellite execution targets, and across viewer/editor/manager/admin scopes.

### Tool-arg paths (`tool_arg_paths`)

Tool arguments that accept an **LLM-supplied file path** (`save_path`, `attachments[].path`, `images[*].source`) MUST be declared in `tool_arg_paths` so the framework translates + **policy-gates** each path before your MCP sees it:

```json
"tool_arg_paths": {
  "display_images": { "images[*].source": {"mode": "read"} },
  "save_image":     { "dest_path": {"mode": "write"} }
}
```

Per-arg fields: `mode` (`"read"` default; `"write"` triggers push-back for Docker MCPs), `optional` (default `false`), `relative_anchor` (default `OTO_WORKSPACE_DIR`). The JSONPath subset supports `name`, `a.b`, `name[*]`, `a[*].b`.

**Do NOT re-gate the path locally.** By the time your MCP receives the argument it has already been validated against the session's `allow_full_fs` / home-dir / agent-tree policy. A second local allowlist check (e.g. an `OTO_ALLOWED_ROOTS` `startswith`) is redundant AND breaks cross-platform (Windows flips `/`→`\`; the framework legitimately admits Desktop/home paths your allowlist wouldn't). Just `os.path.isfile(path)` and proceed. (Re-anchoring an out-of-workspace *write* into your own subfolder for UX is fine — that's not a policy check.)

### `OTO_*` env vars (auto-injected, read-only)

Every **stdio** MCP launch receives these with zero manifest declaration. (Docker MCPs don't get them — they resolve paths via the `/v1/hooks/resolve-path` callback instead.)

| Env var | Value |
|---|---|
| `OTO_AGENT_NAME` | agent slug |
| `OTO_USERNAME` | session username (empty for agent-scoped) |
| `OTO_USER_SUB` | session owner's stable id (empty for agent-scoped) — use this instead of decoding any JWT |
| `OTO_SCOPE` | `"user"` / `"agent"` |
| `OTO_ROLE` | `"viewer"` / `"editor"` / `"manager"` / `"admin"` / `""` |
| `OTO_SESSION_ID` | session id |
| `OTO_WORKSPACE_DIR` | sandbox-style workspace path |
| `OTO_USER_ROOT` | sandbox-style user dir (empty for agent-scoped) |
| `OTO_CONFIG_DIR` | sandbox-style config dir (empty below manager) |
| `OTO_SHARED_WORKSPACE` | sandbox-style shared workspace (empty for viewer) |
| `OTO_KNOWLEDGE_DIR` | sandbox-style knowledge dir (universal) |
| `OTO_ALLOWED_ROOTS` | `:`-joined accessible mount roots |

(The platform also injects `OTO_DEFAULT_SCOPE`, `OTO_TASK_TYPE`, and the memory toggles — the whole `OTO_` namespace is reserved and auto-injected.) Read these for scope-aware paths instead of inventing your own conventions. Empty values mean "this scope has no value for this concept" — degrade gracefully. **Community MCPs MUST NOT define env vars starting with `OTO_`.**

### Path allowlists (`path_env`)

If your MCP validates filesystem access against an allowlist env var, declare it via `path_env` multi-value so it resolves per-role per-scope to exactly the bwrap mount set:

```json
"path_env": {
  "YOUR_ALLOWLIST_ENV": {
    "values": [ {"role": "user_root"}, {"role": "shared_workspace"}, {"role": "config"} ],
    "join": ":"
  }
}
```

The valid `path_env` roles (the only ones) are:

| Role | Resolves to | Notes |
|---|---|---|
| `workspace` | `/users/{u}/workspace` (user) · `/workspace` (agent) | Always present. Optional `subpath`. |
| `user_root` | `/users/{u}` | Empty (dropped) in agent scope. Optional `subpath`. |
| `shared_workspace` | `/workspace` | Empty for **viewer**; present for editor/manager/admin. Optional `subpath`. |
| `config` | `/config` | **manager/admin only**; empty otherwise. (`subpath` ignored.) |
| `knowledge_dir` | `/knowledge` | Every role. Optional `subpath`. |
| `credentials_dir` | `/users/{u}/{subpath}` (user) · `/workspace/{subpath}` (agent) | **`subpath` REQUIRED.** For OAuth token dirs. |

So the multi-value example above resolves to: viewer → `/users/{u}`, editor → `/users/{u}:/workspace`, manager/admin → `/users/{u}:/workspace:/config`, agent-scoped → `/workspace`. Empty resolutions are dropped before the join.

DO NOT bake in `Path.home()`-style defaults — the sandbox `HOME` doesn't reflect where the agent's files live, and you'll silently reject every legitimate path.

### Output dirs (files your MCP creates)

Prefer **reading `OTO_WORKSPACE_DIR` directly** for new MCPs. When wrapping a third-party package that reads a specific env var name internally, **declare `path_env` with `role: "workspace"`** + that env var name (e.g. workspace-mcp's `WORKSPACE_MCP_CREDENTIALS_DIR`). Anchor relative paths against the workspace; re-anchor or reject absolute paths that escape it.

### Credentials dirs (OAuth tokens)

Declare `path_env` with `role: "credentials_dir"` + `subpath` for any MCP that reads/writes per-user credential files:

```json
"path_env": { "MY_TOKEN_DIR": {"role": "credentials_dir", "subpath": "my-service-tokens"} }
```

The platform copies tokens from the central OAuth store into the per-session dir on session start, and writes back any refreshed tokens on session close. NEVER read OAuth tokens from arbitrary host paths.

## Output relocation (`outputs`)

For MCPs that write files to a fixed directory they can't configure per-session (screenshots, renders), declare an `outputs` rule. After each matching tool call the platform moves the produced file(s) into a **flat, hidden** `<workspace>/.screenshots/` dir (kept out of platform↔satellite file-sync) and trims to `keep_recent`:

```json
"outputs": [
  {
    "source": "${mcp_dir}/screenshots",
    "destination_template": "${workspace_dir}/.screenshots",
    "after_tools": ["*"],
    "keep_recent": 15
  }
]
```

- `source` — where the MCP writes. Template vars: `${mcp_dir}`, `${workspace_dir}`, `${session_id}`.
- `destination_template` — flat target dir (files written straight in, no per-session subdir). Keep it `.`-prefixed so file-sync ignores it.
- `after_tools` — tool names that trigger relocation; `["*"]` = every tool from this MCP.
- `keep_recent` — keep only the N newest files (`0`/absent = keep all).

(This is the correct mechanism for per-session output dirs — there is no `screenshots_session` path role.)

## Skills

Markdown how-to files loaded into the agent's system prompt when your MCP is assigned. Declare them in the manifest and put the files under `skills/`:

```json
"skills": [
  { "id": "yourmcp-usage", "file": "skills/usage.md",
    "description": "When and how to use these tools",
    "default_exclude_from": ["phone"] }
]
```

Write skills as agent-facing instructions (when to use each tool, gotchas, examples). Keep them accurate and concise. **Do not mention platform-internal auth** (the proxy session key is service-to-service and never relevant to the agent).

## Dynamic context (`agent_context`)

Optional per-session prompt blocks injected into the agent's `# MCP Dynamic Context` section. Pure data — no code:

```jsonc
"agent_context": [
  {
    "template": "## Account\nUse user_email=\"${account.email}\" on every call.",
    "requires": ["account.email"],     // block is skipped silently if any required token is empty
    "scope": ["user"]                  // ["user"] | ["agent"] | [] = both
  }
]
```

A block may also run a one-shot lookup via a `builder` (calls an HTTP-class MCP tool out-of-band, exposes the result as `${result.*}`) — used e.g. to greet an inbound caller by name. Token namespaces include `${account.*}`, `${agent.*}`, `${user.*}`, `${session.*}`, `${trigger.*}`. Unknown tokens render empty; unknown block keys fail loudly at manifest load.

## Cost reporting contract

MCPs that hit billable APIs (image generation, premium SaaS endpoints) declare per-tool pricing in the manifest. The platform evaluates the rules at `TOOL_RESULT` time and (1) adds the cost to the user-visible total, (2) writes one `usage_records` row per (provider, model), (3) forwards an `mcp_cost` WS event so the dashboard updates live.

**MCP authors do nothing in code.** No proxy hook to call, no cost computation, no auth — pricing is data, not code.

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

1. **`currency` must be `"USD"`** (others are rejected).
2. **Order specific rules first, catch-all last.** Matching is against EXPLICIT args the LLM passed — JSON-schema defaults are NOT substituted. The empty-`match` catch-all handles the default-args case.
3. **`match` values:** a scalar matches by equality; a **list matches by membership** (collapse several arg values into one tier). All keys in `match` must be present in the call's explicit args.
4. **`provider` is one stable tag per MCP** — something users recognise in the admin breakdown (`"image-gen"`, `"google-maps"`).
5. **Use `multiply_by` for batch tools** that bill per-item (`"multiply_by": "num_images"`). Missing/garbage values default to multiplier 1.
6. **`amount` is the raw upstream price**, not your markup.
7. **Don't ship `0` rules just to "report" a free tool.** Omit the `costs` block entirely if the MCP is free.
8. **When prices change:** bump `version` for docker/git+ MCPs; for unpinned node/python MCPs (empty `version`) the change is picked up as a catalog integration update automatically. Existing `usage_records` rows are immutable; updates apply to future calls only.

Malformed `costs` blocks are rejected at install time with a precise error.

## System requirements

If your MCP needs OS packages or a minimum runtime, declare `system_requirements` — the installer **enforces** this at preflight (before pip/npm) and fails with a clear message if unmet:

```json
"system_requirements": {
  "debian": ["libfoo-dev"], "ubuntu": ["libfoo-dev"], "rhel": ["foo-devel"],
  "arch": ["foo"], "macos_brew": ["foo"], "node_min": "20", "notes": "..."
}
```

There is **no `python_min`** — uv reads the upstream package's `requires-python` and fetches the right interpreter automatically. (`requires_system_packages` is a separate **display-only** field that just warns admins; it is not enforced — use `system_requirements` for anything functional.)

## Internal-network MCPs

MCPs that reach a host on the operator's LAN (Prometheus, Home Assistant, a NAS) declare `network_targets`. The agent sandbox is **always network-isolated** (RFC1918 + the host's own subnet are blackholed); when the admin turns on the MCP's `_network_access` toggle, the framework carves egress to **exactly** the resolved targets — nothing else. Each entry is `{source, host_key, port_key?, port_default?}`, where `source` says where the live host/port value comes from (`config` / `instance` / a credential), `host_key` names the field holding the host/IP or URL, and the port falls back to `port_key` then `port_default`.

## Device-control & app-connector MCPs

MCPs that drive a real machine (screen/mouse, a logged-in browser, a desktop app) set `device_capability` (`"computer"` / `"browser"` / `"app"`). These are automatically **remote-only** and gated behind a per-machine grant the owner approves — the capability is the security boundary, so they use `assignment_mode: "auto"`. List any genuinely dangerous tools in `device_high_risk_tools` so they still prompt even after the grant. App-connector MCPs that bridge to a desktop program add a `companion_app` block. Treat all page/app content as untrusted input in your skill guidance.

## Environment contract

Community MCPs **MUST NOT** ship `.env` files. The install pipeline rejects any archive containing `.env` at any depth (only `.env.example` is permitted; the framework never reads it). The platform delivers environment values through four declared layers:

| Field | Where it lives | When evaluated | Use for |
|---|---|---|---|
| `env` | `manifest.json` top-level | Every Docker MCP `.env` write OR every stdio session start | Static / platform-derived values. |
| `agent_env` | `manifest.json` top-level | Stdio session start (resolved into the MCP's subprocess env) | Per-session values. Use `${session.*}` tokens. |
| `instances.fields` | DB (`mcp_instances.field_values_enc`, Fernet-encrypted) | Session start, decrypted just before use | Sensitive credentials and per-instance config. |
| `path_env` | `manifest.json` top-level | Session start (resolved per-role per-scope) | Workspace-relative paths. |

**Auto-injected, do NOT declare:**
- `PROXY_URL` — proxy callback URL.
- `PROXY_API_KEY` — a **session-scoped JWT** (NOT the master key), auto-injected into every stdio MCP. Use it as-is if your MCP calls the proxy; never declare it.
- The `OTO_*` set (see above).

**The master `PROXY_API_KEY` is never given to any MCP, and you cannot template it in.** Both `${platform.api_key}` and `${proxy_api_key}` are **rejected at resolution time** — a manifest can never pull the master key into a config file or container. (A Docker MCP that needs to call the proxy back uses `server.proxy_callbacks`, which injects a per-session JWT bearer — but that's a platform/core concern; community MCPs integrate external services and don't call the proxy.)

**Token namespaces** for `${...}` references inside manifest values:

- `${platform_root}`, `${proxy_url}`, `${agent_name}`, `${mcp_dir}`, `${port}` — manifest-static.
- `${platform.proxy_url_for_docker}`, `${platform.wopi_base_url}`, `${platform.collabora_frame_ancestors}`, `${platform.collabora_service_root}`, `${platform.host_agents_dir}`, `${platform.mcp_port}`, `${platform.oauth_insecure_transport}` — platform config (mainly for Docker MCP `env` blocks).
- `${session.task_owner}`, `${session.task_username}`, `${session.task_scope}`, `${session.chat_id}` — per-session context (for stdio `agent_env`; resolves to `""` outside a real session).
- `${config:mcp_name:key}` — read a config value from another MCP's DB config.
- Inside an `outputs[]` block: `${workspace_dir}`, `${session_id}`.

## PR review checklist

Maintainers reviewing community MCP PRs verify:

- [ ] All path-bearing env vars declared via `path_env` (no hardcoded defaults).
- [ ] LLM-supplied tool-arg paths declared via `tool_arg_paths`; the MCP does NOT re-gate them locally.
- [ ] No `OTO_*` env vars defined by the MCP.
- [ ] No reads from `~/.ssh/`, `~/.aws/`, `~/.config/gcloud`, etc. without an explicit user-supplied path.
- [ ] If the MCP has an allowlist, it's wired via `path_env` multi-value.
- [ ] README documents tool-arg path expectations (relative vs absolute).
- [ ] **No `.env` file in the archive** (rejected at install time).
- [ ] No manifest reference to the master key (`${platform.api_key}` / `${proxy_api_key}` are rejected).
- [ ] All env vars the source reads are declared in `manifest.json` (`env` / `agent_env` / `path_env` / `instances`) — none undeclared.
- [ ] Source reads `OTO_SESSION_ID`, NOT `CLAUDE_SESSION_ID`.
- [ ] Docker MCPs use `url_template: http://${docker_mcp_host}:${port}` and ship a pre-built `server.image`.
- [ ] If the MCP hits a billable upstream API, a `costs` block is declared (pricing in the manifest only — never POST cost from the server).
- [ ] OS deps that must be present are in `system_requirements` (enforced), not just `requires_system_packages` (display-only).
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
| Credentials | <`None` | `Per-user (...)` | `OAuth (...)` | `Per-instance (...)`> |
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

This repo follows semver tags (`v0.1.0`, `v0.2.0`, …). Each OtoDock platform release pins a specific tag and reads `registry.json` from it.

**node/python MCPs are unpinned.** Their `source` is a bare package pointer (`npm:<pkg>` / `pypi:<pkg>`) and `version` is `""`. The upstream registry (npm / PyPI) is the version of record: a fresh install pulls the latest, the platform records the resolved concrete version into the install's local manifest, and a weekly auto-update keeps installs current. Do **not** bump these in the catalog when upstream publishes, and do **not** commit `package.json` / `package-lock.json` for node MCPs — the platform generates `package.json` from `source` at install time, and a committed lockfile would pin a stale version and defeat "pull latest".

**docker and git+ MCPs stay pinned.** OtoDock owns the docker images (installs can't pull from upstream) and git+ MCPs pin a git ref, so their `source` carries the version/tag and `version` is the semver — bump these in the catalog on each new release.

**Bounding node/python auto-update (`version_constraint`).** By default node/python track the absolute latest. If an upstream package makes breaking changes across majors, set `server.version_constraint` (e.g. `">=2,<3"`) so auto-update stays within the validated range. To adopt a new major: update the manifest's integration fields (e.g. `args`, `oauth`) for the new version **and** widen the bound in the same change — installs pick up both on the next update. Any catalog manifest edit (args/oauth/skills/constraint) is detected as an "integration update" and re-applied to installs automatically, so you don't need to bump a version to push an integration fix.
