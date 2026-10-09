# github-mcp

GitHub repositories, issues, pull requests and code search through GitHub's
hosted MCP server (`https://api.githubcopilot.com/mcp/`), the remote form of
the official [`github/github-mcp-server`](https://github.com/github/github-mcp-server).
Nothing runs on the install: each request leaves through the platform's
credential gateway, which adds the user's OAuth token or PAT on the way out,
so the token never reaches a session's config or a paired machine's disk.

| Field | Value |
|-------|-------|
| Manifest name | `github-mcp` |
| Runtime | Remote (vendor-hosted, streamable HTTP) |
| Upstream | `remote:api.githubcopilot.com` |
| Credentials | Per-user OAuth (an OAuth App, or the hosted relay) or a PAT |
| Upstream project | [GitHub](https://github.com/github/github-mcp-server) |
| Icon | the GitHub Invertocat (black) from https://brand.github.com/foundations/logo (GitHub_Logos.zip), permitted to "inform others that your project integrates with GitHub", unaltered |

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

The user follows the connect flow → picks **Personal Access Token** in
the form → pastes the token they generated at
<https://github.com/settings/tokens?type=beta>.

## Connect (per user)

1. User settings → **Accounts** → click **Connect GitHub**.
2. Pick the auth method:
   - **OAuth** — browser consent flow, then granted services map to scopes
     (`repo`, `workflow`, `read:user`, `user:email`, and `admin:org_hook`
     for organization-wide event subscriptions).
   - **Personal Access Token** — paste a token you generated yourself with
     the scopes for the services you want.
3. The platform persists the token under
   `sessions/github-tokens/user/<your-label>.json`.

## Events (webhooks)

The MCP can also receive GitHub events: User settings → **Accounts** → the
GitHub account → **Subscribe to events** (or Agent Settings → MCPs → GitHub
→ Subscribe to events for this agent). A subscription registers a webhook
with GitHub and fires the triggers bound to it — pushes, pull requests,
issues and their comments, reviews, discussions, releases, workflow runs,
stars, forks, branch and tag lifecycle, commit comments and repository
changes. Two kinds of subscription:

- **One repository** (`owner/name`): a repository webhook. Needs admin
  access on that repository.
- **Every repository in an organization** (`org`): one organization
  webhook covering every repository the organization has now and later.
  Needs the **Organization webhooks** permission on the connected account
  (`admin:org_hook`: tick it when connecting, or reconnect) and owner
  rights on the organization. A repository already covered by an
  organization subscription fires twice if it also has its own: pick one.

Events are delivered straight from GitHub to the platform's public URL and
verified with a per-subscription secret; the hosted OAuth relay is not in
that path.

## OAuth vs PAT — when to pick which

|                         | OAuth                               | PAT                          |
|-------------------------|-------------------------------------|------------------------------|
| First-time UX           | Browser consent dialog              | Paste a string               |
| Token lifetime          | ~8 hours, then refresh              | Up to 1 year (you set it)    |
| Mid-session expiry      | Refreshed per request (gateway)     | Never (until manual rotate)  |
| Scope changes           | Re-consent in browser               | Regenerate in GitHub settings |
| Revocation              | GitHub UI or `auth.revoke`          | Delete in GitHub settings    |
| Admin needs OAuth App?  | Yes                                 | No                           |

**For self-hosted single-user installs**: pick PAT. Simpler, no expiry
surprises.

**For multi-user deployments**: pick OAuth. Per-user scope grants + better
audit trail.

## What the agent sees

```
## GitHub Identity

You are acting on GitHub as **alice@example.com**. Your bearer token grants
whatever access this account has on GitHub. When calling `repo`-scoped
tools, default to repositories owned by this user unless the prompt
explicitly names a different org.
```

## `git` and `gh` in the agent's shell

Connecting GitHub also signs `git` and `gh` in for the agent's shell, with the
same account the MCP uses: `git clone` / `push` against `github.com` and
`gh pr create` work without `gh auth login`. Same scope rules as the MCP:
user-scope chats use the user's bound account, agent-scope tasks, calls and
triggers the bound service account.

## Limitations

- **GitHub Enterprise Server** is not covered: the hosted server serves
  github.com only.
- **GitHub Apps** (installation tokens) are not supported; OAuth Apps and PATs
  are.

## Moving from the self-hosted entry

Up to 1.2.0 this entry ran `github-mcp-server` in a container beside the
platform (`ghcr.io/otodock/github-mcp`). An install of that version is offered
the switch on the MCP Servers page (platform 1.7.1 or newer); accounts, webhook
subscriptions and agent assignments carry, the container is stopped and its
image removed, and the bearer allowlist already holds `api.githubcopilot.com` for the
`github` provider (seeded since 1.7.1).

## Tools

GitHub's default toolset: repositories, issues, pull requests, code and
commit search, releases, branches and users (46 tools on 2026-10-09). The exact
list follows GitHub's hosted server; see
<https://github.com/github/github-mcp-server>.

## Troubleshooting

- **401 from GitHub**: token revoked or scopes insufficient. Reconnect and
  pick the services that cover what you need.
- **GitHub left out of a session with "host not allowed"**: an admin removed
  the `github` / `api.githubcopilot.com` row from the bearer allowlist
  (Admin → Security); restore it.
