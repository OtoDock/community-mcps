"""Composition tool handlers: create / edit / validate / render."""

import base64
from pathlib import Path

from mcp.types import ImageContent, TextContent

import composition as comp_mod
import renderer
from shared import _normalize_operations, _notify_file_written, _resolve_path, _to_agents_relative


def _clip_label(clip: dict) -> str:
    for key in ("label", "src", "image", "fill"):
        v = clip.get(key)
        if v:
            return Path(str(v)).name if key in ("src", "image") else str(v)
    return "clip"


def _timeline_report(resolved: dict, media_info: dict) -> str:
    try:
        tl = comp_mod.compute_timeline(resolved, media_info)
    except comp_mod.CompositionError as exc:
        return f"timeline: unresolved ({exc})"
    lines = [f"timeline: {tl['duration']:.2f}s total"]
    clips = comp_mod.base_track(resolved)["clips"]
    for entry in tl["base"]:
        clip = clips[entry["index"]]
        trans = entry.get("transition")
        suffix = (f"  ← {trans['type']} {float(trans.get('duration', 0.5)):.2g}s"
                  if trans else "")
        lines.append(
            f"  [{entry['index']}] {_clip_label(clip):<28} "
            f"{entry['start']:>7.2f} → {entry['end']:>7.2f} "
            f"({entry['duration']:.2f}s){suffix}")
    return "\n".join(lines)


async def _validation_block(comp: dict) -> tuple[str, list[dict], dict, dict]:
    resolved, media_info, issues = await renderer.prepare(comp, _resolve_path)
    parts = [comp_mod.format_issues(issues)]
    if not any(i["level"] == "error" for i in issues):
        parts.append(_timeline_report(resolved, media_info))
    return "\n\n".join(parts), issues, resolved, media_info


async def handle_create_composition(args: dict):
    raw_path = args["path"]
    path = _resolve_path(raw_path)
    if Path(path).exists() and not args.get("create_new"):
        return (f"Error: {raw_path} already exists — pass create_new=true to "
                "overwrite, or use edit_composition to modify it")

    full = args.get("composition")
    if full is not None:
        if not isinstance(full, dict):
            return "Error: 'composition' must be an object"
        comp = dict(full)
        comp.setdefault("version", comp_mod.SCHEMA_VERSION)
        comp.setdefault("project", comp_mod.new_composition()["project"])
        comp.setdefault("tracks", [{"kind": "video", "clips": []}])
        comp.setdefault("audio_master", {"gain_db": 0, "loudnorm": True})
        comp.setdefault("captions", None)
    else:
        comp = comp_mod.new_composition(args.get("project"))

    comp_mod.save_composition(path, comp)
    await _notify_file_written(path)
    report, issues, _, _ = await _validation_block(comp)
    return (f"Created {_to_agents_relative(path)}\n\n{report}\n\n"
            "Next: add clips with edit_composition, then render_composition "
            "(mode=preview) and inspect with render_frames.")


async def handle_edit_composition(args: dict):
    raw_path = args["path"]
    path = _resolve_path(raw_path)
    comp = comp_mod.load_composition(path)
    operations = _normalize_operations(args.get("operations"))
    if not operations:
        return "Error: no operations given"
    comp, results = comp_mod.apply_operations(comp, operations)
    comp_mod.save_composition(path, comp)
    await _notify_file_written(path)
    report, issues, _, _ = await _validation_block(comp)
    return ("\n".join(results)
            + f"\n\nSaved {_to_agents_relative(path)}\n\n{report}")


async def handle_validate_composition(args: dict):
    path = _resolve_path(args["path"])
    comp = comp_mod.load_composition(path)
    report, _, _, _ = await _validation_block(comp)
    return report


async def handle_render_composition(args: dict):
    raw_path = args["path"]
    mode = args.get("mode", "preview")
    if mode not in ("preview", "final"):
        return "Error: mode must be 'preview' or 'final'"
    time_range = None
    rng = args.get("range")
    if rng:
        if isinstance(rng, dict):
            rng = [rng.get("start", 0), rng.get("end")]
        if (not isinstance(rng, (list, tuple)) or len(rng) != 2
                or rng[1] is None or float(rng[1]) <= float(rng[0])):
            return "Error: range must be [start, end] seconds with end > start"
        time_range = (float(rng[0]), float(rng[1]))

    crf = args.get("crf")
    if crf is not None:
        try:
            crf = int(crf)
        except (TypeError, ValueError):
            return "Error: crf must be an integer (x264 range 0-51; web ≈ 23-30)"
        if not 0 <= crf <= 51:
            return "Error: crf must be within the x264 range 0-51"

    try:
        result = await renderer.render_composition(
            raw_path, _resolve_path, mode=mode,
            output_path=args.get("output_path"), time_range=time_range,
            crf=crf)
    except ValueError as exc:
        return f"Error: {exc}"

    # Without this, satellite installs never receive the RENDER — the main
    # deliverable — and the advertised display_video step 400s on a missing
    # file (hit live 2026-07-20).
    await _notify_file_written(result["output"])

    lines = [
        f"Rendered ({result['mode']}): {_to_agents_relative(result['output'])}",
        f"{result['canvas']} @ {result['fps']:.3g} fps · "
        f"{result['duration']:.2f}s · {result['size_mb']} MB",
    ]
    if result["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  - {w['where']}: {w['message']}" for w in result["warnings"])
    if mode == "preview":
        lines.append("Verify with render_frames (cut points, captions, framing), "
                     "then render mode=final.")
    else:
        lines.append("Web-safe H.264/AAC with faststart — show the user with "
                     "display_video.")
    return "\n".join(lines)


async def handle_render_frames(args: dict):
    timestamps = args.get("timestamps") or []
    if not timestamps:
        return "Error: pass timestamps=[…] (seconds on the composition timeline)"
    if len(timestamps) > 16:
        return "Error: at most 16 timestamps per call"
    try:
        png, note = await renderer.render_frames(
            args["path"], _resolve_path,
            [float(t) for t in timestamps],
            columns=int(args.get("columns", 3)))
    except ValueError as exc:
        return f"Error: {exc}"
    return [
        ImageContent(type="image", data=base64.b64encode(png).decode(),
                     mimeType="image/png"),
        TextContent(type="text", text=note),
    ]
