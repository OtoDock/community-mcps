# Packrift MCP

Packrift MCP exposes packaging-supplies catalog, pricing, inventory, shipping-estimate, packaging-recommendation, and cart helper tools for Packrift.

| Field | Value |
|-------|-------|
| Manifest name | `packrift-mcp` |
| Runtime | Docker |
| Upstream | [`Packrift/packrift-mcp`](https://github.com/Packrift/packrift-mcp) |
| Image | `ghcr.io/packrift/packrift-mcp:latest` |
| Hosted endpoint | `https://mcp.packrift.com/mcp` |
| Transport | HTTP (`/mcp`) on port `8787` |
| Credentials | `SHOPIFY_PACKRIFT_TOKEN` per instance |
| Per-tool cost | None |
| Assignment mode | `explicit` |

## What it does

The MCP server lets agents:

- Search Packrift products.
- Fetch product details by handle.
- Get live unit pricing for variants.
- Check live inventory for variants.
- Recommend packaging for item dimensions, weight, and use case.
- Estimate shipping for candidate carts.
- Create Packrift cart URLs.

## Install layout

- `manifest.json` — OtoDock catalog descriptor.
- `docker-compose.yml` — runs the published Packrift MCP container.

## Operator notes

- The public hosted endpoint is `https://mcp.packrift.com/mcp`; this catalog entry uses the container image for OtoDock environments that install MCPs from Docker assets.
- Configure a dedicated Shopify Admin API token in `SHOPIFY_PACKRIFT_TOKEN`. Do not commit tokens or `.env` files.
- `SHOPIFY_STORE_DOMAIN` defaults to `packrift.myshopify.com`.
- `STOREFRONT_DOMAIN` defaults to `packrift.com`.
- The container uses an in-memory cache; the production hosted endpoint remains the canonical Packrift MCP surface.
