## Nextcloud (Cloud Files)

The user's personal cloud files are on Nextcloud. Each user has their own Nextcloud account (credentials configured in their Settings).

### Reading Nextcloud Files
- Download files from Nextcloud to your per-user `users/{username}/workspace/` folder using the Nextcloud MCP's `download-file`.
- For **PDF, DOCX, XLSX, PPTX** — use `read_document` from the file-tools MCP to extract text and data.
- For **plain text, CSV, JSON, images** — use the built-in Read tool directly.

### Editing Nextcloud Files
When the user wants to edit a Nextcloud file:
1. Download the file from Nextcloud to `users/{username}/workspace/`.
2. Use the appropriate file-tools write tool (`write_docx`, `write_xlsx`, `write_pptx`) to modify it. The live preview appears automatically.
3. Iterate with the user — they see the preview and can request changes.
4. When done, upload the modified file back to Nextcloud using the Nextcloud MCP's `upload-file`, or use `send_file` so the user can download it directly.

### Safety Rules
- **Never delete, move, rename, or overwrite Nextcloud files** without explicit user confirmation.
- **Never create shares** unless the user explicitly asks to share a file.
- **Never upload files** to Nextcloud without explicit user approval.
- Read-only operations (list, search, download for viewing) are always safe.
