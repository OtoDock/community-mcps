# Email

IMAP + SMTP email account access for agents, via [`mcp-mail-server`](https://www.npmjs.com/package/mcp-mail-server).

| Field | Value |
|-------|-------|
| Manifest name | `email-server` |
| Runtime | Node (stdio) |
| Upstream | `npm:mcp-mail-server` (unpinned: installs the latest release; validated on 2.1.0, which needs Node 22.13 or newer) |
| Credentials | **Per-user** (`EMAIL_USER`, `EMAIL_PASS`) |
| Config (admin / user-overridable) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_SECURE`, `EMAIL_ADDRESS` |
| Per-tool cost | None |
| Upstream project | [yunfeizhu](https://github.com/yunfeizhu/mcp-mail-server) |
| Icon | none: the project has no mark |

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
- IMAP must use TLS from the start (port 993, `IMAP_SECURE` true): the server refuses to start on false, because its IMAP client cannot guarantee STARTTLS before the login. SMTP takes either: `SMTP_SECURE` true on 465, false on 587 (STARTTLS required).
- `EMAIL_ADDRESS` is the From address when it differs from the login (an alias, a shared mailbox); empty sends as the login.
- Attachments: `send_email` attaches files and `save_attachment` saves them, both within the session's workspace, user folder and shared workspace (`MAIL_ALLOWED_ROOTS`, set by the platform; paths are translated for paired machines). On a Windows machine the list's `:` separator is wrong for that OS, so attachments fail there; mail itself works.
