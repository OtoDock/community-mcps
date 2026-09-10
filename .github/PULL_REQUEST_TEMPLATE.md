<!-- Thanks for the pull request. One entry per PR. CONTRIBUTING.md has the folder layout, the manifest reference and the review checklist this list mirrors. -->

## Entry

<!-- Which MCP server, new or updated, and what changes. For an update of a docker or git+ entry: the version bump and why. -->

## How it was tested

<!-- The OtoDock version you installed it on, and what you ran: the install from the catalog, the tools an agent called. -->

## Checklist

- [ ] `python scripts/generate-registry.py` was run and `registry.json` is committed.
- [ ] `manifest.json` follows CONTRIBUTING.md: path-bearing env vars through `path_env`, LLM-supplied paths through `tool_arg_paths`, every env var the source reads declared, no `OTO_*` variable defined, no reference to the master key.
- [ ] A Docker MCP ships a pre-built `server.image` whose tag equals `version`, with `url_template` set to `http://${docker_mcp_host}:${port}`.
- [ ] A billable upstream API has a `costs` block; OS packages the server needs are in `system_requirements`.
- [ ] No `.env` file, key or credential anywhere in the folder.
- [ ] `README.md` documents the tools, the credentials the server needs and what its path arguments expect.
- [ ] I have reviewed every line I submit, generated or not.
