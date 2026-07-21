"""edit_video: one-shot operations on a single file, no composition needed.

Operations run sequentially through tmp intermediates; the pipeline stops at
the first failed op (later ops depend on earlier output — unlike document
edits, partial application would silently produce the wrong video).
"""

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

import audiofx as audiofx_mod
import captions as captions_mod
import slowmo as slowmo_mod
import stab as stab_mod
from fftools import FFmpegError, atempo_chain, audio_stream, media_duration, probe, run_ffmpeg, stream_fps, video_stream
from shared import _normalize_operations, _notify_file_written, _op_type, _resolve_path, _to_agents_relative

_ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]

_AUDIO_EXTS = {"wav": ".wav", "mp3": ".mp3", "aac": ".m4a", "flac": ".flac"}
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def _f(v) -> str:
    return f"{float(v):.6g}"


async def _step(args: list[str], timeout: float = 1800) -> None:
    await run_ffmpeg(args, timeout=timeout)


async def handle_edit_video(args: dict):
    src = _resolve_path(args["path"])
    if not Path(src).exists():
        return f"Error: file not found: {args['path']}"
    operations = _normalize_operations(args.get("operations"))
    if not operations:
        return ("Error: no operations given. Supported: trim, remove, crop, "
                "smart_reframe, stabilize, resize, speed, speed_ramp, fps, "
                "concat, extract_audio, replace_audio, enhance_audio, "
                "match_color, mute, volume, loudness_normalize, "
                "burn_subtitles, to_gif, to_webp")

    notes: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="vt-edit-"))
    try:
        current = src
        for i, op in enumerate(operations):
            kind = _op_type(op)
            handler = _OPS.get(kind)
            if handler is None:
                return _fail(notes, kind or "(missing type)",
                             f"unknown operation — valid: {', '.join(sorted(_OPS))}")
            try:
                current, note = await handler(current, tmp / f"step{i}", op)
                notes.append(f"ok: {kind}: {note}")
            except (FFmpegError, ValueError) as exc:
                return _fail(notes, kind, str(exc))

        out_ext = Path(current).suffix
        default_name = Path(src).stem + "_edited" + out_ext
        out_arg = args.get("output_path")
        out = _resolve_path(out_arg) if out_arg else str(Path(src).parent / default_name)
        if Path(out).suffix.lower() != out_ext.lower():
            out = str(Path(out).with_suffix(out_ext))
            notes.append(f"note: output extension adjusted to {out_ext}")
        if current == src:
            return _fail(notes, "pipeline", "nothing was produced")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(current, out)
        await _notify_file_written(out)

        try:
            info = await probe(out)
            dur = media_duration(info)
            size = Path(out).stat().st_size / 1e6
            summary = f"{dur:.2f}s · {size:.1f} MB"
        except FFmpegError:
            summary = "written"
        notes.append(f"saved: {_to_agents_relative(out)} ({summary})")
        notes.append("Show it to the user with display_video when appropriate.")
        return "\n".join(notes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fail(notes: list[str], kind: str, message: str) -> str:
    notes.append(f"error: {kind}: {message}")
    notes.append("Pipeline stopped — no output written.")
    return "\n".join(notes)


# ---------------------------------------------------------------------------
# Individual ops — each returns (output_path, note)
# ---------------------------------------------------------------------------


async def _op_trim(src: str, base, op: dict):
    start = float(op.get("start", 0))
    end = op.get("end")
    if end is None or float(end) <= start:
        raise ValueError("trim needs start < end (seconds)")
    out = str(base) + ".mp4"
    await _step(["-ss", _f(start), "-to", _f(end), "-i", src, *_ENCODE, out])
    return out, f"kept {start}-{end}s"


async def _op_remove(src: str, base, op: dict):
    start, end = float(op.get("start", -1)), float(op.get("end", -1))
    if not 0 <= start < end:
        raise ValueError("remove needs 0 ≤ start < end")
    info = await probe(src)
    has_audio = audio_stream(info) is not None
    out = str(base) + ".mp4"
    graph = (f"[0:v]select='not(between(t,{_f(start)},{_f(end)}))',"
             f"setpts=N/FRAME_RATE/TB[v]")
    maps = ["-map", "[v]"]
    if has_audio:
        graph += (f";[0:a]aselect='not(between(t,{_f(start)},{_f(end)}))',"
                  f"asetpts=N/SR/TB[a]")
        maps += ["-map", "[a]"]
    await _step(["-i", src, "-filter_complex", graph, *maps, *_ENCODE, out])
    return out, f"cut out {start}-{end}s"


async def _op_crop(src: str, base, op: dict):
    info = await probe(src)
    vs = video_stream(info)
    if vs is None:
        raise ValueError("no video stream to crop")
    W, H = int(vs["width"]), int(vs["height"])
    aspect = op.get("aspect")
    if aspect:
        m = re.fullmatch(r"(\d+):(\d+)", str(aspect))
        if not m:
            raise ValueError("aspect must look like '9:16'")
        aw, ah = int(m.group(1)), int(m.group(2))
        w = min(W, int(H * aw / ah) // 2 * 2)
        h = min(H, int(W * ah / aw) // 2 * 2)
        x, y = (W - w) // 2, (H - h) // 2
        note = f"center-cropped to {aspect} ({w}x{h})"
    else:
        try:
            w, h = int(op["width"]), int(op["height"])
        except KeyError:
            raise ValueError("crop needs width+height or aspect")
        x = int(op.get("x", (W - w) // 2))
        y = int(op.get("y", (H - h) // 2))
        note = f"cropped {w}x{h}+{x}+{y}"
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"crop={w}:{h}:{x}:{y}",
                 *_ENCODE, out])
    return out, note


async def _op_resize(src: str, base, op: dict):
    if op.get("short_side"):
        s = int(op["short_side"])
        vf = (f"scale=if(gt(iw\\,ih)\\,-2\\,{s}):if(gt(iw\\,ih)\\,{s}\\,-2)"
              f":flags=lanczos")
        note = f"short side → {s}px"
    else:
        w, h = op.get("width", -2), op.get("height", -2)
        if w == -2 and h == -2:
            raise ValueError("resize needs width, height, or short_side")
        vf = f"scale={int(w)}:{int(h)}:flags=lanczos"
        note = f"scaled to {w}x{h}"
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", vf, *_ENCODE, out])
    return out, note


async def _op_speed(src: str, base, op: dict):
    factor = float(op.get("factor", 0))
    if not 0.1 <= factor <= 4:
        raise ValueError("speed factor must be 0.1–4")
    interp = op.get("interpolate", "duplicate")
    if interp not in slowmo_mod.INTERP_MODES:
        raise ValueError(f"interpolate must be one of {slowmo_mod.INTERP_MODES}")
    info = await probe(src)
    has_audio = audio_stream(info) is not None
    vchain = [f"setpts=PTS/{_f(factor)}"]
    note = f"speed ×{factor}"
    if factor < 1 and interp != "duplicate":
        # No timeline here — retimed native frames ARE the output. If they
        # still play smoothly (≥24 fps effective, e.g. 60 fps shot at 0.5×)
        # nothing needs synthesizing; below that, interpolate back up to a
        # standard rate.
        fps = stream_fps(video_stream(info) or {}) or 30.0
        effective = fps * factor
        if effective >= 24.0:
            note += f" (native frames, {effective:.3g} fps — no interpolation needed)"
        else:
            target = min(fps, 30.0)
            chain = (slowmo_mod.FLOW_CHAIN if interp == "flow"
                     else slowmo_mod.BLEND_CHAIN)
            vchain.append(chain.format(fps=f"{target:.6g}"))
            note += f" ({interp}-interpolated to {target:.3g} fps)"
    graph = f"[0:v]{','.join(vchain)}[v]"
    maps = ["-map", "[v]"]
    if has_audio:
        chain = ",".join(atempo_chain(factor)) or "anull"
        graph += f";[0:a]{chain}[a]"
        maps += ["-map", "[a]"]
    out = str(base) + ".mp4"
    await _step(["-i", src, "-filter_complex", graph, *maps, *_ENCODE, out],
                timeout=3600)
    return out, note


async def _op_speed_ramp(src: str, base, op: dict):
    """Segmented speed ramp over the whole file (the composition clip field
    of the same name is the full-featured path — mezzanine slow-mo,
    transitions on the edges). Slow segments here duplicate frames; for
    flow-interpolated ramps, build a composition."""
    import speedramp as speedramp_mod

    ramp = {"from": op.get("from", 0), "to": op.get("to", 0),
            "curve": op.get("curve", "linear")}
    for k in ("from", "to"):
        if not 0.1 <= float(ramp[k]) <= 4:
            raise ValueError(f"speed_ramp '{k}' must be 0.1–4")
    if ramp["curve"] not in speedramp_mod.RAMP_CURVES:
        raise ValueError(f"curve must be one of {speedramp_mod.RAMP_CURVES}")

    info = await probe(src)
    dur = media_duration(info)
    if dur <= 0.2:
        raise ValueError("source too short to ramp")
    has_audio = audio_stream(info) is not None
    fps = stream_fps(video_stream(info) or {}) or 30.0

    speeds = speedramp_mod.segment_speeds(ramp)
    n = len(speeds)
    seg = dur / n
    parts, vlabels, alabels = [], [], []
    for i, s in enumerate(speeds):
        a, b = i * seg, (dur if i == n - 1 else (i + 1) * seg)
        parts.append(f"[0:v]trim=start={_f(a)}:end={_f(b)},"
                     f"setpts=(PTS-STARTPTS)/{_f(s)}[v{i}]")
        vlabels.append(f"[v{i}]")
        if has_audio:
            achain = ",".join(["asetpts=PTS-STARTPTS"] + atempo_chain(s))
            parts.append(f"[0:a]atrim=start={_f(a)}:end={_f(b)},{achain}[a{i}]")
            alabels.append(f"[a{i}]")
    # fps= after concat: the trim/setpts segments lose CFR metadata and the
    # variable frame spacing of slow segments plays back unevenly without it.
    parts.append("".join(vlabels)
                 + f"concat=n={n}:v=1:a=0,fps={_f(min(fps, 60.0))}[v]")
    maps = ["-map", "[v]"]
    if has_audio:
        parts.append("".join(alabels) + f"concat=n={n}:v=0:a=1[a]")
        maps += ["-map", "[a]"]
    out = str(base) + ".mp4"
    await _step(["-i", src, "-filter_complex", ";".join(parts), *maps,
                 *_ENCODE, out], timeout=3600)
    return out, ("speed ramp " + speedramp_mod.describe(ramp)
                 + ("" if min(speeds) >= 1.0 or stream_fps(video_stream(info) or {}) == 0
                    else "; slow segments duplicate frames — use a "
                         "composition with interpolate: flow for synthesis"))


async def _op_fps(src: str, base, op: dict):
    fps = float(op.get("fps", 0))
    if not 1 <= fps <= 120:
        raise ValueError("fps must be 1–120")
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"fps={_f(fps)}", *_ENCODE, out])
    return out, f"resampled to {fps} fps"


async def _op_concat(src: str, base, op: dict):
    others = [_resolve_path(p) for p in op.get("paths", [])]
    if not others:
        raise ValueError("concat needs paths=[…] to append")
    paths = [src] + others
    first = await probe(src)
    vs = video_stream(first)
    if vs is None:
        raise ValueError("no video stream")
    W, H = int(vs["width"]), int(vs["height"])
    fps = stream_fps(vs) or 30.0

    chains, pairs = [], []
    for i, p in enumerate(paths):
        info = await probe(p) if i else first
        chains.append(
            f"[{i}:v]fps={_f(fps)},scale={W}:{H}:force_original_aspect_ratio=decrease"
            f":flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,format=yuv420p[v{i}]")
        if audio_stream(info) is not None:
            chains.append(f"[{i}:a]aresample=48000,"
                          f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            dur = media_duration(info)
            chains.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{_f(dur)}[a{i}]")
        pairs.append(f"[v{i}][a{i}]")
    chains.append("".join(pairs) + f"concat=n={len(paths)}:v=1:a=1[v][a]")
    out = str(base) + ".mp4"
    args = []
    for p in paths:
        args += ["-i", p]
    args += ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
             *_ENCODE, out]
    await _step(args, timeout=3600)
    return out, f"concatenated {len(paths)} files at {W}x{H}"


async def _op_smart_reframe(src: str, base, op: dict):
    aspect = op.get("aspect", "9:16")
    m = re.fullmatch(r"(\d+):(\d+)", str(aspect))
    if not m:
        raise ValueError("aspect must look like '9:16'")
    info = await probe(src)
    vs = video_stream(info)
    if vs is None:
        raise ValueError("no video stream")
    W, H = int(vs["width"]), int(vs["height"])
    aw, ah = int(m.group(1)), int(m.group(2))
    w = min(W, int(H * aw / ah) // 2 * 2)
    h = min(H, int(W * ah / aw) // 2 * 2)
    if (w, h) == (W, H):
        raise ValueError(f"source is already {aspect} — nothing to reframe")

    import analysis
    import reframe
    # More sensitive than analyze_video's default 27: a missed cut blends two
    # framings into one averaged crop (bad), while an extra split just yields
    # another static segment (harmless).
    threshold = float(op.get("threshold", 15.0))
    shots = await asyncio.to_thread(analysis._detect_shots, src, threshold)
    if not shots:
        shots = [(0.0, media_duration(info))]
    segments = await asyncio.to_thread(
        reframe.plan_segments, src, shots, w, h, W, H)
    x_expr = reframe.step_expr(segments, "x")
    y_expr = reframe.step_expr(segments, "y")
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"crop={w}:{h}:x='{x_expr}':y='{y_expr}'",
                 *_ENCODE, out], timeout=3600)
    tracked = sum(1 for s in segments if s["faces"])
    return out, (f"reframed to {aspect} ({w}x{h}) — {len(segments)} shot(s), "
                 f"{tracked} subject-tracked, others center-cropped")


async def _op_stabilize(src: str, base, op: dict):
    params = stab_mod.spec_params(op)
    trf = str(base) + ".trf"
    hit, _ = await stab_mod.ensure_trf(src, None, params["shakiness"], trf)
    out = str(base) + ".mp4"
    vf = ",".join(stab_mod.transform_filters(trf, params))
    await _step(["-i", src, "-vf", vf, *_ENCODE, out], timeout=3600)
    return out, (f"stabilized ({op.get('strength', 'medium')}, "
                 f"smoothing {params['smoothing']}"
                 + (", reused cached analysis" if hit else "")
                 + ") — border compensation zooms in slightly")


async def _op_extract_audio(src: str, base, op: dict):
    fmt = str(op.get("format", "wav")).lower()
    if fmt not in _AUDIO_EXTS:
        raise ValueError(f"format must be one of {sorted(_AUDIO_EXTS)}")
    codec = {"wav": ["-c:a", "pcm_s16le"], "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
             "aac": ["-c:a", "aac", "-b:a", "192k"], "flac": ["-c:a", "flac"]}[fmt]
    out = str(base) + _AUDIO_EXTS[fmt]
    await _step(["-i", src, "-vn", *codec, out])
    return out, f"audio extracted as {fmt}"


async def _op_replace_audio(src: str, base, op: dict):
    audio = _resolve_path(op.get("audio_path", ""))
    if not Path(audio).exists():
        raise ValueError(f"audio file not found: {op.get('audio_path')}")
    out = str(base) + ".mp4"
    mix_db = op.get("mix_original_db")
    if mix_db is None:
        await _step(["-i", src, "-i", audio, "-map", "0:v", "-map", "1:a",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-shortest", "-movflags", "+faststart", out])
        return out, "audio replaced"
    graph = (f"[0:a]volume={_f(mix_db)}dB[orig];"
             f"[1:a][orig]amix=inputs=2:duration=first:normalize=0[a]")
    await _step(["-i", src, "-i", audio, "-filter_complex", graph,
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out, f"new audio mixed over original ({mix_db} dB)"


async def _op_match_color(src: str, base, op: dict):
    import colormatch

    ref = op.get("ref")
    if not ref:
        raise ValueError('match_color needs ref: "path@seconds" '
                         "(the frame to match this video to)")
    ref_path, ref_time = colormatch.parse_ref(ref)
    ref_path = _resolve_path(ref_path)
    if not Path(ref_path).exists():
        raise ValueError(f"reference file not found: {ref}")
    info = await probe(src)
    dur = media_duration(info)
    tt = op.get("target_time")
    tt = dur / 2 if tt is None else float(tt)
    strength = float(op.get("strength", 1.0))
    cube = str(base) + ".cube"
    await asyncio.to_thread(
        colormatch.generate_match_lut,
        src, colormatch.sample_window(tt, 0.0, max(0.0, dur - 0.05)),
        ref_path, colormatch.sample_window(ref_time, 0.0, ref_time + 0.15),
        cube, strength)
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"lut3d=file='{cube}'",
                 *_ENCODE, out], timeout=3600)
    return out, (f"color-matched to {Path(ref_path).name}@{ref_time:g} "
                 f"(strength {strength:g})")


async def _op_motion_blur(src: str, base, op: dict):
    strength = float(op.get("strength", 0.5))
    if not 0 <= strength <= 1:
        raise ValueError("strength must be 0–1")
    frames = 2 + round(strength * 4)
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"tmix=frames={frames}", *_ENCODE, out])
    return out, (f"motion blur ({frames} stacked frames) — strongest on "
                 "high-fps or flow-interpolated footage")


async def _op_enhance_audio(src: str, base, op: dict):
    preset = op.get("preset", "voice")
    chain = audiofx_mod.enhance_chain(preset, op)
    info = await probe(src)
    if audio_stream(info) is None:
        raise ValueError("no audio stream to enhance")
    vcodec = ["-c:v", "copy"] if video_stream(info) is not None else []
    out = str(base) + ".mp4"
    await _step(["-i", src, *vcodec, "-af", ",".join(chain),
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    stages = [k for k in audiofx_mod.AUDIO_KEYS
              if (audiofx_mod.ENHANCE_PRESETS[preset].get(k)
                  if k not in op else op.get(k))]
    return out, f"enhanced ({preset}: {' → '.join(stages)} → limiter)"


async def _op_mute(src: str, base, op: dict):
    out = str(base) + ".mp4"
    await _step(["-i", src, "-an", "-c:v", "copy", "-movflags", "+faststart", out])
    return out, "audio removed"


async def _op_volume(src: str, base, op: dict):
    db = float(op.get("db", 0))
    if not -60 <= db <= 12:
        raise ValueError("db must be -60–+12")
    out = str(base) + ".mp4"
    await _step(["-i", src, "-c:v", "copy", "-af", f"volume={_f(db)}dB",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out, f"volume {db:+} dB"


async def _op_loudnorm(src: str, base, op: dict):
    i = float(op.get("target_lufs", -14))
    tp = float(op.get("true_peak", -1.5))
    lra = float(op.get("lra", 11))
    _, stderr = await run_ffmpeg(
        ["-i", src, "-vn", "-af",
         f"loudnorm=I={_f(i)}:TP={_f(tp)}:LRA={_f(lra)}:print_format=json",
         "-f", "null", "-"], timeout=900, heavy=False)
    matches = list(_LOUDNORM_JSON.finditer(stderr))
    if not matches:
        raise ValueError("loudness measurement failed")
    meas = json.loads(matches[-1].group(0))
    ln = (f"loudnorm=I={_f(i)}:TP={_f(tp)}:LRA={_f(lra)}"
          f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
          f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
          f":offset={meas.get('target_offset', 0)}:linear=true")
    out = str(base) + ".mp4"
    await _step(["-i", src, "-c:v", "copy", "-af", ln,
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    return out, (f"normalized {meas['input_i']} → {i} LUFS (two-pass)")


async def _op_burn_subtitles(src: str, base, op: dict):
    sub = _resolve_path(op.get("subtitle_path", ""))
    if not Path(sub).exists():
        raise ValueError(f"subtitle file not found: {op.get('subtitle_path')}")
    info = await probe(src)
    vs = video_stream(info)
    if vs is None:
        raise ValueError("no video stream")
    ass_text = captions_mod.build_ass(
        sub, play_w=int(vs["width"]), play_h=int(vs["height"]),
        preset=op.get("preset", captions_mod.DEFAULT_PRESET),
        position=op.get("position", "lower_third"),
        font_size=op.get("font_size"),
        highlight_color=op.get("highlight_color"),
        uppercase=op.get("uppercase"),
    )
    ass_path = str(base) + ".ass"
    Path(ass_path).write_text(ass_text, encoding="utf-8")
    out = str(base) + ".mp4"
    await _step(["-i", src, "-vf", f"ass=filename='{ass_path}'",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-c:a", "copy", "-movflags", "+faststart", out])
    return out, f"burned {Path(sub).name} ({op.get('preset', 'karaoke')})"


async def _op_to_gif(src: str, base, op: dict):
    fps = int(op.get("fps", 12))
    width = int(op.get("width", 480))
    pre = ""
    if op.get("start") is not None or op.get("end") is not None:
        s, e = float(op.get("start", 0)), op.get("end")
        pre = f"trim=start={_f(s)}" + (f":end={_f(float(e))}" if e else "")
        pre += ",setpts=PTS-STARTPTS,"
    graph = (f"[0:v]{pre}fps={fps},scale={width}:-2:flags=lanczos,split[a][b];"
             f"[a]palettegen=stats_mode=diff[p];"
             f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[v]")
    out = str(base) + ".gif"
    await _step(["-i", src, "-filter_complex", graph, "-map", "[v]", out])
    return out, f"GIF {width}px @ {fps}fps"


async def _op_to_webp(src: str, base, op: dict):
    fps = int(op.get("fps", 15))
    width = int(op.get("width", 720))
    quality = int(op.get("quality", 80))
    pre = ""
    if op.get("start") is not None or op.get("end") is not None:
        s, e = float(op.get("start", 0)), op.get("end")
        pre = f"trim=start={_f(s)}" + (f":end={_f(float(e))}" if e else "")
        pre += ",setpts=PTS-STARTPTS,"
    out = str(base) + ".webp"
    await _step(["-i", src, "-vf", f"{pre}fps={fps},scale={width}:-2:flags=lanczos",
                 "-c:v", "libwebp", "-q:v", str(quality), "-loop", "0", "-an", out])
    return out, f"animated WebP {width}px @ {fps}fps q{quality}"


_OPS = {
    "trim": _op_trim,
    "remove": _op_remove,
    "crop": _op_crop,
    "smart_reframe": _op_smart_reframe,
    "stabilize": _op_stabilize,
    "resize": _op_resize,
    "speed": _op_speed,
    "speed_ramp": _op_speed_ramp,
    "fps": _op_fps,
    "concat": _op_concat,
    "extract_audio": _op_extract_audio,
    "replace_audio": _op_replace_audio,
    "enhance_audio": _op_enhance_audio,
    "match_color": _op_match_color,
    "motion_blur": _op_motion_blur,
    "mute": _op_mute,
    "volume": _op_volume,
    "loudness_normalize": _op_loudnorm,
    "burn_subtitles": _op_burn_subtitles,
    "to_gif": _op_to_gif,
    "to_webp": _op_to_webp,
}
