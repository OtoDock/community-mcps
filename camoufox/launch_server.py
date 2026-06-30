import base64
import subprocess
from pathlib import Path

import orjson
from playwright._impl._driver import compute_driver_executable

from camoufox.server import get_nodejs, to_camel_case_dict
from camoufox.utils import launch_options

# Get launch config
config = launch_options(
    headless=True,
    os="windows",
    humanize=True,
    port=3000,
    ws_path="connect",
)

# Strip None values — Playwright's launchServer chokes on proxy: null
config = {k: v for k, v in config.items() if v is not None}

# Convert to camelCase for JS
data = orjson.dumps(to_camel_case_dict(config))

# Get Node.js binary bundled with Playwright
nodejs = get_nodejs()

# Find launchServer.js
from camoufox.pkgman import LOCAL_DATA
launch_script = LOCAL_DATA / "launchServer.js"

print("Launching camoufox server...", flush=True)

process = subprocess.Popen(
    [nodejs, str(launch_script)],
    cwd=Path(nodejs).parent / "package",
    stdin=subprocess.PIPE,
    text=True,
)

if process.stdin:
    process.stdin.write(base64.b64encode(data).decode())
    process.stdin.close()

process.wait()
