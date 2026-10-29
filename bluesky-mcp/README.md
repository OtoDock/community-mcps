# Bluesky

Bluesky / AT Protocol MCP: post (text, threads, images, video), read timelines
and feeds, search posts and people, manage follows/mutes/blocks/lists, DMs,
notifications, and account analysis — 51 tools against the official
`@atproto/api` client.

| Field | Value |
|-------|-------|
| Manifest name | `bluesky-mcp` |
| Runtime | `node` (stdio) |
| Upstream | `npm:atproto-mcp` (https://github.com/cameronrye/atproto-mcp, MIT) |
| Credentials | Per-user (handle + app password) |
| Per-tool cost | None (Bluesky API is free) |
| Assignment mode | `auto` |
| Upstream project | [cameronrye](https://github.com/cameronrye/atproto-mcp) |
| Icon | none: the Bluesky butterfly is free only as a social-media icon linking to a Bluesky profile; other uses need permission (trademarks@blueskyweb.xyz) |

## What it does

Reads: timelines, author feeds, custom feeds, post threads with context,
actor/post search, followers/following, notifications, conversations,
bookmarks, lists, starter packs, account analysis. Writes: create posts and
threads, reply, like/repost, follow/mute/block, update profile, upload
image/video media from the workspace, send DMs, manage lists and bookmarks,
report content. Typical agent use: publish a post with an image rendered
earlier in the session, monitor mentions/replies, or research a topic across
the network.

## Install layout

- `manifest.json` — catalog manifest (node/stdio, per-user credentials).
- `README.md` — this file.

The platform installs the npm package at install time (unpinned; bounded
`>=0.6.2,<1` — 0.6.2 fixed a Windows entry-point bug).

## Operator notes

- **Credentials:** each user connects with their handle (e.g.
  `you.bsky.social`) and an **app password** — created in Bluesky under
  Settings → Privacy & Security → App Passwords. Never the main account
  password. For `send_direct_message` to work, the app password must be
  created with **"Allow access to your direct messages"** checked.
- Session JWTs are refreshed automatically by the official client; app
  passwords don't expire — revoke them from the same Bluesky settings page.
- **Media uploads** (`upload_image` / `upload_video`) read files from the
  session workspace (`ATPROTO_MEDIA_DIR` is anchored there). Files outside
  the workspace are rejected by the upstream server's media-dir gate.
- Custom PDS / self-hosted AT Protocol servers: override the `ATPROTO_SERVICE`
  config (default `https://bsky.social`).
- Without credentials the upstream runs in a limited public read mode, but
  since Bluesky's 2025 API changes most endpoints (including `search_posts`)
  require authentication — treat credentials as effectively required.
- Upstream has built-in per-tool rate limiting and SSRF protection for
  outbound URL fetches; `NODE_ENV=production` (set by the manifest) keeps
  error output sanitized.
