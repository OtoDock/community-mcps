## Web Browsing & Research

### Tool Selection
- **`WebSearch`** — for search queries (finding information, research, looking things up). Uses a search API with no bot detection issues. Always use this instead of navigating to google.com.
- **Playwright** — for visiting and interacting with websites (navigating pages, reading content, filling forms, clicking elements, taking screenshots). Uses an anti-detect browser (Camoufox) that bypasses Cloudflare and most bot detection. Works on sites that block normal automated browsers.
- **`WebFetch`** — only for raw API endpoints or simple static pages. Cannot execute JavaScript — most modern sites will return empty or broken results.

### Browsing Best Practices
- **Do NOT navigate to google.com** — Google aggressively blocks all automated browsers regardless of stealth. Use `WebSearch` for all search queries.
- **Prefer screenshots over snapshots** — Vision mode is enabled, so `browser_take_screenshot` returns images you can see directly. Avoid `browser_snapshot` on content-heavy pages (e-commerce, news, social media) as it dumps the full accessibility tree and wastes context. Use `browser_run_code` to query specific elements when you need text data from the page.
- **Do NOT pass file paths** to `browser_take_screenshot` — call it with default parameters. The screenshot is returned inline as an image.
- **Where browser screenshots actually go (read carefully).** `browser_take_screenshot`'s result mentions a path like `screenshots/page-….png`, but the file is **NOT** there. It is saved into your workspace's **hidden `.screenshots/` folder — note the leading dot** — separate from the regular `screenshots/` folder. To show one to the user, call `display_images` with the absolute path **`/users/<your-username>/workspace/.screenshots/<filename>`** (or `/workspace/.screenshots/<filename>` for shared/agent-scoped work). ⚠️ Do **not** use `…/workspace/screenshots/<filename>` (no dot) or the raw `/screenshots/…` path the tool prints — both are wrong; the file is always in **`.screenshots/`** (with the dot). Only the newest ~15 are kept, so display soon after capturing.
- **Be efficient** — navigate directly to target URLs when possible. Minimize tool calls.
- Prefer **viewport screenshots** focused on the relevant content (scroll to the right section first). Full-page screenshots include too much noise. Take 1-3 focused viewport captures rather than one huge full-page screenshot.
