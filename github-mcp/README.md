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
| Credentials | Per-user OAuth (an OAuth App or a GitHub App, or the hosted relay) or a PAT |
| Upstream project | [GitHub](https://github.com/github/github-mcp-server) |
| Icon | the GitHub Invertocat (black) from https://brand.github.com/foundations/logo (GitHub_Logos.zip), permitted to "inform others that your project integrates with GitHub", unaltered |

## Setup (admin)

### Option A — an OAuth App

1. Create a GitHub OAuth App at <https://github.com/settings/developers> →
   **OAuth Apps** → **New OAuth App**.

2. **Authorization callback URL**: your OtoDock instance's OAuth callback:

       https://<your-otodock>/v1/oauth/github/callback

3. Copy the **Client ID** and **Client secret**.

4. In OtoDock admin → **MCP Servers** → install `github-mcp` → open
   detail page → **Credentials** → paste the Client ID and Secret.

### Option A2 — a GitHub App (narrowed to chosen repositories)

A GitHub App is installed on the repositories a person chooses, and its
tokens reach those only. Pick **GitHub App** on the admin card and paste
the five values from <https://github.com/settings/apps> → **New GitHub
App**: the **App ID**, the **Client ID**, a generated **client secret**, a
generated **private key** (the `.pem` file's text) and the **webhook
secret**. The settings page needs three addresses from the card: the
callback URL, the setup URL (with "Redirect on update" ticked) and the
webhook URL. Grant the repository permissions Contents, Issues, Pull
requests, Actions, Workflows, Discussions (read and write), Metadata and
Commit statuses (read), and the organization permission Members (read),
which lets an organization owner be told apart from a member.

A person then signs in and installs the app where they choose (the card
offers **Install on GitHub** and **Refresh**); an account that installed
nothing reaches nothing. A manager can bind one of their owned
installations as an agent's service identity: the agent's `git` and `gh`
then act with the installation's own token, which reaches its
repositories only.

**With hosted sign-in** nothing is registered: OtoDock's own GitHub App
serves the sign-in and the install page, and its events reach the install
through OtoDock's relay. A person connects, picks **Install on GitHub** on
the card and chooses the repositories. Hosted sign-in has no installation
identity for an agent.

### Option B — Personal Access Token

Skip the OAuth App registration. Each user generates their own PAT.

The user follows the connect flow → picks **Personal Access Token** in
the form → pastes the token they generated at
<https://github.com/settings/tokens?type=beta>.

## Connect (per user)

1. User settings → **Integrations** → GitHub → **Connect**.
2. Pick one of the ways to connect. The dialog lists the ones this install
   offers, the first one picked:
   - **GitHub App** (recommended): sign in with GitHub, then install the
     app on the repositories you choose. Shown when the install signs in
     through a GitHub App (an admin's own, or OtoDock's with hosted
     sign-in). No services are ticked: the app's permissions decide.
   - **GitHub sign-in**: "Sign in with your personal GitHub account." The
     OAuth App: the services you tick map to scopes (`repo`, `workflow`,
     `read:user`, `user:email`, and `admin:org_hook` for organization-wide
     event subscriptions). With hosted sign-in both browser ways are listed;
     a self-managed install lists the kind its admin registered.
   - **Personal Access Token**: "Create a personal access token on GitHub
     and paste it here." Give it the scopes for the services you tick.
3. The platform persists the token under
   `sessions/github-tokens/<your-username>/<your-label>.json`.

## Events (webhooks)

The MCP can also receive GitHub events: User settings → **Integrations** →
the GitHub account → **Subscribe to events** (or Agent Settings → MCPs → GitHub
→ Subscribe to events for this agent). A subscription registers a webhook
with GitHub and fires the triggers bound to it — pushes, pull requests,
issues and their comments, reviews and review threads, discussions,
releases, workflow runs and their jobs, check runs and suites,
deployments, Dependabot, code scanning and secret scanning alerts,
repository advisories, labels and milestones, stars, forks, branch and
tag lifecycle, commit comments, repository changes and a repository made
public, collaborators, teams and organization membership. Three kinds of
subscription:

- **One repository**: type it as `owner/name`. A repository webhook. You
  need admin access on it.
- **Every repository in an organization**: type the organization's name.
  One subscription covers every repository in it, now and later. You need
  owner rights on the organization and the **Organization webhooks**
  permission on the connected account (`admin:org_hook`: tick it when
  connecting, or reconnect). A repository already covered by an
  organization subscription fires twice if it also has its own: pick one.
- **An installation** (a GitHub-App account): pick it from the list. One
  subscription covers every repository the installation reaches, now and
  later, and nothing is registered on GitHub. You can subscribe only to an
  installation you own.

When the connected account lacks a permission the ticked events need, the
dialog says so above the events ("These events need the repo permission.
Reconnect the account and tick Repositories."). A GitHub-App account never
sees that line: its permissions come from the app.

Events are delivered straight from GitHub to the platform's public URL and
verified with a per-subscription secret; the hosted OAuth relay is not in
that path. An installation subscription of a hosted account is the
exception: OtoDock's app delivers it through the relay.

## Which way to connect

|                 | GitHub App                         | GitHub sign-in                    | Personal Access Token          |
|-----------------|------------------------------------|-----------------------------------|--------------------------------|
| Reaches         | The repositories you install it on | Every repository you can see      | What you gave the token        |
| You do          | Sign in, then Install on GitHub    | Sign in and tick the services     | Create a token and paste it    |
| Admin setup     | None with hosted sign-in, else a GitHub App | None with hosted sign-in, else an OAuth App | None          |
| Token renewal   | OtoDock keeps it current           | OtoDock keeps it current          | None: it lasts until the date you set |
| Change the reach | Change on GitHub, then Refresh    | Reconnect and tick other services | Edit the token on GitHub       |

Pick the GitHub App when agents should reach only chosen repositories.
GitHub sign-in reaches everything the person can, organization
repositories included. A token suits a one-person install, or a narrow
token kept as its own account for one agent.

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
triggers the bound service account, or the bound installation's own token.

## Limitations

- **GitHub Enterprise Server** is not covered: the hosted server serves
  github.com only.
- **GitHub Apps**: an agent's identity takes the whole installation; the
  hosted MCP server itself gets the person's user token (narrowed by
  GitHub to the app's installations) while `git` and `gh` get the
  installation's own.

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
