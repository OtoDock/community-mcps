#!/usr/bin/env bash
# Launcher for workspace-mcp in single-user mode.
#
# The platform injects the connected account's address into this process as
# GOOGLE_EMAIL (the manifest's `account_credential_keys`); upstream reads
# USER_GOOGLE_EMAIL as the default for every tool's `user_google_email`
# (auth/service_decorator.py: a missing argument falls back to it). Mapping
# one onto the other lets every caller omit the parameter — an agent in a
# chat or a task, and an app button fired headlessly — instead of each of
# them having to know and repeat the address. A caller that passes it still
# wins, as before. Nothing else about the launch changes.
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${USER_GOOGLE_EMAIL:-}" ] && [ -n "${GOOGLE_EMAIL:-}" ]; then
  export USER_GOOGLE_EMAIL="${GOOGLE_EMAIL}"
fi

exec "${BASE}/venv/bin/workspace-mcp" --single-user --transport stdio
