# Blender

Drive a running Blender on this machine via the official [Blender Lab MCP server](https://projects.blender.org/lab/blender_mcp): inspect the scene, run `bpy` Python, render.

| Field | Value |
|-------|-------|
| Manifest name | `blender` |
| Runtime | Python (stdio), device-local |
| Upstream | `git+https://projects.blender.org/lab/blender_mcp.git` at Git tag v1.0.3 (pinned to its commit) |
| Credentials | None (talks to the local Blender instance) |
| Assignment mode | per-agent (device capability: `app`) |
| Upstream project | [Blender](https://projects.blender.org/lab/blender_mcp) |
| Icon | none: Blender's logo policy limits third-party use to crediting Blender or linking to blender.org, with the third party's own branding more prominent |

## What it does

Connects to a running Blender instance on the same machine through the official Blender Lab MCP add-on. Tools cover scene inspection, running arbitrary `bpy` Python, and rendering.

## Requirements

Blender **5.1 or newer** with the official **MCP add-on** installed and enabled (one-time, by the user). The add-on comes from the Blender Lab extension repository. In Blender, open Preferences > Get Extensions > Repositories, add `https://lab.blender.org/`, then install and enable "MCP".

Dropping the zip from [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/) onto Blender twice does the same, and keeps update notifications. The first drop adds the repository, the second installs the add-on.

The platform installs the MCP **server** from git. The add-on is the in-Blender bridge the server talks to (default `127.0.0.1:9876`).

The background tools (`*_for_cli`) start their own Blender process. Set **Blender executable** (`BLENDER_PATH`, default `blender`) when Blender is not on the machine's `PATH`. Each user can override it for their own machine.

`execute_blender_code` and `execute_blender_code_for_cli` run any Python inside Blender. They follow the chat's permission mode like the other tools. The machine's App connectors grant is the consent: where it is on, a chat in Ask or Auto runs them without asking.
