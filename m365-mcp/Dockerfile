# Microsoft 365 MCP — Docker wrap of softeria/ms-365-mcp-server.
#
# Upstream natively supports HTTP mode with per-request Authorization
# headers (no sidecar needed, unlike github-mcp). We disable the
# upstream's internal OAuth dynamic client registration so OtoDock owns
# the OAuth dance end-to-end.
#
# Tool filtering: ENABLED_TOOLS_FLAG is written by the platform when the admin
# sets a tool-filter regex. The
# ENTRYPOINT shell expansion inlines it into the argv so the upstream
# binary sees the --enabled-tools flag at startup.

FROM node:22.22.0-slim

ARG MS365_MCP_VERSION=0.128.2
ENV MCP_PORT=3000
ENV ENABLED_TOOLS_FLAG=""

RUN npm install -g @softeria/ms-365-mcp-server@${MS365_MCP_VERSION}

# Drop privileges for the runtime.
USER node

EXPOSE 3000
# Shell-form ENTRYPOINT lets ${MCP_PORT} and ${ENABLED_TOOLS_FLAG}
# expand from the container env. Empty ENABLED_TOOLS_FLAG just adds an
# extra space — harmless for argv parsing.
ENTRYPOINT ["sh", "-c", "exec ms-365-mcp-server --http ${MCP_PORT} --no-dynamic-registration ${ENABLED_TOOLS_FLAG}"]
