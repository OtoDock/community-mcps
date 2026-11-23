# Mastodon

Mastodon / Fediverse MCP, with 37 tools via the `activitypub-mcp` server. It
reads timelines, threads, notifications, trending hashtags and posts, and
searches across ActivityPub servers. It posts statuses and threads, replies,
boosts, favourites, bookmarks, follows, votes in polls, uploads media, and
schedules posts. Reads span Mastodon, Misskey, Pleroma/Akkoma, Lemmy,
PeerTube, and Pixelfed. Instance-API reads and all writes target
Mastodon/Misskey instances.

| Field | Value |
|-------|-------|
| Manifest name | `mastodon-mcp` |
| Runtime | `node` (stdio) |
| Upstream | `npm:activitypub-mcp` (https://github.com/cameronrye/activitypub-mcp, MIT) |
| Credentials | Per-user (instance URL + access token) |
| Per-tool cost | None |
| Assignment mode | `auto` |
| Upstream project | [cameronrye](https://github.com/cameronrye/activitypub-mcp) |
| Icon | none: Mastodon's trademark policy (https://joinmastodon.org/trademark) allows the word mark only, in referential phrases |

## What it does

Reads (public, no account): discover actors, fetch actor timelines, read post
threads, instance info, public timelines, trending hashtags/posts, search,
discover instances. Reads (authenticated): home timeline, notifications,
bookmarks, favourites, relationships, scheduled posts. Writes: post
and reply, delete, boost, favourite, bookmark, follow/mute/block, vote on
polls, upload media, and manage scheduled posts. Typical agent use: monitor
mentions and trends across the fediverse, or publish an update with an image
rendered earlier in the session.

## Install layout

- `manifest.json`: catalog manifest (node/stdio, per-user credentials).
- `README.md`: this file.

The platform installs the npm package at install time (unpinned, bounded
`>=3.2.1,<4`).

## Operator notes

- **Write tools are on.** The manifest's `env` sets
  `ACTIVITYPUB_ENABLE_WRITES` to `true`, so agents can post, boost and follow
  out of the box. Upstream's own default is off, and a config default reaches
  the server only once an admin saves the form.

  In a chat's default mode, the permission card asks before a write tool's
  first use in that chat. One Allow then covers that tool for the rest of the
  chat. Scheduled tasks, triggers and calls run write tools without asking.

  To hide the write tools, set **Enable write tools** to `false` (per-user
  overridable). Then injected fediverse content cannot trigger account
  actions.

  The upstream login stores a READ-ONLY token when writes are off at login
  time. So turning writes on later needs a re-login. An install that took
  the earlier form default never had writes on. Review the upstream threat
  model.
- **Credentials:** each user connects with their **instance** (e.g.
  `mastodon.social`) and a **static access token**. Obtain the token from the
  instance: Preferences → Development → New application. Grant the scopes
  you need (`read` for reads, `read write` for posting), then copy its access
  token. Mastodon access tokens do not expire. Revoke them from the same
  page.
- **No OAuth relay.** Mastodon is decentralized: every instance is its own
  OAuth authority (per-instance app registration). So there is no single
  hosted OAuth app to relay through. A per-user static token is the correct
  model.
- **Media uploads** (`upload-media`) read a file path from the session
  workspace. The framework policy-gates the path before the server sees it.
- Fediverse reach: `discover-actor` and `fetch-timeline` speak plain
  ActivityPub and read any conformant server. Instance-API reads (search,
  trending, public timeline) and all writes require a Mastodon- or
  Misskey-API instance.
