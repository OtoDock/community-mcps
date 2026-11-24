"""The released platform reads this entry as the catalog publishes it: the
catalog reader takes the entry from this registry file, then the manifest
parser, the platform floor, the manifest hash and the install's compose
check, taken from the platform's code at the commit the publish ledger names
for 2.0.0 and run with the platform's own interpreter. The test skips, saying
why, when the platform checkout, its interpreter, the ledger or that commit
is not beside this repository."""

import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[1]
CATALOG = ENTRY.parent
PLATFORM = CATALOG.parent / "oto-dock"
PYTHON = PLATFORM / "proxy" / "venv" / "bin" / "python"
LEDGER = CATALOG.parent / "otodock-release-check" / "published.log"
RELEASE = "2.0.0"

# Run inside the platform's own proxy folder: what an install of RELEASE
# does with the entry, printed as one JSON line. The catalog reader's one
# request for the registry is answered with this repository's file, read the
# way the platform reads the catalog's entries.
_READ = r"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, ".")
from core.config import deployment
from services.community import community_catalog
from services.mcp import compose_rewrite
from services.mcp.mcp_manifest_parse import _parse_manifest

folder, registry_file = Path(sys.argv[1]), Path(sys.argv[2])

async def _this_registry(url, etag):
    assert url == community_catalog.REGISTRY_RAW_URL
    return json.loads(registry_file.read_text()), None, 200
community_catalog._http_get_json = _this_registry
registry = asyncio.run(community_catalog.fetch_registry())
entry = {m.get("name"): m for m in registry.get("mcps", [])}[folder.name]
manifest = _parse_manifest(folder / "manifest.json")
deployment.in_docker_compose = lambda: True
compose_rewrite.check_pull_compose(manifest)
community_catalog.require_platform_compat(entry["name"], entry["platform_min_version"])
print(json.dumps({
    "entry": entry,
    "name": manifest.name, "version": manifest.version, "image": manifest.server.image,
    "floor_ok": community_catalog.platform_version_ok(entry["platform_min_version"]),
    "hash": community_catalog.normalized_manifest_hash(json.loads((folder / "manifest.json").read_text())),
}))
"""


def _release_commit() -> str:
    if not LEDGER.is_file():
        pytest.skip("the publish ledger is not beside this repository")
    for line in LEDGER.read_text().splitlines():
        if f"publish OtoDock {RELEASE} " in line:
            return line.split()[1]
    pytest.skip(f"the publish ledger names no {RELEASE}")


def test_the_released_platform_reads_the_entry_as_published(tmp_path):
    if not (PLATFORM / ".git").exists() or not PYTHON.is_file():
        pytest.skip("the platform checkout and its interpreter are not beside this repository")
    commit = _release_commit()
    archive = tmp_path / "platform.tar"
    with archive.open("wb") as out:
        made = subprocess.run(["git", "-C", str(PLATFORM), "archive", commit, "proxy", "VERSIONS.md"],
                              stdout=out, stderr=subprocess.PIPE, check=False)
    if made.returncode:
        pytest.skip(f"the platform checkout lacks {RELEASE}'s commit: {made.stderr.decode().strip()[:120]}")
    platform = tmp_path / "platform"
    with tarfile.open(archive) as tar:
        tar.extractall(platform, filter="data")
    folder = tmp_path / ENTRY.name
    shutil.copytree(ENTRY, folder, ignore=shutil.ignore_patterns("__tests__", "__pycache__", ".venv"))
    entry = next(e for e in json.loads((CATALOG / "registry.json").read_text())["mcps"] if e["name"] == ENTRY.name)
    read = subprocess.run([str(PYTHON), "-I", "-c", _READ, str(folder), str(CATALOG / "registry.json")],
                          cwd=platform / "proxy", capture_output=True, text=True, timeout=120, check=False)
    assert read.returncode == 0, read.stderr[-2000:]
    seen = json.loads(read.stdout.strip().splitlines()[-1])
    manifest = json.loads((ENTRY / "manifest.json").read_text())
    assert seen == {"entry": entry, "name": entry["name"], "version": manifest["version"],
                    "image": manifest["server"]["image"], "floor_ok": True, "hash": entry["manifest_hash"]}
    assert re.fullmatch(r"\d+\.\d+\.\d+", entry["platform_min_version"])
