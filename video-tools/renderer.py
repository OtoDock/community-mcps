"""Render orchestration: resolve → probe → validate → compile → ffmpeg.

Owns everything the pure compiler must not touch: path resolution (via the
proxy hook), probing, ASS/LUT staging into a per-render tmp dir (safe
filenames — no user-controlled characters ever reach the filtergraph), the
two-pass loudnorm, and frame extraction for visual QC.
"""

import asyncio
import contextlib
import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import audiofx as audiofx_mod
import captions as captions_mod
import color as color_mod
import composition as comp_mod
import slowmo as slowmo_mod
import speedramp as speedramp_mod
import stab as stab_mod
from compiler import (compile_render, estimate_window_bytes, plan_segments,
                      window_pruned)
from fftools import (
    FFmpegError,
    audio_stream,
    media_duration,
    probe,
    render_budget_bytes,
    run_ffmpeg,
    stream_color,
    stream_fps,
    video_stream,
)
from shared import _notify_file_written, logger

PREVIEW_SHORT_SIDE = 540
FRAMES_SHORT_SIDE = 480
# How long one render's encode may run: a preview, a final render. The
# manifest's limit for one call (``server.tool_timeout_sec``) is at least the
# final's, so an engine never ends a call the renderer still runs.
PREVIEW_TIMEOUT_S = 1200
FINAL_TIMEOUT_S = 3600

# Pruning slack around a rendered window: must cover the longest transition
# overlap plus preset edge styling so a clip that only contributes via a
# join at the window edge is never replaced by a fill.
WINDOW_PAD = 3.0


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
                # Stream colour tags (VUI/colr) drive the compiler's head
                # tag: only fields the probe leaves unknown get a default.
                "color": stream_color(vs),
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
    # The renderer's own keys (``_slomo``, ``_stab``, ``_match``, set below
    # for the files it staged) never come from a stored composition: each
    # names a file no session judged. Dropped from the copy and refused.
    for ti, track in enumerate(resolved.get("tracks", [])):
        for ci, clip in enumerate(track.get("clips", []) if isinstance(track, dict) else []):
            own = sorted(k for k in clip if isinstance(k, str) and k.startswith("_")) \
                if isinstance(clip, dict) else []
            for key in own:
                del clip[key]
            if own:
                problems.append({"level": "error", "where": f"tracks[{ti}].clips[{ci}]",
                                 "message": f"keys that start with an underscore are not allowed: {own}"})
    for original in comp_mod.media_paths(comp):
        try:
            mapping[original] = resolve(original)
        except ValueError as exc:
            problems.append({"level": "error", "where": "paths", "message": str(exc)})
    # A caption font is a file the session names too: judged like the media,
    # and asked to exist only once resolved.
    caps_in = comp.get("captions")
    font_file = caps_in.get("font_file") if isinstance(caps_in, dict) else None
    font_resolved = None
    if isinstance(font_file, str) and font_file:
        try:
            font_resolved = resolve(font_file)
        except ValueError as exc:
            problems.append({"level": "error", "where": "captions", "message": str(exc)})

    def rewrite(clip: dict):
        for key in ("src", "image"):
            if clip.get(key) in mapping:
                clip[key] = mapping[clip[key]]
        mask = clip.get("mask")
        if isinstance(mask, dict) and mask.get("image") in mapping:
            mask["image"] = mapping[mask["image"]]
        col = clip.get("color")
        if isinstance(col, dict):
            color_mod.rewrite_lut_refs(col, mapping)
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
    if isinstance(proj_color, dict):
        color_mod.rewrite_lut_refs(proj_color, mapping)
    caps = resolved.get("captions")
    if isinstance(caps, dict) and caps.get("source") in mapping:
        caps["source"] = mapping[caps["source"]]
    if isinstance(caps, dict) and font_resolved:
        caps["font_file"] = font_resolved

    media_info = await probe_media_paths(sorted(set(mapping.values())))
    for path, entry in media_info.items():
        if "error" in entry:
            problems.append({"level": "error", "where": "media",
                             "message": f"unreadable media '{path}': {entry['error']}"})

    # A path the platform refused keeps its raw text, and the container sees
    # every agent's folder: only a judged place is asked whether it exists.
    judged = set(mapping.values()) | ({font_resolved} if font_resolved else set())
    issues = problems + comp_mod.validate(
        resolved,
        exists=lambda p: p in judged and Path(p).exists(),
        media_info=media_info,
    )
    return resolved, media_info, issues


def _stage_luts(resolved: dict, tmp: Path, resolve) -> dict[str, str]:
    """Stage every referenced LUT under a safe tmp name → {lut_key: tmp
    path}. A chain entry with strength < 1 stages a copy blended with
    identity (color.blend_cube) — the mix costs nothing at render time."""
    entries: set[tuple[str, float]] = set()

    def collect(spec):
        if isinstance(spec, dict) and spec.get("lut") is not None:
            entries.update(color_mod.lut_entries(spec))

    for track in resolved.get("tracks", []):
        for clip in track.get("clips", []):
            collect(clip.get("color"))
    collect(resolved.get("project", {}).get("color"))
    luts: dict[str, str] = {}
    for i, (ref, strength) in enumerate(sorted(entries)):
        src = color_mod.resolve_lut(ref, resolve if not Path(ref).is_absolute() else (lambda p: p))
        if not Path(src).exists():
            raise ValueError(f"LUT not found: {ref} → {src}")
        dst = tmp / f"lut{i}.cube"
        if strength >= 1.0:
            shutil.copyfile(src, dst)
        else:
            color_mod.blend_cube(src, str(dst), strength)
        luts[color_mod.lut_key(ref, strength)] = str(dst)
    return luts


def _stage_captions(resolved: dict, tmp: Path, canvas: tuple[int, int],
                    resolve, issues: list | None = None
                    ) -> tuple[str | None, str | None]:
    """→ (ASS path, fontsdir). A `font_file` is copied under a safe name
    into the render tmp (user-controlled names never reach the filtergraph)
    and the Style line carries the family the FILE declares — libass
    matches fontsdir faces by that name, not by path."""
    caps = resolved.get("captions")
    if not caps:
        return None, None
    fonts_dir = None
    family = caps.get("font")
    font_file = caps.get("font_file")
    if font_file:
        src = resolve(font_file)
        fonts_dir = tmp / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        staged = fonts_dir / ("caption" + Path(src).suffix.lower())
        shutil.copyfile(src, staged)
        declared = captions_mod.font_file_family(str(staged))
        if family and family.strip().lower() != declared.lower() and issues is not None:
            issues.append({
                "level": "warning", "where": "captions",
                "message": (f"font_file declares family '{declared}' — used "
                            f"instead of font '{family}'"),
            })
        family = declared
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
        font=family,
        bold=caps.get("bold"),
        italic=bool(caps.get("italic", False)),
    )
    path = tmp / "captions.ass"
    path.write_text(ass_text, encoding="utf-8")
    return str(path), (str(fonts_dir) if fonts_dir else None)


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
    """match_color pre-pass: sample target + reference frames (ffmpeg, in
    the RGB the chain's head tag implies), bake the quantile-curve .cube
    LUT(s) into the render tmp dir, inject `_match` for the compiler. Ramp
    mode bakes TWO LUTs (clip start matched to ramp_from's frame, clip end
    to ramp_to's) that the compiler dissolves between. Sampling reads the
    ORIGINAL source (stab/slow-mo don't move color), so ordering vs the
    other pre-passes doesn't matter."""
    import colormatch

    def head(path: str, spec=None) -> str:
        mi = media_info.get(path) or {}
        return color_mod.tag_filter(
            color_mod.head_tags(spec, mi.get("color"), mi.get("height")))

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
            src_head = head(src, col)
            if match.get("ref") is not None:
                rp, rt = colormatch.parse_ref(match["ref"])
                tt = match.get("target_time")
                tt = (cin + cout) / 2 if tt is None else float(tt)
                cube = tmp / f"match{n}.cube"
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(tt, cin, hi),
                    rp, colormatch.sample_window(rt, 0.0, rt + 0.15),
                    str(cube), strength,
                    target_head=src_head, ref_head=head(rp))
                clip["_match"] = {"cube": str(cube)}
            else:
                pa, ta = colormatch.parse_ref(match["ramp_from"])
                pb, tb = colormatch.parse_ref(match["ramp_to"])
                ca, cb = tmp / f"match{n}a.cube", tmp / f"match{n}b.cube"
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(cin + 0.05, cin, hi),
                    pa, colormatch.sample_window(ta, 0.0, ta + 0.15),
                    str(ca), strength,
                    target_head=src_head, ref_head=head(pa))
                await asyncio.to_thread(
                    colormatch.generate_match_lut,
                    src, colormatch.sample_window(cout - 0.05, cin, hi),
                    pb, colormatch.sample_window(tb, 0.0, tb + 0.15),
                    str(cb), strength,
                    target_head=src_head, ref_head=head(pb))
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


async def _measure_loudnorm(resolved: dict, media_info: dict, cfg: dict,
                            tmp: Path) -> tuple[str | None, dict | None]:
    """Pass 1: run the audio-only graph through loudnorm print_format=json,
    return the mode's pass-2 chain (audiofx.loudnorm_pass2) and its report.

    Returns ``(None, None)`` when the mix measures silent (``input_i`` =
    -inf or below the -70 LUFS floor): loudnorm rejects non-finite measured
    values outright (``Value -inf for parameter 'measured_I' out of range``
    — ffmpeg exit 222, killing the whole render), and "normalizing" silence
    would only amplify the noise floor. Silent sources are normal — drone
    and phone footage often has no audio track at all."""
    plan = compile_render(resolved, media_info, mode="final", streams="a")
    graph = plan.graph.replace("__LOUDNORM__",
                               audiofx_mod.loudnorm_measure_filter(cfg))
    graph_file = tmp / "graph_loudnorm.txt"
    graph_file.write_text(graph, encoding="utf-8")
    args = _input_args(plan.inputs) + [
        "-filter_complex_script", str(graph_file),
        "-map", f"[{plan.audio_label}]", "-f", "null", "-",
    ]
    _, stderr = await run_ffmpeg(args, timeout=900, heavy=False)
    meas = audiofx_mod.parse_loudnorm_json(stderr)
    if not meas:
        logger.warning("loudnorm pass 1 produced no measurement — falling back to single-pass")
        return (f"loudnorm=I={cfg['i']}:TP={cfg['tp']}:LRA={cfg['lra']}"
                f",aresample={audiofx_mod.CHAIN_RATE}"), None
    if audiofx_mod.measured_is_silent(meas):
        return None, None
    return audiofx_mod.loudnorm_pass2(cfg, meas)


def _apply_loudnorm(graph: str, ln: str | None, report: dict | None,
                    issues: list[dict]) -> str:
    """Substitute the pass-2 chain into a graph and record what it does."""
    if ln is None:
        issues.append({
            "level": "warning", "where": "audio",
            "message": "mix is silent — loudness normalization skipped",
        })
        return graph.replace("__LOUDNORM__", "anull")
    warning = audiofx_mod.loudnorm_warning(report) if report else None
    if warning:
        issues.append({"level": "warning", "where": "audio", "message": warning})
    return graph.replace("__LOUDNORM__", ln)


async def _measure_delivered(path: str) -> dict | None:
    """R128 numbers of the file that actually landed on disk — the codec
    adds up to ~1 dB of true-peak overshoot above the PCM ceiling, so the
    render report carries what a platform will measure, not the target."""
    try:
        _, stderr = await run_ffmpeg(
            ["-i", path, "-vn", "-af",
             audiofx_mod.loudnorm_measure_filter(audiofx_mod.loudnorm_config(True)),
             "-f", "null", "-"], timeout=600, heavy=False)
    except FFmpegError:
        return None
    meas = audiofx_mod.parse_loudnorm_json(stderr)
    if not meas or audiofx_mod.measured_is_silent(meas):
        return None
    try:
        return {"integrated_lufs": float(meas["input_i"]),
                "true_peak_dbtp": float(meas["input_tp"]),
                "lra": float(meas["input_lra"])}
    except (KeyError, TypeError, ValueError):
        return None


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
                            segments: list[tuple[float, float]],
                            captions_fontsdir: str | None = None,
                            at=None, checkpoint=None):
    """Low-RAM render: the timeline renders in windows split at bare cuts,
    each window compiled from a window-pruned sub-composition (far clips →
    fills, far overlays dropped) so only that window's media is opened and
    decoded, then the windows concat LOSSLESSLY (-c copy) and mux with the
    audio. Audio renders in ONE cheap full-timeline pass — audio frames
    are never the memory problem, and splitting the mix would break amix,
    ducking and two-pass loudnorm semantics. Every window recomputes the
    identical timeline, and frame pts partition exactly at the window edges
    ONLY because fills are frame-count pinned to the media rounding
    (trim=end_frame in _base_video_chain) and compute_timeline quantizes
    durations to the frame grid — an unpinned color source emits ceil(d·fps)
    frames, one more than media for off-grid durations, which drifted every
    window head onto bare background (found on a real 52 s production
    render, 2026-08). The concat is
    frame-exact and cuts stay cuts. Returns the last window's RenderPlan
    (canvas/fps are identical across windows)."""
    aplan = compile_render(resolved, media_info, mode=mode, streams="a")
    agraph = aplan.graph
    ln_report = None
    if aplan.loudnorm:
        ln, ln_report = await _measure_loudnorm(resolved, media_info,
                                                aplan.loudnorm, tmp)
        agraph = _apply_loudnorm(agraph, ln, ln_report, issues)
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
    timeout = PREVIEW_TIMEOUT_S if mode == "preview" else FINAL_TIMEOUT_S
    for n, (t0, t1) in enumerate(segments):
        logger.info("segmented render: window %d/%d (%.2f–%.2fs)",
                    n + 1, len(segments), t0, t1)
        pruned = window_pruned(resolved, media_info, t0, t1)
        plan = compile_render(
            pruned, media_info, mode=mode, canvas_scale=scale,
            time_range=(t0, t1), captions_ass=captions_ass, luts=luts,
            crf=crf, streams="v", captions_fontsdir=captions_fontsdir)
        graph_file = tmp / f"graph_seg{n}.txt"
        graph_file.write_text(plan.graph, encoding="utf-8")
        seg_out = tmp / f"seg{n}.mp4"
        await run_ffmpeg(_input_args(plan.inputs) + [
            "-filter_complex_script", str(graph_file),
            "-map", f"[{plan.video_label}]",
            *_video_encode_args(plan.encode_args), str(seg_out)],
            timeout=timeout, progress=at(t0) if at else None,
            claim=estimate_window_bytes(resolved, media_info, t0, t1))
        seg_files.append(seg_out)
        if checkpoint:
            checkpoint()

    concat_list = tmp / "segments.txt"
    concat_list.write_text("".join(f"file '{p}'\n" for p in seg_files),
                           encoding="utf-8")
    await run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-movflags", "+faststart", out],
        timeout=600, heavy=False)
    return plan, ln_report


@dataclass
class RenderPlan:
    """A render's fast half, done in the call: the composition loaded and
    judged, every path it names resolved for the session, the output judged
    as a write, captions and looks staged. ``run_render`` does the rest."""
    mode: str
    out: str
    resolved: dict
    media_info: dict
    issues: list
    tmp: Path
    scale: float
    time_range: tuple[float, float] | None
    crf: int | None
    captions_ass: str | None
    captions_fontsdir: str | None
    luts: dict
    duration: float


# The partial files of the renders that run now, never removed as stale.
_writing: set[str] = set()


def partial_path(out: str, token: str) -> str:
    """Where a render writes before its rename: a hidden name beside the
    output, with its extension (ffmpeg picks the format by it)."""
    p = Path(out)
    return str(p.parent / f".{p.stem}.partial-{token}{p.suffix}")


def _remove_stale_partials(out: str) -> None:
    """Partial files of this output that no render writes now (a tool that
    stopped mid-render left them)."""
    p = Path(out)
    for stale in p.parent.glob(f".{p.stem}.partial-*{p.suffix}"):
        if str(stale) not in _writing:
            with contextlib.suppress(OSError):
                stale.unlink()


async def plan_render(
    comp_path: str,
    resolve,
    mode: str = "preview",
    output_path: str | None = None,
    time_range: tuple[float, float] | None = None,
    crf: int | None = None,
) -> RenderPlan:
    """The fast half: raises ValueError (validation, a path the session may
    not use) before anything heavy starts."""
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

    out = resolve(output_path or default_output_path(comp_path, mode), writing=True)

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
        captions_ass, captions_fontsdir = _stage_captions(
            resolved, tmp, (w, h), resolve, issues)
        luts = _stage_luts(resolved, tmp, resolve)
        total = comp_mod.compute_timeline(resolved, media_info)["duration"]
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    duration = (time_range[1] - time_range[0]) if time_range else total
    return RenderPlan(mode=mode, out=out, resolved=resolved, media_info=media_info,
                      issues=issues, tmp=tmp, scale=scale, time_range=time_range, crf=crf,
                      captions_ass=captions_ass, captions_fontsdir=captions_fontsdir,
                      luts=luts, duration=max(duration, 0.001))


async def run_render(plan: RenderPlan, *, token: str = "", progress=None,
                     cancelled=None) -> dict:
    """The heavy half: the pre-passes, the encode into a partial file beside
    the output, its probe, and the rename. ``progress(percent)`` follows the
    encode; ``cancelled()`` is read between steps (a step in a thread runs
    to its end). Removes the partial file and the scratch folder whatever
    happens."""
    resolved, media_info, issues, tmp = plan.resolved, plan.media_info, plan.issues, plan.tmp
    mode, scale, time_range, crf = plan.mode, plan.scale, plan.time_range, plan.crf
    part = partial_path(plan.out, token or os.urandom(6).hex())
    _remove_stale_partials(plan.out)
    _writing.add(part)

    def checkpoint():
        if cancelled is not None and cancelled():
            raise asyncio.CancelledError()

    def at(offset: float):
        if progress is None:
            return None
        return lambda seconds: progress(min(99.0, 100.0 * (offset + seconds) / plan.duration))

    try:
        await _prepare_stabilization(resolved, media_info, tmp)
        checkpoint()
        await _prepare_slowmo(resolved, media_info, issues)
        checkpoint()
        await _prepare_match(resolved, media_info, tmp)
        checkpoint()

        ln_report = None
        # Planned against the whole budget: a render shares it only while
        # another heavy run goes beside it (fftools.heavy_run). No memory
        # warning: after a render that succeeded nothing is left to do, and
        # one the container's limit ends fails with fftools.OUT_OF_MEMORY.
        segments = (plan_segments(resolved, media_info, render_budget_bytes())
                    if time_range is None else [])

        if segments:
            render_plan, ln_report = await _render_segmented(
                resolved, media_info, issues, mode=mode, out=part,
                scale=scale, tmp=tmp, captions_ass=plan.captions_ass,
                luts=plan.luts, crf=crf, segments=segments,
                captions_fontsdir=plan.captions_fontsdir, at=at, checkpoint=checkpoint)
        else:
            # An explicit time_range compiles from a window-pruned comp:
            # far clips become fills, so their inputs are never opened —
            # a slice render must not cost the whole timeline's memory.
            comp_graph = resolved
            span = time_range or (0.0, plan.duration)
            if time_range is not None:
                comp_graph = window_pruned(resolved, media_info,
                                           time_range[0], time_range[1],
                                           pad=WINDOW_PAD)
            render_plan = compile_render(
                comp_graph, media_info, mode=mode, output_path=part,
                canvas_scale=scale, time_range=time_range,
                captions_ass=plan.captions_ass, luts=plan.luts, crf=crf,
                captions_fontsdir=plan.captions_fontsdir,
            )

            graph = render_plan.graph
            if render_plan.loudnorm:
                ln_filter, ln_report = await _measure_loudnorm(
                    resolved, media_info, render_plan.loudnorm, tmp)
                graph = _apply_loudnorm(graph, ln_filter, ln_report, issues)
            checkpoint()

            graph_file = tmp / "graph.txt"
            graph_file.write_text(graph, encoding="utf-8")
            args = _input_args(render_plan.inputs) + [
                "-filter_complex_script", str(graph_file),
                "-map", f"[{render_plan.video_label}]", "-map", f"[{render_plan.audio_label}]",
                *render_plan.encode_args, part,
            ]
            timeout = PREVIEW_TIMEOUT_S if mode == "preview" else FINAL_TIMEOUT_S
            await run_ffmpeg(args, timeout=timeout, progress=at(0.0),
                             claim=estimate_window_bytes(resolved, media_info, *span))

        checkpoint()
        out_info = await probe(part)
        size_mb = Path(part).stat().st_size / 1e6
        result = {
            "output": plan.out,
            "mode": mode,
            "duration": round(media_duration(out_info), 2),
            "canvas": f"{render_plan.canvas[0]}x{render_plan.canvas[1]}",
            "fps": render_plan.fps,
            "size_mb": round(size_mb, 2),
            "warnings": [i for i in issues if i["level"] == "warning"],
        }
        if mode == "final" and audio_stream(out_info) is not None:
            result["audio"] = {
                "loudnorm": ln_report,
                "delivered": await _measure_delivered(part),
            }
        os.replace(part, plan.out)
        return result
    finally:
        _writing.discard(part)
        with contextlib.suppress(OSError):
            os.unlink(part)
        shutil.rmtree(tmp, ignore_errors=True)


def discard(plan: RenderPlan) -> None:
    """A planned render that will not run: its scratch folder goes."""
    shutil.rmtree(plan.tmp, ignore_errors=True)


async def render_composition(
    comp_path: str,
    resolve,
    mode: str = "preview",
    output_path: str | None = None,
    time_range: tuple[float, float] | None = None,
    crf: int | None = None,
) -> dict:
    """Full render pipeline for a composition file, in the call. Returns a
    result summary dict; raises ValueError (validation) or FFmpegError
    (render)."""
    plan = await plan_render(comp_path, resolve, mode=mode, output_path=output_path,
                             time_range=time_range, crf=crf)
    return await run_render(plan)


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
        captions_ass, captions_fontsdir = _stage_captions(
            resolved, tmp, (w, h), resolve)
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
                captions_fontsdir=captions_fontsdir,
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
