"""Render orchestration: resolve → probe → validate → compile → ffmpeg.

Owns everything the pure compiler must not touch: path resolution (via the
proxy hook), probing, ASS/LUT staging into a per-render tmp dir (safe
filenames — no user-controlled characters ever reach the filtergraph), the
two-pass loudnorm, and frame extraction for visual QC.
"""

import asyncio
import io
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

import captions as captions_mod
import color as color_mod
import composition as comp_mod
import slowmo as slowmo_mod
import speedramp as speedramp_mod
import stab as stab_mod
from compiler import (compile_render, estimate_window_bytes, plan_segments,
                      window_pruned)
from fftools import FFmpegError, media_duration, probe, run_ffmpeg, stream_fps, video_stream, audio_stream
from shared import _notify_file_written, logger

PREVIEW_SHORT_SIDE = 540
FRAMES_SHORT_SIDE = 480

# Pruning slack around a rendered window: must cover the longest transition
# overlap plus preset edge styling so a clip that only contributes via a
# join at the window edge is never replaced by a fill.
WINDOW_PAD = 3.0


def _cgroup_memory_limit_bytes() -> float | None:
    """Container memory limit when one applies (cgroup v2, then v1); None when
    unlimited or unreadable. sysconf reports the HOST's RAM even inside a
    mem_limit'ed container, so the budget must honor the cgroup cap or
    windowed planning sizes windows the container cannot hold."""
    for path, unlimited in (
        ("/sys/fs/cgroup/memory.max", "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None),
    ):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == unlimited:
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        if val >= 1 << 60:  # v1 reports a huge sentinel when unlimited
            return None
        return float(val)
    return None


def _render_budget_bytes() -> float:
    """Per-render decoded-frame budget for plan_segments. Override with
    VIDEO_TOOLS_RENDER_BUDGET_MB; default 40% of physical RAM, bounded by
    the container's cgroup limit when one is set — the filtergraph pile is
    only part of the process (decoders, x264 and the canvas-side chains ride
    on top), and other services share the host."""
    env = os.environ.get("VIDEO_TOOLS_RENDER_BUDGET_MB")
    if env:
        try:
            return max(1, int(env)) * 1e6
        except ValueError:
            logger.warning(
                "ignoring non-integer VIDEO_TOOLS_RENDER_BUDGET_MB=%r", env)
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 4e9
    limit = _cgroup_memory_limit_bytes()
    if limit is not None:
        total = min(total, limit)
    return max(min(1.5e9, total * 0.6), total * 0.4)


def default_output_path(comp_path: str, mode: str) -> str:
    """myvideo.vproj.json → myvideo.mp4 / myvideo.preview.mp4 (sibling)."""
    p = Path(comp_path)
    stem = p.name
    for suffix in (".json", ".vproj"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    suffix = ".preview.mp4" if mode == "preview" else ".mp4"
    return str(p.parent / (stem + suffix))


async def probe_media_paths(paths: list[str]) -> dict:
    """path → {duration, has_audio, has_video, width, height, fps}."""
    info: dict[str, dict] = {}
    for path in paths:
        ext = Path(path).suffix.lower()
        if ext in (".json", ".srt", ".ass", ".cube"):
            continue
        try:
            raw = await probe(path)
        except FFmpegError as exc:
            info[path] = {"error": str(exc)}
            continue
        vs, austream = video_stream(raw), audio_stream(raw)
        entry = {
            "duration": media_duration(raw),
            "has_video": vs is not None,
            "has_audio": austream is not None,
        }
        if vs:
            entry.update({
                "width": vs.get("width"),
                "height": vs.get("height"),
                "fps": round(stream_fps(vs), 3),
            })
            # Alpha WebM (motion clips): the native vp9/vp8 decoder ignores
            # the alpha side-plane — the compiler must force libvpx to keep
            # transparency when compositing.
            codec = vs.get("codec_name")
            if (codec in ("vp9", "vp8")
                    and str(vs.get("tags", {}).get("alpha_mode", "")) == "1"):
                entry["alpha_codec"] = "libvpx-vp9" if codec == "vp9" else "libvpx"
            # Still images probe with tiny/zero durations.
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                entry["duration"] = 0.0
                entry["still"] = True
        info[path] = entry
    return info


async def prepare(comp: dict, resolve) -> tuple[dict, dict, list[dict]]:
    """Resolve every referenced path, probe media, validate.

    Returns ``(resolved_comp, media_info, issues)`` — the resolved copy is
    for compiling only; the user's composition file keeps original paths.
    """
    import copy

    resolved = copy.deepcopy(comp)
    mapping: dict[str, str] = {}
    problems: list[dict] = []
    for original in comp_mod.media_paths(comp):
        try:
            mapping[original] = resolve(original)
        except ValueError as exc:
            problems.append({"level": "error", "where": "paths", "message": str(exc)})

    def rewrite(clip: dict):
        for key in ("src", "image"):
            if clip.get(key) in mapping:
                clip[key] = mapping[clip[key]]
        mask = clip.get("mask")
        if isinstance(mask, dict) and mask.get("image") in mapping:
            mask["image"] = mapping[mask["image"]]
        col = clip.get("color")
        if isinstance(col, dict) and col.get("lut") in mapping:
            col["lut"] = mapping[col["lut"]]
        if isinstance(col, dict) and isinstance(col.get("match"), dict):
            for key in ("ref", "ramp_from", "ramp_to"):
                ref = col["match"].get(key)
                if isinstance(ref, str) and "@" in ref:
                    path, _, t = ref.rpartition("@")
                    if path in mapping:
                        col["match"][key] = f"{mapping[path]}@{t}"

    for track in resolved.get("tracks", []):
        for clip in track.get("clips", []) if isinstance(track, dict) else []:
            if isinstance(clip, dict):
                rewrite(clip)
    proj_color = resolved.get("project", {}).get("color")
    if isinstance(proj_color, dict) and proj_color.get("lut") in mapping:
        proj_color["lut"] = mapping[proj_color["lut"]]
    caps = resolved.get("captions")
    if isinstance(caps, dict) and caps.get("source") in mapping:
        caps["source"] = mapping[caps["source"]]

    media_info = await probe_media_paths(sorted(set(mapping.values())))
    for path, entry in media_info.items():
        if "error" in entry:
            problems.append({"level": "error", "where": "media",
                             "message": f"unreadable media '{path}': {entry['error']}"})

    issues = problems + comp_mod.validate(
        resolved,
        exists=lambda p: Path(p).exists(),
        media_info=media_info,
    )
    return resolved, media_info, issues


def _stage_luts(resolved: dict, tmp: Path, resolve) -> dict[str, str]:
    """Copy every referenced LUT to a safe tmp name → {ref: tmp path}."""
    refs: set[str] = set()
    for track in resolved.get("tracks", []):
        for clip in track.get("clips", []):
            col = clip.get("color")
            if isinstance(col, dict) and col.get("lut"):
                refs.add(col["lut"])
    proj_color = resolved.get("project", {}).get("color")
    if isinstance(proj_color, dict) and proj_color.get("lut"):
        refs.add(proj_color["lut"])
    luts: dict[str, str] = {}
    for i, ref in enumerate(sorted(refs)):
        src = color_mod.resolve_lut(ref, resolve if not Path(ref).is_absolute() else (lambda p: p))
        if not Path(src).exists():
            raise ValueError(f"LUT not found: {ref} → {src}")
        dst = tmp / f"lut{i}.cube"
        shutil.copyfile(src, dst)
        luts[ref] = str(dst)
    return luts


def _stage_captions(resolved: dict, tmp: Path, canvas: tuple[int, int]) -> str | None:
    caps = resolved.get("captions")
    if not caps:
        return None
    ass_text = captions_mod.build_ass(
        caps["source"],
        play_w=canvas[0],
        play_h=canvas[1],
        preset=caps.get("preset", captions_mod.DEFAULT_PRESET),
        position=caps.get("position", "lower_third"),
        font_size=caps.get("font_size"),
        highlight_color=caps.get("highlight_color"),
        uppercase=caps.get("uppercase"),
        max_words_per_cue=caps.get("max_words_per_cue"),
        offset=float(caps.get("offset", 0.0)),
    )
    path = tmp / "captions.ass"
    path.write_text(ass_text, encoding="utf-8")
    return str(path)


async def _prepare_stabilization(resolved: dict, media_info: dict, tmp: Path) -> None:
    """Vidstab pass 1 for every stabilized clip: reuse or create the .trf
    sidecar cache next to the source, stage a safe-named copy in the render
    tmp dir (user-controlled source names must never reach the filtergraph),
    and inject the transform params for the compiler (``_stab``)."""
    n = 0
    for track in resolved.get("tracks", []):
        if track.get("kind") not in ("video", "overlay"):
            continue
        for clip in track.get("clips", []):
            spec = clip.get("stabilize")
            if not spec or not clip.get("src"):
                continue
            params = stab_mod.spec_params(spec)
            cin = float(clip.get("in", 0.0))
            cout = clip.get("out")
            if cout is None:
                cout = float(media_info[clip["src"]]["duration"])
            staged = tmp / f"stab{n}.trf"
            n += 1
            hit, sidecar = await stab_mod.ensure_trf(
                clip["src"], (cin, float(cout)), params["shakiness"], str(staged))
            if not hit and sidecar:
                await _notify_file_written(sidecar)
            # shakiness rides along for downstream cache keys (the flow
            # mezzanine bakes the transform in — its identity includes it).
            clip["_stab"] = {"trf": str(staged),
                             "shakiness": params["shakiness"],
                             "smoothing": params["smoothing"],
                             "zoom": params["zoom"]}


async def _prepare_slowmo(resolved: dict, media_info: dict,
                          issues: list[dict]) -> None:
    """Flow slow-mo pre-pass. Must run AFTER ``_prepare_stabilization``:
    when a clip has both, the stabilization transform is baked INTO the
    mezzanine (stabilize first, then interpolate — synthesizing frames
    from shaky footage warps them) and ``_stab`` is consumed here so the
    graph doesn't apply it twice."""
    fps_t = float(resolved["project"].get("fps", 30))
    built = reused = 0
    for track in resolved.get("tracks", []):
        if track.get("kind") not in ("video", "overlay"):
            continue
        for clip in track.get("clips", []):
            if not clip.get("src"):
                continue
            speed = float(clip.get("speed", 1.0))
            interp = clip.get("interpolate")
            if speed >= 1.0 or interp not in ("flow", "blend"):
                continue
            mi = media_info.get(clip["src"]) or {}
            src_fps = float(mi.get("fps") or 0)
            if slowmo_mod.native_sufficient(src_fps, speed, fps_t):
                issues.append({
                    "level": "warning", "where": "slowmo",
                    "message": (f"'{clip['src']}' at {speed}x: {src_fps:.0f} fps "
                                "source covers the slow motion natively — no "
                                "interpolation needed (best quality)")})
                continue
            if interp != "flow":
                continue  # blend is inlined by the compiler
            cin = float(clip.get("in", 0.0))
            cout = clip.get("out")
            if cout is None:
                cout = float(mi.get("duration", 0.0))
            st = clip.pop("_stab", None)
            stab_filters = stab_mod.transform_filters(st["trf"], st) if st else None
            mezz, hit = await slowmo_mod.ensure_mezzanine(
                clip["src"], (cin, float(cout)), speed, fps_t,
                stab_filters=stab_filters, stab_params=st)
            built += 0 if hit else 1
            reused += 1 if hit else 0
            clip["_slomo"] = {"src": mezz}
    if built or reused:
        issues.append({
            "level": "warning", "where": "slowmo",
            "message": (f"flow slow-motion mezzanine: {built} built, "
                        f"{reused} reused from cache")})


async def _prepare_match(resolved: dict, media_info: dict, tmp: Path) -> None:
    """match_color pre-pass: sample target + reference frames (cv2), bake
    the Lab-affine .cube LUT(s) into the render tmp dir, inject `_match`
    for the compiler. Ramp mode bakes TWO LUTs (clip start matched to
    ramp_from's frame, clip end to ramp_to's) that the compiler dissolves
    between. Sampling reads the ORIGINAL source (stab/slow-mo don't move
    color), so ordering vs the other pre-passes doesn't matter."""
    import colormatch

    n = 0
    for track in resolved.get("tracks", []):
        if track.get("kind") != "video":
            continue
        for clip in track.get("clips", []):
            col = clip.get("color")
            match = col.get("match") if isinstance(col, dict) else None
            if not match or not clip.get("src"):
                continue
            src = clip["src"]
            cin = float(clip.get("in", 0.0))
            cout = clip.get("out")
            if cout is None:
                cout = float(media_info[src].get("duration", 0.0))
            cout = float(cout)
            strength = float(match.get("strength", 1.0))
            hi = max(cin, cout - 0.05)
            if match.get("ref") is not None:
                rp, rt = colormatch.parse_ref(match["ref"])
                tt = match.get("target_time")
                tt = (cin + cout) / 2 if tt is None else float(tt)
                cube = tmp / f"match{n}.cube"
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(tt, cin, hi),
                    rp, colormatch.sample_window(rt, 0.0, rt + 0.15),
                    str(cube), strength)
                clip["_match"] = {"cube": str(cube)}
            else:
                pa, ta = colormatch.parse_ref(match["ramp_from"])
                pb, tb = colormatch.parse_ref(match["ramp_to"])
                ca, cb = tmp / f"match{n}a.cube", tmp / f"match{n}b.cube"
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(cin + 0.05, cin, hi),
                    pa, colormatch.sample_window(ta, 0.0, ta + 0.15),
                    str(ca), strength)
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(cout - 0.05, cin, hi),
                    pb, colormatch.sample_window(tb, 0.0, tb + 0.15),
                    str(cb), strength)
                duration = (cout - cin) / float(clip.get("speed", 1.0))
                clip["_match"] = {"a": str(ca), "b": str(cb),
                                  "duration": duration}
            n += 1


def _input_args(inputs: list) -> list[str]:
    args: list[str] = []
    for opts, path in inputs:
        args.extend(opts)
        args.extend(["-i", path])
    return args


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


async def _measure_loudnorm(resolved: dict, media_info: dict, cfg: dict,
                            tmp: Path) -> str | None:
    """Pass 1: run the audio-only graph through loudnorm print_format=json,
    return the pass-2 filter string with measured_* values (linear mode).

    Returns ``None`` when the mix measures silent (``input_i`` = -inf or
    below the -70 LUFS floor): loudnorm rejects non-finite measured values
    outright (``Value -inf for parameter 'measured_I' out of range`` —
    ffmpeg exit 222, killing the whole render), and "normalizing" silence
    would only amplify the noise floor. Silent sources are normal — drone
    and phone footage often has no audio track at all."""
    plan = compile_render(resolved, media_info, mode="final", streams="a")
    measure = (f"loudnorm=I={cfg['i']}:TP={cfg['tp']}:LRA={cfg['lra']}"
               f":print_format=json")
    graph = plan.graph.replace("__LOUDNORM__", measure)
    graph_file = tmp / "graph_loudnorm.txt"
    graph_file.write_text(graph, encoding="utf-8")
    args = _input_args(plan.inputs) + [
        "-filter_complex_script", str(graph_file),
        "-map", f"[{plan.audio_label}]", "-f", "null", "-",
    ]
    _, stderr = await run_ffmpeg(args, timeout=900, heavy=False)
    m = None
    for m in _LOUDNORM_JSON.finditer(stderr):
        pass  # take the LAST json block (progress lines can contain braces)
    if not m:
        logger.warning("loudnorm pass 1 produced no measurement — falling back to single-pass")
        return f"loudnorm=I={cfg['i']}:TP={cfg['tp']}:LRA={cfg['lra']}"
    meas = json.loads(m.group(0))
    try:
        measured_i = float(meas["input_i"])
    except (KeyError, TypeError, ValueError):
        measured_i = float("-inf")
    if not math.isfinite(measured_i) or measured_i < -70.0:
        return None
    return (
        f"loudnorm=I={cfg['i']}:TP={cfg['tp']}:LRA={cfg['lra']}"
        f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
        f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
        f":offset={meas.get('target_offset', 0)}:linear=true"
    )


def _video_encode_args(encode_args: list[str]) -> list[str]:
    """The video-only slice of a plan's encode args — segment renders carry
    no audio; the final concat mux adds the separately-rendered track."""
    out: list[str] = []
    skip = False
    for a in encode_args:
        if skip:
            skip = False
            continue
        if a in ("-c:a", "-b:a"):
            skip = True
            continue
        out.append(a)
    return out


async def _render_segmented(resolved: dict, media_info: dict,
                            issues: list[dict], *, mode: str, out: str,
                            scale: float, tmp: Path,
                            captions_ass: str | None, luts: dict[str, str],
                            crf: int | None,
                            segments: list[tuple[float, float]]):
    """Low-RAM render: the timeline renders in windows split at bare cuts,
    each window compiled from a window-pruned sub-composition (far clips →
    fills, far overlays dropped) so only that window's media is opened and
    decoded, then the windows concat LOSSLESSLY (-c copy) and mux with the
    audio. Audio renders in ONE cheap full-timeline pass — audio frames
    are never the memory problem, and splitting the mix would break amix,
    ducking and two-pass loudnorm semantics. Every window recomputes the
    identical timeline (fills preserve every duration and transition
    offset), so frame pts partition exactly at the window edges: the
    concat is frame-exact and cuts stay cuts. Returns the last window's
    RenderPlan (canvas/fps are identical across windows)."""
    aplan = compile_render(resolved, media_info, mode=mode, streams="a")
    agraph = aplan.graph
    if aplan.loudnorm:
        ln = await _measure_loudnorm(resolved, media_info, aplan.loudnorm, tmp)
        if ln is None:
            agraph = agraph.replace("__LOUDNORM__", "anull")
            issues.append({
                "level": "warning", "where": "audio",
                "message": "mix is silent — loudness normalization skipped",
            })
        else:
            agraph = agraph.replace("__LOUDNORM__", ln)
    graph_file = tmp / "graph_audio.txt"
    graph_file.write_text(agraph, encoding="utf-8")
    audio_path = tmp / "audio.m4a"
    await run_ffmpeg(_input_args(aplan.inputs) + [
        "-filter_complex_script", str(graph_file),
        "-map", f"[{aplan.audio_label}]",
        "-c:a", "aac", "-b:a", "192k" if mode == "final" else "128k",
        str(audio_path)], timeout=900, heavy=False)

    plan = None
    seg_files: list[Path] = []
    timeout = 1200 if mode == "preview" else 3600
    for n, (t0, t1) in enumerate(segments):
        logger.info("segmented render: window %d/%d (%.2f–%.2fs)",
                    n + 1, len(segments), t0, t1)
        pruned = window_pruned(resolved, media_info, t0, t1)
        plan = compile_render(
            pruned, media_info, mode=mode, canvas_scale=scale,
            time_range=(t0, t1), captions_ass=captions_ass, luts=luts,
            crf=crf, streams="v")
        graph_file = tmp / f"graph_seg{n}.txt"
        graph_file.write_text(plan.graph, encoding="utf-8")
        seg_out = tmp / f"seg{n}.mp4"
        await run_ffmpeg(_input_args(plan.inputs) + [
            "-filter_complex_script", str(graph_file),
            "-map", f"[{plan.video_label}]",
            *_video_encode_args(plan.encode_args), str(seg_out)],
            timeout=timeout)
        seg_files.append(seg_out)

    concat_list = tmp / "segments.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in seg_files),
                           encoding="utf-8")
    await run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-movflags", "+faststart", out],
        timeout=600, heavy=False)
    return plan


async def render_composition(
    comp_path: str,
    resolve,
    mode: str = "preview",
    output_path: str | None = None,
    time_range: tuple[float, float] | None = None,
    crf: int | None = None,
) -> dict:
    """Full render pipeline for a composition file. Returns a result summary
    dict; raises ValueError (validation) or FFmpegError (render)."""
    comp = comp_mod.load_composition(resolve(comp_path))
    resolved, media_info, issues = await prepare(comp, resolve)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ValueError("Composition has errors — fix them first:\n"
                         + comp_mod.format_issues(issues))

    # Ramp expansion BEFORE the pre-passes: stabilization and slow-mo must
    # see the constant-speed segments (slow ones route through the
    # mezzanine like any flow clip).
    for note in speedramp_mod.ramp_notes(resolved):
        issues.append({"level": "warning", "where": "speed_ramp",
                       "message": note})
    resolved = speedramp_mod.expand_composition(resolved, media_info)

    out = resolve(output_path) if output_path else resolve(default_output_path(comp_path, mode))

    proj = resolved["project"]
    scale = 1.0
    if mode == "preview":
        short = min(int(proj["width"]), int(proj["height"]))
        scale = min(1.0, PREVIEW_SHORT_SIDE / short)

    tmp = Path(tempfile.mkdtemp(prefix="vt-render-"))
    try:
        # Canvas must be known before ASS generation → compute like compiler.
        w = int(round(int(proj["width"]) * scale / 2) * 2)
        h = int(round(int(proj["height"]) * scale / 2) * 2)
        captions_ass = _stage_captions(resolved, tmp, (w, h))
        luts = _stage_luts(resolved, tmp, resolve)
        await _prepare_stabilization(resolved, media_info, tmp)
        await _prepare_slowmo(resolved, media_info, issues)
        await _prepare_match(resolved, media_info, tmp)

        segments = []
        if time_range is None:
            budget = _render_budget_bytes()
            segments = plan_segments(resolved, media_info, budget)
            if not segments:
                total = comp_mod.compute_timeline(resolved,
                                                  media_info)["duration"]
                est = estimate_window_bytes(resolved, media_info, 0.0, total)
                if est > budget:
                    issues.append({
                        "level": "warning", "where": "render",
                        "message": (f"estimated decode footprint "
                                    f"{est / 1e6:.0f} MB exceeds the "
                                    f"{budget / 1e6:.0f} MB memory budget and "
                                    "the timeline cannot be windowed any "
                                    "finer — the render may need substantial "
                                    "RAM and can be killed by the "
                                    "container's memory cap"),
                    })

        if segments:
            plan = await _render_segmented(
                resolved, media_info, issues, mode=mode, out=out,
                scale=scale, tmp=tmp, captions_ass=captions_ass,
                luts=luts, crf=crf, segments=segments)
            issues.append({
                "level": "warning", "where": "render",
                "message": (f"rendered in {len(segments)} windows (split at "
                            f"cuts, plus synthetic in-clip points where "
                            f"needed) to stay inside the "
                            f"{budget / 1e6:.0f} MB memory budget "
                            "(VIDEO_TOOLS_RENDER_BUDGET_MB overrides)"),
            })
        else:
            # An explicit time_range compiles from a window-pruned comp:
            # far clips become fills, so their inputs are never opened —
            # a slice render must not cost the whole timeline's memory.
            comp_graph = resolved
            if time_range is not None:
                comp_graph = window_pruned(resolved, media_info,
                                           time_range[0], time_range[1],
                                           pad=WINDOW_PAD)
            plan = compile_render(
                comp_graph, media_info, mode=mode, output_path=out,
                canvas_scale=scale, time_range=time_range,
                captions_ass=captions_ass, luts=luts, crf=crf,
            )

            graph = plan.graph
            if plan.loudnorm:
                ln_filter = await _measure_loudnorm(resolved, media_info,
                                                    plan.loudnorm, tmp)
                if ln_filter is None:
                    graph = graph.replace("__LOUDNORM__", "anull")
                    issues.append({
                        "level": "warning", "where": "audio",
                        "message": "mix is silent — loudness normalization skipped",
                    })
                else:
                    graph = graph.replace("__LOUDNORM__", ln_filter)

            graph_file = tmp / "graph.txt"
            graph_file.write_text(graph, encoding="utf-8")
            args = _input_args(plan.inputs) + [
                "-filter_complex_script", str(graph_file),
                "-map", f"[{plan.video_label}]", "-map", f"[{plan.audio_label}]",
                *plan.encode_args, out,
            ]
            timeout = 1200 if mode == "preview" else 3600
            await run_ffmpeg(args, timeout=timeout)

        out_info = await probe(out)
        size_mb = Path(out).stat().st_size / 1e6
        result = {
            "output": out,
            "mode": mode,
            "duration": round(media_duration(out_info), 2),
            "canvas": f"{plan.canvas[0]}x{plan.canvas[1]}",
            "fps": plan.fps,
            "size_mb": round(size_mb, 2),
            "warnings": [i for i in issues if i["level"] == "warning"],
        }
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def render_frames(
    comp_path: str,
    resolve,
    timestamps: list[float],
    columns: int = 3,
) -> tuple[bytes, str]:
    """Render single frames from the composition at the given timestamps and
    assemble a labeled grid PNG (for the model's visual QC)."""
    from PIL import Image, ImageDraw

    comp = comp_mod.load_composition(resolve(comp_path))
    resolved, media_info, issues = await prepare(comp, resolve)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        raise ValueError("Composition has errors — fix them first:\n"
                         + comp_mod.format_issues(issues))
    resolved = speedramp_mod.expand_composition(resolved, media_info)

    proj = resolved["project"]
    short = min(int(proj["width"]), int(proj["height"]))
    scale = min(1.0, FRAMES_SHORT_SIDE / short)

    tmp = Path(tempfile.mkdtemp(prefix="vt-frames-"))
    try:
        w = int(round(int(proj["width"]) * scale / 2) * 2)
        h = int(round(int(proj["height"]) * scale / 2) * 2)
        captions_ass = _stage_captions(resolved, tmp, (w, h))
        luts = _stage_luts(resolved, tmp, resolve)
        await _prepare_stabilization(resolved, media_info, tmp)
        await _prepare_slowmo(resolved, media_info, issues)
        await _prepare_match(resolved, media_info, tmp)
        total = comp_mod.compute_timeline(resolved, media_info)["duration"]
        eps = 1.0 / float(resolved["project"].get("fps", 30))
        frames: list[tuple[float, Path]] = []
        for i, t in enumerate(timestamps):
            t = max(0.0, min(float(t), max(0.0, total - eps)))
            # Each frame compiles from a window-pruned comp: only the clips
            # around t are opened — a QC frame from a long timeline must not
            # decode (or buffer) the whole thing.
            plan = compile_render(
                window_pruned(resolved, media_info, t, t, pad=WINDOW_PAD),
                media_info, mode="preview", canvas_scale=scale,
                captions_ass=captions_ass, luts=luts, streams="v",
            )
            graph_t = (plan.graph
                       + f";\n[{plan.video_label}]trim=start={t:.6g},"
                         f"setpts=PTS-STARTPTS[vframe]")
            graph_file = tmp / f"graph_frame{i}.txt"
            graph_file.write_text(graph_t, encoding="utf-8")
            out_png = tmp / f"frame{i}.png"
            args = _input_args(plan.inputs) + [
                "-filter_complex_script", str(graph_file),
                "-map", "[vframe]", "-frames:v", "1", str(out_png),
            ]
            await run_ffmpeg(args, timeout=600, heavy=False)
            frames.append((t, out_png))

        cols = max(1, min(columns, len(frames)))
        rows = (len(frames) + cols - 1) // cols
        label_h = 22
        cell_w, cell_h = w, h + label_h
        grid = Image.new("RGB", (cols * cell_w, rows * cell_h), "#101010")
        draw = ImageDraw.Draw(grid)
        for i, (t, png) in enumerate(frames):
            img = Image.open(png).convert("RGB")
            cx, cy = (i % cols) * cell_w, (i // cols) * cell_h
            grid.paste(img, (cx, cy + label_h))
            draw.text((cx + 6, cy + 4), f"t={t:.2f}s", fill="#e0e0e0")
        buf = io.BytesIO()
        grid.save(buf, format="PNG")
        note = (f"{len(frames)} frame(s) at {w}x{h} "
                f"(timeline {total:.2f}s)")
        return buf.getvalue(), note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
