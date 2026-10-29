## Nextcloud

The user's own Nextcloud account (credentials configured in their Settings):
files, and the Nextcloud apps their server runs (calendar, contacts, notes,
deck, Talk, mail, tables, collectives…). The tools are prefixed `nc_` (files:
`nc_webdav_*`); tools for an app the server does not have are hidden.

### Reading files
- Find a file with `nc_webdav_list_directory`, `nc_webdav_find_by_name` or
  `nc_webdav_search_files`.
- `nc_webdav_read_file` returns the content in the tool result: plain files as
  they are, documents (PDF, Word, Excel, PowerPoint) as extracted text.
  `parse_document: "markdown"` keeps headings and tables; read a long PDF in
  ranges (`page_start` / `page_end`). When `parse_notes` is not empty, tell the
  user what degraded. Offer the returned `url` to open the file in Nextcloud.
- Nothing is downloaded into the workspace: there is no download-to-path tool.

### Writing files
- `nc_webdav_write_file` writes text content (notes, markdown, CSV, code).
  Creating a file: omit `if_match`. Changing an existing file: read it first and
  pass its `etag` as `if_match`; on a conflict, re-read and tell the user. Never
  force with `"*"` unless the user asks for it.
- Binary documents (Word, Excel, PowerPoint, images) cannot be written back:
  produce the new version in the workspace (the file-tools MCP) and hand it over
  with `send_file` for the user to upload.

### Safety rules
- **Never delete, move, rename or overwrite files**, and never create shares or
  public links, without explicit user confirmation.
- **Never send mail or Talk messages, or change calendars, contacts or deck
  cards**, without the user confirming the exact content.
- Read-only operations (list, search, read) are always safe.
