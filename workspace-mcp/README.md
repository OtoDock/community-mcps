# Google Workspace

Google Workspace access (Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Tasks, Contacts, Chat, Apps Script) via Google OAuth, packaged as [`workspace-mcp`](https://pypi.org/project/workspace-mcp/).

| Field | Value |
|-------|-------|
| Manifest name | `google-workspace` |
| Runtime | Python (stdio) |
| Upstream | `pypi:workspace-mcp@1.18.0` |
| Credentials | **Per-user OAuth** (Gmail / Calendar / Contacts scopes selectable) |
| App credential (admin) | `google-oauth-app` — `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` |
| Hosted relay | OtoDock Google Relay (default for managed deployments) |
| Per-tool cost | None |
| Assignment mode | `auto` (per-user OAuth, no admin instance config) |

## What it does

Lets agents work across the user's Google Workspace — mail, Drive files, calendar, Docs/Sheets/Slides, Forms, Tasks, and Contacts — on behalf of the signed-in user. Each user OAuths into Google individually; the agent only sees that user's data.

The platform's OAuth bridge handles the popup flow, token refresh, and writeback. The MCP itself only reads tokens from a per-session directory the platform populates.

## OAuth modes

Two ways to wire up Google OAuth:

1. **Hosted (default)** — Use the OtoDock-operated Google OAuth app. No Google Cloud Console setup. The platform proxies the OAuth flow through the OtoDock relay. Best for getting started.
2. **Self-hosted** — Create your own Google Cloud project, enable Gmail/Calendar/People API, configure an OAuth consent screen, and add a `Web application` client. Drop the client ID + secret into the admin page. Best when you don't want a third party to hold your refresh tokens.

## Install layout

- `manifest.json` — MCP descriptor with `path_env` declarations for the per-user credentials dir.

## Operator notes

- Per-user tokens live under `<workspace>/credentials/google-tokens/` (translated to the actual sandbox path at session start).
- `OAUTHLIB_INSECURE_TRANSPORT` is auto-derived from the platform's `oauth_insecure_transport` setting — on HTTPS deployments this is automatically `false`.
- The OAuth scopes the user agrees to are surfaced in the per-user MCP settings ("Gmail", "Calendar", "Contacts"); revocation is also exposed there and propagates to the upstream Google account.
