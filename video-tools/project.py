"""Composition tool handlers: create / edit / validate / render."""

import asyncio
import base64
import json
from pathlib import Path

from mcp.types import ImageContent, TextContent

import audiofx as audiofx_mod
import composition as comp_mod
import renderer
from shared import _normalize_operations, _notify_file_written, _resolve_path, _to_agents_relative, safe_kind


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
    path = await _resolve_path(raw_path, writing=True)
    if (await asyncio.to_thread(safe_kind, path)).kind != "missing" and not args.get("create_new"):
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

    await asyncio.to_thread(comp_mod.save_composition, path, comp)
    await _notify_file_written(path)
    report, issues, _, _ = await _validation_block(comp)
    return (f"Created {_to_agents_relative(path)}\n\n{report}\n\n"
            "Next: add clips with edit_composition, then render_composition "
            "(mode=preview) and inspect with render_frames.")


async def handle_edit_composition(args: dict):
    raw_path = args["path"]
    path = await _resolve_path(raw_path, writing=True)
    comp = await asyncio.to_thread(comp_mod.load_composition, path)
    operations = _normalize_operations(args.get("operations"))
    if not operations:
        return "Error: no operations given"
    comp, results = comp_mod.apply_operations(comp, operations)
    await asyncio.to_thread(comp_mod.save_composition, path, comp)
    await _notify_file_written(path)
    report, issues, _, _ = await _validation_block(comp)
    return ("\n".join(results)
            + f"\n\nSaved {_to_agents_relative(path)}\n\n{report}")


async def handle_validate_composition(args: dict):
    path = await _resolve_path(args["path"])
    comp = await asyncio.to_thread(comp_mod.load_composition, path)
    report, issues, resolved, media_info = await _validation_block(comp)
    if args.get("format") == "json":
        return json.dumps(_timeline_json(comp, resolved, media_info, issues))
    return report


def _timeline_json(comp: dict, resolved: dict, media_info: dict, issues: list) -> dict:
    """The computed timeline as data, for a page that draws it: every base
    clip with its place after the transitions, the other tracks' clips, the
    composition's own paths (never the container's)."""
    out: dict = {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}
    try:
        tl = comp_mod.compute_timeline(resolved, media_info)
    except comp_mod.CompositionError as exc:
        out["timeline_error"] = str(exc)
        return out
    clips = comp_mod.base_track(comp).get("clips", [])
    base = []
    for entry in tl["base"]:
        clip = clips[entry["index"]] if entry["index"] < len(clips) else {}
        base.append({**entry, "label": _clip_label(clip),
                     "src": clip.get("src") or clip.get("image") or "",
                     **{k: clip[k] for k in ("in", "out", "speed", "volume_db", "mute") if k in clip}})
    tracks = []
    # Durations are read off the resolved copy (its paths key the probes),
    # the paths shown off the composition.
    for track, rtrack in zip(comp.get("tracks", []), resolved.get("tracks", [])):
        if not isinstance(track, dict) or track.get("kind") == "video":
            continue
        rows = []
        for i, (c, rc) in enumerate(zip(track.get("clips", []), rtrack.get("clips", []))):
            if not isinstance(c, dict):
                continue
            try:
                duration = round(comp_mod.clip_duration(rc, media_info), 6)
            except (comp_mod.CompositionError, KeyError, TypeError, ValueError):
                duration = None
            rows.append({"index": i, "label": _clip_label(c), "src": c.get("src") or c.get("image") or "",
                         "start": float(c.get("start", 0.0)), "duration": duration})
        tracks.append({"kind": track.get("kind"), "clips": rows})
    out.update({"duration": tl["duration"], "base": base, "tracks": tracks})
    return out


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
        plan = await renderer.plan_render(
            raw_path, _resolve_path, mode=mode,
            output_path=args.get("output_path"), time_range=time_range,
            crf=crf)
    except ValueError as exc:
        return f"Error: {exc}"

    if args.get("background"):
        import jobs
        try:
            job = await jobs.start(plan, lambda result: _render_report(result, mode))
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps({"job_id": job.id, "state": job.state,
                           "output": _to_agents_relative(plan.out)})

    try:
        result = await renderer.run_render(plan)
    except ValueError as exc:
        return f"Error: {exc}"

    # Without this, satellite installs never receive the RENDER — the main
    # deliverable — and the advertised display_video step 400s on a missing
    # file (seen in production).
    await _notify_file_written(result["output"])
    return _render_report(result, mode)


def _render_report(result: dict, mode: str) -> str:
    """The answer of a finished render, in the call or for its job."""
    lines = [
        f"Rendered ({result['mode']}): {_to_agents_relative(result['output'])}",
        f"{result['canvas']} @ {result['fps']:.3g} fps · "
        f"{result['duration']:.2f}s · {result['size_mb']} MB",
    ]
    audio = result.get("audio")
    if audio:
        if audio.get("loudnorm"):
            lines.append("audio: " + audiofx_mod.loudnorm_summary(audio["loudnorm"]))
        d = audio.get("delivered")
        if d:
            lines.append(f"delivered: {d['integrated_lufs']:.1f} LUFS · "
                         f"{d['true_peak_dbtp']:+.1f} dBTP · LRA {d['lra']:.1f} LU "
                         "(measured on the encoded file)")
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


async def handle_render_status(args: dict):
    import jobs
    return json.dumps(await jobs.status(str(args.get("job_id") or "")))


async def handle_cancel_render(args: dict):
    import jobs
    return json.dumps(await jobs.cancel(str(args.get("job_id") or "")))
