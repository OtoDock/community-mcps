# SSH

Remote server management via SSH, using [`@fangjunjie/ssh-mcp-server`](https://www.npmjs.com/package/@fangjunjie/ssh-mcp-server).

| Field | Value |
|-------|-------|
| Manifest name | `ssh-server` |
| Runtime | Node (stdio) |
| Upstream | `npm:@fangjunjie/ssh-mcp-server@1.6.0` |
| Credentials (per host) | `name`, `host`, `port`, `username`, `key_name` |
| Per-tool cost | None |
| Assignment mode | `explicit` (admin must configure at least one host) |
| Data delivery | `config_file` — instance fields are rendered into `config/hosts.json` |

## What it does

Provides shell access to remote hosts: `run_command`, `upload_file`, `download_file`, `list_directory`, etc. Each configured host is addressable by its `name`; the agent picks the right host based on the task.

## Configuration

The platform writes `config/hosts.json` from the configured instances in the admin UI. SSH private keys live in `keys/` (also platform-managed). Both directories are **runtime-only** and gitignored — never commit a real key.

## Install layout

- `manifest.json` — MCP descriptor.
- `package.json` — pins upstream npm version. `node_modules/` is not committed.
- `config/`, `keys/` — generated at runtime, not present in this repo.

## Operator notes

- Generate ed25519 keys in the platform UI (or paste existing ones); the platform stores them encrypted and writes the unencrypted file to `keys/` only for the duration of a session.
- Restrict each key on the target server with a `command="..."` directive in `~/.ssh/authorized_keys` if you want to lock the agent to a specific binary.
- Configure host fingerprints out of band — `StrictHostKeyChecking=accept-new` is fine for first-contact in a controlled environment but consider pre-seeding `known_hosts` for production.
