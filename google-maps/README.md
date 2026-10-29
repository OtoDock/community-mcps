# Google Maps

Places search and details, directions by car, foot, bike or public transport (with departure and arrival times), multi-stop routes, geocoding, weather, air quality and map pictures, via [`@cablate/mcp-google-map`](https://www.npmjs.com/package/@cablate/mcp-google-map) on Google's current Routes API and Places API (New).

| Field | Value |
|-------|-------|
| Manifest name | `google-maps` |
| Runtime | Node (stdio) |
| Upstream | `npm:@cablate/mcp-google-map` (unpinned, bounded `>=0.0.69,<1`; MIT) |
| Credentials (per instance) | `GOOGLE_MAPS_API_KEY` |
| Per-tool cost | None billed by the platform; Google bills the key's project |
| Assignment mode | `explicit` (an admin configures the key as an instance) |
| Requires | OtoDock 1.8.0 (map pictures are saved and shown in the chat) |
| Upstream project | [cablate](https://github.com/cablate/mcp-google-map) |
| Icon | none: Google's brand guidelines do not permit the Maps mark on a third-party integration |

## What it does

Finds places (nearby, by text, along a route), compares them and reads their details; plans routes for driving, walking, cycling and public transport, multi-stop trips in the shortest order and distance matrices; geocodes and reverse-geocodes; reads weather, air quality, time zones and elevation; and draws map pictures. A map picture (`maps_static_map`) is saved under `maps/` in the workspace and shown in the chat by the platform (`result_files`). The skill has the agent add a Google Maps link to every route and place, which opens the app on a phone.

## Install layout

- `manifest.json` — MCP descriptor.
- `skills/google-maps.md` — places, routes, links, map pictures and API errors.
- The platform installs the upstream npm package (manifest `source`) at install time; nothing else is committed here.

## Operator notes

- **The key's Google Cloud project needs billing and these APIs enabled:** Routes API (directions, routes, distances), Places API (New) (search, details), Geocoding API (addresses and coordinates; the platform's own address hint for a person's location uses it too) and Maps Static API (map pictures). Optional, per tool: Weather API, Air Quality API, Time Zone API, Elevation API. A disabled API fails only its own tools; the agent names the API to enable.
- **Enabled tools** (admin setting) lists every tool except `maps_local_rank_tracker`, which runs a grid of billed searches; `*` enables all.
- Restrict the key to these APIs in Google Cloud. It reaches only this MCP's process and the platform's address hint.
- Attribution: Google requires its places and reviews to be shown as Google Maps content; the skill asks the agent to name the source.
- This entry replaced the OtoDock fork `OtoDock/google-maps-mcp-server`, which called Google's legacy Directions, Distance Matrix and Places APIs: a Google Cloud project created after 2025-03-01 cannot enable them. An install of the fork is offered the switch on the MCP Servers page; its instances and their key carry.
