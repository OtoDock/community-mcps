"""Serve camoufox's patched Firefox over Playwright's websocket protocol.

`launch_server` hands the Playwright driver package to camoufox's
launchServer.js, sends the launch options as one frame and keeps stdin open:
the Node process reads EOF as a shutdown request.
"""

from camoufox.server import launch_server

print("Launching camoufox server...", flush=True)

launch_server(
    headless=True,
    os="windows",
    humanize=True,
    port=3000,
    ws_path="connect",
)
