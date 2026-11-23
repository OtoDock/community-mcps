# Google Maps

Places search and details, directions, multi-stop routes, geocoding, weather, air quality and map pictures. Directions go by car, foot, bike or public transport, with departure and arrival times. The tools come from [`@cablate/mcp-google-map`](https://www.npmjs.com/package/@cablate/mcp-google-map), on Google's current Routes API and Places API (New).

| Field | Value |
|-------|-------|
| Manifest name | `google-maps` |
| Runtime | Node (stdio) |
| Upstream | `npm:@cablate/mcp-google-map` (MIT licence, unpinned, bounded `>=0.0.69,<1`) |
| Credentials (per instance) | `GOOGLE_MAPS_API_KEY` |
| Per-tool cost | None billed by the platform. Google bills the key's project. |
| Assignment mode | `explicit` (an admin configures the key as an instance) |
| Upstream project | [cablate](https://github.com/cablate/mcp-google-map) |
| Icon | none: Google's brand guidelines do not permit the Maps mark on a third-party integration |

## What it does

- Finds places (nearby, by text, along a route), compares them and reads their details.
- Plans routes for driving, walking, cycling and public transport, multi-stop trips in the shortest order and distance matrices.
- Geocodes and reverse-geocodes.
- Reads weather, air quality, time zones and elevation.
- Draws map pictures.

A map picture (`maps_static_map`) is saved under `maps/` in the workspace. The platform shows it in the chat (`result_files`). The skill has the agent add a Google Maps link to every route and place. The link opens the app on a phone.

## Install layout

- `manifest.json`: MCP descriptor.
- `skills/google-maps.md`: places, routes, links, map pictures and API errors.
- The platform installs the upstream npm package (manifest `source`) at install time. Nothing else is committed here.

## Operator notes

- **The key's Google Cloud project needs billing and these APIs enabled:**
  - Routes API (directions, routes, distances).
  - Places API (New) (search, details).
  - Geocoding API (addresses and coordinates). The platform's own address hint for a person's location uses it too.
  - Maps Static API (map pictures).

  Optional, per tool: Weather API, Air Quality API, Time Zone API, Elevation API. A disabled API fails only its own tools. The agent names the API to enable.
- **Every tool is on, with no setting to fill in.** The API key is the only thing an admin enters. `maps_local_rank_tracker` runs one billed search per grid point and keyword (9 by default, up to 147). The skill has the agent use it only when asked for a local ranking. First the agent says how many searches it runs.
- Restrict the key to these APIs in Google Cloud. It reaches only this MCP's process and the platform's address hint.
- Attribution: Google requires its places and reviews to be shown as Google Maps content. The skill asks the agent to name the source.
- This entry replaced the former OtoDock fork `OtoDock/google-maps-mcp-server` (deleted). The fork called Google's legacy Directions, Distance Matrix and Places APIs. A Google Cloud project created after 2025-03-01 cannot enable them. An install of the fork is offered the switch on the MCP Servers page. Its instances and their key carry over.
