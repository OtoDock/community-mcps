# Email

IMAP + SMTP email account access for agents, via [`mcp-mail-server`](https://www.npmjs.com/package/mcp-mail-server).

| Field | Value |
|-------|-------|
| Manifest name | `email-server` |
| Runtime | Node (stdio) |
| Upstream | `npm:mcp-mail-server` (unpinned — installs the latest release; validated on 1.2.1, and 2.0.2 keeps the same entry point and env contract) |
| Credentials | **Per-user** (`EMAIL_USER`, `EMAIL_PASS`) |
| Config (admin / user-overridable) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_SECURE` |
| Per-tool cost | None |

## What it does

Lets the agent read inboxes (IMAP) and send mail (SMTP). Each user supplies their own mailbox credentials — there is no shared account.

## Credential flow

- Admin can pre-fill the SMTP/IMAP server defaults on the MCP's admin page so a user only enters address + password.
- Each user enters their own `EMAIL_USER` / `EMAIL_PASS` from the per-user MCP settings panel before the agent is allowed to use any tool. Credentials are Fernet-encrypted in the platform DB.

## Install layout

- `manifest.json` — MCP descriptor.
- The platform's MCP installer generates `package.json` from the manifest `source` and runs `npm install` in the live install directory; neither `package.json` nor `node_modules/` is committed to this repo.

## Operator notes

- For Gmail with 2FA: use an [App Password](https://myaccount.google.com/apppasswords), not the account password. SMTP host `smtp.gmail.com:465` (TLS), IMAP host `imap.gmail.com:993` (TLS).
- For self-hosted Mailcow / Stalwart / Dovecot: point both `SMTP_HOST` and `IMAP_HOST` at the same hostname; ports are typically `465` (SMTP submission) and `993` (IMAP).
