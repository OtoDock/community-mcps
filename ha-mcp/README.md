# Home Assistant

Smart-home control via Home Assistant's REST + WebSocket API, packaged as [`ha-mcp`](https://pypi.org/project/ha-mcp/).

| Field | Value |
|-------|-------|
| Manifest name | `home-assistant` |
| Runtime | Python (stdio) |
| Upstream | `pypi:ha-mcp@7.2.0` |
| Credentials (per instance) | `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` |
| Per-tool cost | None |
| Assignment mode | `explicit` (admin must configure an instance) |
| Tool count | ~97 |

## What it does

Exposes a large surface of Home Assistant entities and services: lights, switches, scenes, scripts, automations, climate, media players, sensors, calendar entries, energy stats. Tools cover both read (`get_state`, `list_entities`) and write (`turn_on`, `call_service`, `trigger_automation`) operations.

## Install layout

- `manifest.json` — MCP descriptor. Declares the instance fields used to populate the upstream env vars.

## Operator notes

- Create a **Long-Lived Access Token** under your Home Assistant profile → Security. Treat it as a password.
- `HOMEASSISTANT_URL` should point at the HA frontend the OtoDock host can reach (e.g. `http://homeassistant.local:8123` or your reverse-proxied HTTPS URL). The MCP follows redirects.
- Because this MCP is `assignment_mode: "explicit"`, an admin configures the URL+token instance once; agents are assigned the MCP afterwards.
