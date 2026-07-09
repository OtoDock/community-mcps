# Microsoft 365 MCP

Microsoft 365 access (Mail, Calendar, Teams chat / meetings / transcripts,
OneDrive, SharePoint, Contacts) via the upstream
[`softeria/ms-365-mcp-server`](https://github.com/softeria/ms-365-mcp-server),
wrapped as a Docker container that OtoDock launches alongside the proxy.

The container runs the upstream in HTTP mode (`--http <port>`) so OtoDock
forwards the user's OAuth token (or service-account token) as
`Authorization: Bearer <token>` on every MCP request — no sidecar, no
per-session subprocess churn.

## Admin setup

You need a Microsoft Entra ID (Azure AD) OAuth app. One app serves every
OtoDock user; users individually consent at connect time.

### 1. Register the app

1. Visit <https://entra.microsoft.com> → **App registrations** → **New
   registration**.
2. **Name**: anything (e.g. "OtoDock Microsoft 365").
3. **Supported account types**:
   * Choose **multi-tenant** (`Accounts in any organizational directory`)
     for OtoDock cloud / multi-customer deployments.
   * Choose **single-tenant** (`Accounts in this organizational directory
     only`) for security-sensitive deployments where only one company's
     users should be able to connect.
4. **Redirect URI**: Web → `https://<your-otodock>/v1/oauth/microsoft/callback`.
5. Click **Register**.

### 2. Mint a client secret

1. In the new app → **Certificates & secrets** → **Client secrets** →
   **New client secret**.
2. Set a long expiry (24 months is the maximum).
3. Copy the secret **Value** immediately — Microsoft never shows it
   again.

### 3. (Optional) Set up tenant-admin consent for `*.All` scopes

The `OnlineMeetingTranscript.Read.All` and `Sites.ReadWrite.All` scopes
require **tenant-admin consent** — a per-user consent dialog isn't
sufficient. Without it, tools targeting those scopes return
`AADSTS65001: The user or administrator has not consented to use the
application`.

You have two options:

* **OtoDock-driven (recommended)**: in OtoDock → Admin → MCP Servers →
  m365-mcp → "Grant for whole tenant" button. Opens the Microsoft
  tenant-admin consent URL — sign in as a tenant admin, click Accept,
  done. Backed by `POST /v1/oauth/microsoft/admin-consent/start`. The
  tenant grant is permanent until the admin revokes it in the Entra
  console.
* **Manually at Entra**: app registration → **API permissions** → add
  the `.All` permissions → click **Grant admin consent for `<tenant>`**.

### 4. (Required for `OnlineMeetingTranscript.Read.All`) Teams Premium

Microsoft only exposes meeting transcripts via Graph when the tenant
has a **Teams Premium** SKU. Tenants without Premium get AADSTS65001
even after admin consent.

### 5. Paste into OtoDock

In OtoDock → Admin → MCP Servers → m365-mcp → **OAuth App Credentials**:

| Field | Value |
|---|---|
| Application (client) ID | from app registration → Overview |
| Client Secret | the Value from step 2 |
| Tenant ID | EITHER blank (multi-tenant) OR your Azure AD tenant UUID (single-tenant lockdown) |

Single-tenant deploys MUST paste a tenant UUID — leaving it blank lets
ANY Microsoft user (work or personal MSA) connect via the `/common/`
endpoint, which is usually wrong for a company install. The dashboard
shows a warning if MS_TENANT_ID is empty.

### 6. Enable + assign

* Admin → MCP Servers → m365-mcp → Enable. The Docker container starts.
* Assign to specific agents via the agent settings, or set
  `assignment_mode: auto` (default) so managers can toggle it per agent.

## User flow

Settings → Integrations → Microsoft 365 → **Connect** → pick services →
consent in Microsoft → done.

The token lands at `sessions/microsoft-tokens/<user>/<account_label>.json`
with `extra.tenant_id`, `extra.preferred_username`, and `extra.object_id`
populated from the id_token claims — usable via
`${account.extra.preferred_username}` etc. in agent prompts.

For headless / CLI environments where there's no browser: pick the
**device code** flow on the connect dialog. The user gets a code +
verification URL to enter on another device.

## Tool curation (optional)

ms-365-mcp-server ships ~200 tools (the entire Microsoft Graph surface).
For most agents this is fine — `tools/list` is metadata-only and the
LLM only loads schemas on demand. If you want to restrict the tool
surface (e.g. an agent should ONLY see mail+calendar):

1. Admin → MCP Servers → m365-mcp → **Tool Filter** field.
2. Paste a regex matching the tool names you want exposed. Examples:
   * `^(mail|calendar)_` — only mail + calendar tools.
   * `^(mail|calendar|teams)_` — mail + calendar + Teams.
   * `^(?!sharepoint_)` — everything EXCEPT SharePoint.
3. Save. The Docker container restarts to pick up the new
   `--enabled-tools <regex>` flag.

The regex maps onto upstream's `--enabled-tools` CLI flag. Run
`docker run --rm node:22-slim sh -c 'npm i -g @softeria/ms-365-mcp-server
&& ms-365-mcp-server --list-permissions'` to see the full tool list
before crafting the regex.

This is the generic OtoDock Tool Filter feature — any MCP
that declares `tool_filter.arg_name` in its manifest can use the same
admin field.

## Limitations

* **No live meeting attendance**: this MCP provides post-meeting
  transcripts (via Graph). For bots that JOIN meetings and stream live
  audio into OtoDock's STT pipeline (separate, future work) —
  needs Microsoft Teams Bot Framework Calling + Meetings SDK.
* **Refresh token rotation**: Microsoft rotates refresh tokens on
  every refresh. OtoDock's refresh worker handles this atomically;
  long-lived sessions just work.
* **Service account multi-account**: declare `service_account: true`
  on the manifest (already done) — admins connect a service-acting
  Microsoft account for agent-scope sessions (voice / task / trigger).
* **Personal MSAs vs work accounts**: multi-tenant deploys accept
  both. Single-tenant deploys reject personal MSAs entirely (the
  tenant-restricted authorize endpoint rejects them at login).
* **GitHub Enterprise / Microsoft on-prem**: not in scope. This MCP
  targets Microsoft 365 cloud only.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AADSTS65001` on a tool call | Service requires admin consent | Admin clicks "Grant for whole tenant" |
| `AADSTS65001` on `teams_transcripts` even after admin consent | Tenant lacks Teams Premium license | Buy / assign Teams Premium |
| `AADSTS65004` after clicking "Grant for whole tenant" | Caller isn't a Microsoft tenant admin | Ask a tenant admin to run the flow |
| `AADSTS90011` ("tenant identifier must be a GUID") | `MS_TENANT_ID` is empty + admin-consent attempted | Paste the tenant UUID first |
| Connect succeeds but personal MSA gets `interaction_required` on a `.All` scope | Personal MSAs can't grant org-tenant admin consent | Use a work account instead |
| 401 on every tool | Bearer didn't reach the MCP | Check Admin → OAuth Bearer Allowlist contains `(microsoft, localhost)` |
| Container fails to start | Upstream npm install failed | `docker logs m365-mcp` for the npm output |
