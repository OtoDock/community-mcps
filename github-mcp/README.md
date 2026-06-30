# github-mcp

GitHub repositories, issues, pull requests, actions, and code search via the
official [`github/github-mcp-server`](https://github.com/github/github-mcp-server)
Go binary, wrapped in a local Python sidecar so OtoDock can inject per-user
auth tokens.

## Why a sidecar?

`github-mcp-server` is stdio-only and reads its auth token from the
`GITHUB_PERSONAL_ACCESS_TOKEN` env var. OtoDock's framework injects per-user
auth via the inbound HTTP request's `Authorization: Bearer …` header, so we
need a small bridge:

```
chat session  ─HTTP+Bearer─►  sidecar.py  ─stdio+env─►  github-mcp-server
                              (per-session
                               subprocess)
```

The sidecar (~250 LOC, FastAPI + asyncio) listens on streamable-HTTP, mints
a `Mcp-Session-Id` for each new session, and spawns one stdio subprocess per
session with the captured bearer in env. Subprocesses are torn down when
the session closes or after 10 minutes idle.

## Setup (admin)

### Option A — OAuth (recommended for first-time users)

1. Create a GitHub OAuth App at <https://github.com/settings/developers> →
   **OAuth Apps** → **New OAuth App**.

2. **Authorization callback URL**: your OtoDock instance's OAuth callback:

       https://<your-otodock>/v1/oauth/github/callback

3. Copy the **Client ID** and **Client secret**.

4. In OtoDock admin → **MCP Servers** → install `github-mcp` → open
   detail page → **Credentials** → paste the Client ID and Secret.

### Option B — Personal Access Token (recommended for production)

Skip the OAuth App registration. Each user generates their own PAT.
Better long-term ergonomics — PATs don't expire mid-session like OAuth
access tokens do.

The user follows the connect flow → picks **Personal Access Token** in
the form → pastes the token they generated at
<https://github.com/settings/tokens?type=beta>.

## Connect (per user)

1. User settings → **Accounts** → click **Connect GitHub**.
2. Pick the auth method:
   - **OAuth** — browser consent flow, then granted services map to scopes
     (`repo`, `workflow`, `read:user`, `user:email`).
   - **Personal Access Token** — paste a token you generated yourself with
     the scopes for the services you want.
3. The platform persists the token under
   `sessions/github-tokens/user/<your-label>.json`.

## OAuth vs PAT — when to pick which

|                         | OAuth                               | PAT                          |
|-------------------------|-------------------------------------|------------------------------|
| First-time UX           | Browser consent dialog              | Paste a string               |
| Token lifetime          | ~8 hours, then refresh              | Up to 1 year (you set it)    |
| Mid-session expiry      | Possible — start new chat to refresh| Never (until manual rotate)  |
| Scope changes           | Re-consent in browser               | Regenerate in GitHub settings |
| Revocation              | GitHub UI or `auth.revoke`          | Delete in GitHub settings    |
| Admin needs OAuth App?  | Yes                                 | No                           |

**For self-hosted single-user installs**: pick PAT. Simpler, no expiry
surprises.

**For multi-user deployments**: pick OAuth. Per-user scope grants + better
audit trail.

## Architecture knobs

The Dockerfile pins github-mcp-server to a git ref (default: `main`). To
pin to a specific release tag for reproducibility:

```bash
docker build \
  --build-arg GITHUB_MCP_REF=v0.5.0 \
  -t otodock-github-mcp:v0.5.0 .
```

The sidecar's idle session TTL is configurable via env:

| Env var               | Default | Purpose                                      |
|-----------------------|---------|----------------------------------------------|
| `MCP_PORT`            | 8935    | Port the sidecar binds to inside the container |
| `GITHUB_MCP_BINARY`   | `/usr/local/bin/github-mcp-server` | Path to the Go binary     |
| `GITHUB_MCP_IDLE_TTL` | 600     | Seconds before idle session subprocess gets reaped |
| `GITHUB_HOST`         | (empty) | Override for GitHub Enterprise (e.g., `https://github.acme.io`) |
| `LOG_LEVEL`           | INFO    | Python logging level                         |

## What the agent sees

```
## GitHub Identity

You are acting on GitHub as **alice@example.com**. Your bearer token grants
whatever access this account has on GitHub. When calling `repo`-scoped
tools, default to repositories owned by this user unless the prompt
explicitly names a different org.
```

## Bash sandbox auth (`git` / `gh` in chat)

Connecting GitHub also auto-authenticates `git` and `gh` invocations in
the agent's bash sandbox — no separate `gh auth login` step needed. The
manifest declares `credentials.oauth.env_injection: ["GH_TOKEN", "GITHUB_TOKEN"]`,
so the framework sets both env vars on the bash subprocess with the
bound account's `access_token`. `git`'s `credential.helper` (HTTP URLs)
and `gh`'s CLI both consult these vars automatically.

The agent can `git clone`, `git push`, `gh pr create`, etc. against
any repo the bound account has access to, in any directory the sandbox
permits. `git`/`gh` are edit-tier in the bash permission gate — they
auto-approve in `acceptEdits` / `dontAsk` modes and prompt in `default`.

Same scope rules as the MCP itself: user-scope chats use the user's
bound GitHub account, agent-scope tasks / voice / triggers use the
bound service account.

## Limitations

- **OAuth refresh requires new chat session**: the sidecar captures the
  bearer at session-init and sets it as env on the stdio subprocess.
  Env can't be changed mid-process, so when an OAuth access token rotates,
  the in-flight session keeps the old token until the session closes or
  the user starts a fresh chat. Mitigation: prefer PATs in production, or
  accept the start-a-new-chat UX for OAuth tokens.
- **GitHub Enterprise**: set `GITHUB_HOST` in `docker-compose.yml` or
  override the manifest's `agent_env` block.
- **GitHub Apps (vs OAuth Apps)**: only OAuth Apps + PATs are supported.
  GitHub Apps with installation tokens are deferred to a future MCP.

## Tools exposed by github-mcp-server

This MCP exposes the upstream `github-mcp-server` toolset — repositories, issues,
pull requests, Actions, code search, and user/org tools. For the exact,
version-matched tool list and their parameters, see the upstream documentation:
<https://github.com/github/github-mcp-server>.

## Troubleshooting

- **401 from GitHub**: token revoked or scopes insufficient. Reconnect and
  pick the services that cover what you need.
- **`github-mcp-server binary not found`** in sidecar logs: the multi-stage
  Docker build failed at stage 1. Check the GitHub ref is valid:
  `docker build --build-arg GITHUB_MCP_REF=main .`
- **Stale OAuth token mid-session**: see Limitations. Start a new chat to
  pick up the refreshed token.
