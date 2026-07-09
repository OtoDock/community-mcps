# Notion MCP

Connects OtoDock agents to **Notion** by running Notion's official local MCP server (`@notionhq/notion-mcp-server`) as a stdio subprocess. OtoDock runs the OAuth dance and injects the user's Notion token (`NOTION_TOKEN`) into the server's environment; the server then calls the Notion API directly with that token.

## What admins need to do

1. **Create a Notion integration**
   - Open <https://www.notion.so/my-integrations>
   - Click "New integration"
   - Type: **Public integration** (required for OAuth — internal integrations don't support the OAuth flow)
   - Capabilities: pick **Read content** + **Update content** + **Insert content** (omit Comments if you want a tighter scope)
   - Redirect URI: `https://<your-dashboard-public-url>/v1/oauth/notion/callback`
   - Note your **OAuth Client ID** and **OAuth Client Secret**

2. **Paste credentials into OtoDock**
   - Admin → MCP Servers → notion-mcp → "OAuth App Credentials"
   - Paste `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET`

## What users need to do

Click **Connect Notion Account** in User Settings → Integrations. Notion's consent screen asks the user to pick which pages/databases the integration can access (this is Notion's own page-level access model — OtoDock doesn't override it). Approve, and the account appears in the list.

Multi-account: one Notion OAuth connection per workspace. Add another for a second workspace.

## Notes on Notion's permission model

Notion's OAuth doesn't grant access to your entire workspace — only the **pages you explicitly share with the integration** during the consent flow. If a tool call returns "Page not found", the user needs to share that page (or its parent) with the OtoDock integration in Notion's UI.

## Refresh token rotation

Notion rotates the refresh token on every refresh. The framework's `provider.refresh()` always re-persists BOTH the access and refresh tokens, so rotation is handled automatically. If you see "invalid_grant" errors after a long idle period, the rotation chain was broken — reconnect.

## Troubleshooting

- **401 / "API token is invalid"** — the Notion token didn't reach the server, or the connection lapsed. Reconnect the account in User Settings.
- **"object_not_found" on a page** — the integration doesn't have access. Share the page with the integration in Notion's UI.
- **Connect popup goes blank** — Notion sometimes rejects redirect URIs with trailing slashes. Make sure the redirect URI in your integration matches `https://<dashboard>/v1/oauth/notion/callback` exactly.
