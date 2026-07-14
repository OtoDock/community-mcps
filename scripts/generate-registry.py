#!/usr/bin/env python3
"""Generate registry.json for the OtoDock community MCPs catalog.

Walks every `<mcp>/manifest.json`, validates required fields, and emits a
top-level `registry.json` consumed by the OtoDock platform's Browse Community
MCPs UI.

Usage:
    python scripts/generate-registry.py            # write registry.json
    python scripts/generate-registry.py --check    # exit non-zero if stale

The script is intentionally dependency-free so CI doesn't need a venv.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "registry.json"
REGISTRY_VERSION = "1"
PLATFORM_MIN_VERSION = "1.0.0"

REQUIRED_MANIFEST_FIELDS = ("name", "label", "description", "version", "server")
ALLOWED_RUNTIMES = {"python", "node", "docker", "remote"}


def _iter_mcp_dirs() -> list[Path]:
    dirs = []
    for entry in sorted(REPO_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in {"scripts", "docs"}:
            continue
        if (entry / "manifest.json").is_file():
            dirs.append(entry)
    return dirs


def _manifest_hash(manifest: dict) -> str:
    """Stable hash of a manifest, ignoring the two fields the platform pins
    locally on install (`version` + `server.source`).

    CONTRACT — must stay byte-identical to the platform's
    ``proxy/services/community_catalog.normalized_manifest_hash``: the platform
    compares this against the same hash of each install's manifest to detect
    integration changes ("re-converge this MCP"). Dropping exactly version +
    source makes a freshly-installed (pinned) manifest hash-equal to its catalog
    source. The serialization is pinned (`sort_keys`, compact `separators`,
    `ensure_ascii=True`) so contributor formatting doesn't affect the hash.
    """
    m = copy.deepcopy(manifest)
    m.pop("version", None)
    server = m.get("server")
    if isinstance(server, dict):
        server.pop("source", None)
    blob = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _read_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: invalid JSON ({exc})")


def _runtime(server: dict) -> str | None:
    """Catalog runtime. ``server.runtime`` when declared (python/node/docker);
    remote MCPs (``source: remote:…``) run on vendor infra and omit it, so
    derive ``"remote"`` from the source prefix."""
    rt = server.get("runtime")
    if rt:
        return rt
    if str(server.get("source", "")).startswith("remote:"):
        return "remote"
    return None


def _validate(manifest: dict, mcp_dir: Path) -> None:
    missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in manifest]
    if missing:
        raise SystemExit(f"{mcp_dir.name}: missing required fields {missing}")
    runtime = _runtime(manifest["server"])
    if runtime not in ALLOWED_RUNTIMES:
        raise SystemExit(
            f"{mcp_dir.name}: server.runtime must be one of {sorted(ALLOWED_RUNTIMES)}, got {runtime!r}"
        )
    if not (mcp_dir / "README.md").is_file():
        raise SystemExit(f"{mcp_dir.name}: README.md missing")


def _directory_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {"node_modules", "venv", ".venv", "__pycache__", ".git"}]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _derive_tags(manifest: dict) -> list[str]:
    tags = set()
    label = manifest.get("label", "").lower()
    for token in label.split():
        if token.isalpha() and len(token) > 2:
            tags.add(token)
    runtime = manifest.get("server", {}).get("runtime")
    if runtime:
        tags.add(runtime)
    return sorted(tags)


def _entry_for_mcp(mcp_dir: Path) -> dict:
    manifest = _read_manifest(mcp_dir / "manifest.json")
    _validate(manifest, mcp_dir)
    server = manifest["server"]
    runtime = _runtime(server)
    requires_credentials = bool(
        manifest.get("credentials", {}).get("type") not in (None, "none")
        or manifest.get("instances", {}).get("fields")
    )
    assignment_mode = manifest.get("assignment_mode", "auto")
    has_icon = (mcp_dir / "icon.png").is_file()
    entry = {
        "name": manifest["name"],
        "label": manifest["label"],
        "description": manifest["description"],
        "category": manifest.get("category", "community"),
        "version": manifest["version"],
        "runtime": runtime,
        "source": server.get("source", ""),
        # node/python auto-update bound (PEP 440 specifier; "" = unbounded latest).
        "version_constraint": server.get("version_constraint", ""),
        # Hash of the integration manifest (minus the locally-pinned version+source);
        # lets the platform detect catalog changes and re-converge installs.
        "manifest_hash": _manifest_hash(manifest),
        "manifest_url": f"./{mcp_dir.name}/manifest.json",
        "readme_url": f"./{mcp_dir.name}/README.md",
        "icon_url": f"./{mcp_dir.name}/icon.png" if has_icon else None,
        "tags": _derive_tags(manifest),
        "author": manifest.get("author", "OtoDock"),
        "author_url": manifest.get("author_url", "https://github.com/OtoDock"),
        "license": manifest.get("license", "MIT"),
        "requires_credentials": requires_credentials,
        "requires_system_packages": manifest.get("requires_system_packages", []),
        "platform_min_version": manifest.get("platform_min_version", PLATFORM_MIN_VERSION),
        "assignment_mode": assignment_mode,
        "size_bytes": _directory_size(mcp_dir),
        "deprecated": bool(manifest.get("deprecated", False)),
        "patched": bool(manifest.get("patched", False)),
        "patch_note": manifest.get("patch_note"),
    }
    return entry


def _build_registry() -> dict:
    mcps = [_entry_for_mcp(d) for d in _iter_mcp_dirs()]
    return {
        "registry_version": REGISTRY_VERSION,
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform_min_version": PLATFORM_MIN_VERSION,
        "mcps": mcps,
    }


def _write(registry: dict, path: Path) -> None:
    text = json.dumps(registry, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if registry.json is stale instead of writing.",
    )
    args = parser.parse_args()

    registry = _build_registry()

    if args.check:
        if not REGISTRY_PATH.is_file():
            print("registry.json missing", file=sys.stderr)
            return 1
        existing = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        # Ignore updated_at when comparing — only structural diffs matter.
        existing.pop("updated_at", None)
        candidate = {**registry}
        candidate.pop("updated_at", None)
        if existing != candidate:
            print("registry.json is stale — run scripts/generate-registry.py", file=sys.stderr)
            return 1
        print(f"registry.json is up to date ({len(registry['mcps'])} MCPs)")
        return 0

    _write(registry, REGISTRY_PATH)
    print(f"Wrote {REGISTRY_PATH.relative_to(REPO_ROOT)} ({len(registry['mcps'])} MCPs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
