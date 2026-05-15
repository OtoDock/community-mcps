# Google Maps

Places search, directions, and geocoding via the Google Maps Platform APIs, packaged as [`google-maps-mcp-server`](https://pypi.org/project/google-maps-mcp-server/).

| Field | Value |
|-------|-------|
| Manifest name | `google-maps` |
| Runtime | Python (stdio) |
| Upstream | `pypi:google-maps-mcp-server@0.2.1` |
| Credentials (per instance) | `GOOGLE_MAPS_API_KEY` |
| Per-tool cost | None directly billed by the platform — Google bills you for API usage |
| Assignment mode | `explicit` (admin must configure an instance before agents can use it) |
| Patched | **Yes** — `patches/places.py` |

## What it does

Provides four canonical maps tools to agents:

- `search_places` — keyword + location based search returning name, address, ratings, etc.
- `get_directions` — turn-by-turn directions for the given mode (`driving`, `walking`, `transit`).
- `geocode` — address ↔ lat/lng.
- `reverse_geocode` — lat/lng → address.

## Patch — `patches/places.py`

The upstream package uses the legacy `SearchNearbyRequest` proto which doesn't honor the `keyword` field, so multi-word searches like `"coffee shop"` returned generic nearby places. Our patch:

1. Switches to `SearchTextRequest` for keyword queries.
2. Normalizes underscores → spaces (LLMs sometimes emit `coffee_shop`).
3. Splits the query into tokens and applies them through the text search endpoint.

The patch file is applied by the platform's MCP installer **after** `pip install` lands the package into the live venv — never edit the installed venv files directly; modify the patch.

## Install layout

- `manifest.json` — MCP descriptor.
- `patches/places.py` — replacement for the upstream `tools/places.py`.
- `skills/google-maps.md` — usage skill loaded into agent prompts when this MCP is enabled.

## Operator notes

- Get an API key from [Google Cloud Console → Maps Platform](https://console.cloud.google.com/google/maps-apis/credentials). Enable: Places API (New), Directions API, Geocoding API.
- Restrict the key by IP (your OtoDock host) or by referrer in Cloud Console.
- All Maps tools count against your Google Cloud quota; Google bills the project owner directly.
