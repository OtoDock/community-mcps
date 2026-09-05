"""Analysis tools: the model's eyes and ears.

Everything here converts real-time media into structured, timestamped data +
sparse images the model can reason over: shot lists with contact sheets,
beat grids / energy / loudness / silence maps with a waveform render, and
frame sampling. Results are cached as sidecars next to the source
(`<stem>.analysis.json`, `<stem>.shots.png`, `<stem>.waveform.png`) so a
later session — or another agent — reuses them without re-analysis.
"""

import asyncio
import base64
import io
import json
import re
import tempfile
from pathlib import Path

from mcp.types import ImageContent, TextContent

import color as color_mod
from fftools import audio_stream, colr_box, media_duration, probe, run_ffmpeg, stream_color, stream_fps, timecode, video_stream
from shared import _notify_file_written, _resolve_path, _to_agents_relative

_MAX_SHOT_THUMBS = 60


def _sidecar_path(media_path: str, suffix: str) -> Path:
    p = Path(media_path)
    return p.parent / (p.stem + suffix)


def _merge_sidecar(media_path: str, key: str, payload: dict) -> Path:
    path = _sidecar_path(media_path, ".analysis.json")
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data[key] = payload
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _png_content(png_bytes: bytes) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(png_bytes).decode(),
        mimeType="image/png",
    )


def _grid(cells: list[tuple[str, "object"]], columns: int = 4,
          cell_width: int = 320) -> bytes:
    """Label+image cells → grid PNG bytes."""
    from PIL import Image, ImageDraw

    label_h = 20
    imgs = []
    for label, img in cells:
        ratio = cell_width / img.width
        imgs.append((label, img.resize((cell_width, max(1, int(img.height * ratio))))))
    cell_h = max(i.height for _, i in imgs) + label_h
    cols = max(1, min(columns, len(imgs)))
    rows = (len(imgs) + cols - 1) // cols
    grid = Image.new("RGB", (cols * cell_width, rows * cell_h), "#101010")
    draw = ImageDraw.Draw(grid)
    for i, (label, img) in enumerate(imgs):
        cx, cy = (i % cols) * cell_width, (i // cols) * cell_h
        grid.paste(img, (cx, cy + label_h))
        draw.text((cx + 5, cy + 3), label, fill="#e0e0e0")
    buf = io.BytesIO()
    grid.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# probe_media
# ---------------------------------------------------------------------------


async def handle_probe_media(args: dict):
    path = _resolve_path(args["path"])
    info = await probe(path)
    fmt = info.get("format", {})
    lines = [f"# {_to_agents_relative(path)}"]
    dur = media_duration(info)
    size = float(fmt.get("size", 0) or 0)
    lines.append(f"container: {fmt.get('format_name', '?')} · "
                 f"duration {dur:.2f}s · {size / 1e6:.1f} MB · "
                 f"bitrate {int(float(fmt.get('bit_rate', 0) or 0) / 1000)} kb/s")
    hdr = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            lines.append(
                f"video: {s.get('codec_name')} {s.get('width')}x{s.get('height')} "
                f"@ {stream_fps(s):.3g} fps · pix_fmt {s.get('pix_fmt')}")
            if s.get("disposition", {}).get("attached_pic", 0) == 1:
                continue
            col = stream_color(s)
            trc = col.get("color_transfer")
            kind = color_mod.HDR_TRANSFERS.get(trc) or (
                "SDR" if trc in color_mod.SDR_TRANSFERS else None)
            hdr = hdr or color_mod.HDR_TRANSFERS.get(trc)
            lines.append(
                f"color: matrix={col.get('color_space', 'unknown')} · "
                f"primaries={col.get('color_primaries', 'unknown')} · "
                f"transfer={trc or 'unknown'}{f' ({kind})' if kind else ''} · "
                f"range={col.get('color_range', 'unknown')}")
        elif s.get("codec_type") == "audio":
            lines.append(
                f"audio: {s.get('codec_name')} {s.get('sample_rate')} Hz · "
                f"{s.get('channels')}ch")
    if video_stream(info) is not None:
        lines.append(_colr_line(path, fmt.get("format_name", "")))
    tc = timecode(info)
    if tc:
        lines.append(f"timecode: {tc[0]} ({tc[1]})")
    if hdr:
        preset = color_mod.CONVERT_FOR.get(hdr, "hlg->rec709")
        lines.append(
            f"{hdr} source: in a composition set color.convert "
            f"\"{preset}\" on the clip (built-in {hdr}→Rec.709 conversion; "
            "no technical LUT needed), or declare color.input and chain your "
            "own technical LUT — see the video-editing skill, Color section. "
            "edit_video ops keep the file HDR as it is (no tone-mapping).")
    return "\n".join(lines)


def _colr_line(path: str, format_name: str) -> str:
    """Whether the CONTAINER declares colour — ffprobe merges the colr box
    and the bitstream VUI, so only a box walk can tell (Sony XAVC writes
    none; the tags above then come from the VUI or are guesses)."""
    if not any(n in format_name for n in ("mp4", "mov", "3gp", "mj2")):
        return "colr box: n/a (not MP4/MOV)"
    box = colr_box(path)
    if not box:
        return ("colr box: absent — the colour tags above come from the "
                "bitstream (VUI), or are unknown")
    if box["type"] in ("nclx", "nclc"):
        rng = box.get("full_range")
        rng_s = "" if rng is None else f" · full_range={'yes' if rng else 'no'}"
        return (f"colr box: present ({box['type']}: primaries={box['primaries']} "
                f"· transfer={box['transfer']} · matrix={box['matrix']}{rng_s})")
    return f"colr box: present ({box['type']} ICC profile)"


# ---------------------------------------------------------------------------
# analyze_video — shots + contact sheet
# ---------------------------------------------------------------------------


def _detect_shots(path: str, threshold: float) -> list[tuple[float, float]]:
    from scenedetect import ContentDetector, detect

    scenes = detect(path, ContentDetector(threshold=threshold))
    return [(s.get_seconds(), e.get_seconds()) for s, e in scenes]


def _shot_thumbnails(path: str, midpoints: list[float]) -> list:
    import cv2
    from PIL import Image

    cap = cv2.VideoCapture(path)
    thumbs = []
    try:
        for t in midpoints:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                thumbs.append(None)
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            thumbs.append(Image.fromarray(rgb))
    finally:
        cap.release()
    return thumbs


async def handle_analyze_video(args: dict):
    path = _resolve_path(args["path"])
    threshold = float(args.get("threshold", 27.0))
    info = await probe(path)
    vs = video_stream(info)
    if vs is None:
        return f"Error: '{args['path']}' has no video stream"
    duration = media_duration(info)

    shots = await asyncio.to_thread(_detect_shots, path, threshold)
    if not shots:
        shots = [(0.0, duration)]

    listed = shots[:_MAX_SHOT_THUMBS]
    midpoints = [(s + e) / 2 for s, e in listed]
    thumbs = await asyncio.to_thread(_shot_thumbnails, path, midpoints)
    cells = [
        (f"#{i} {s:.1f}-{e:.1f}s", img)
        for i, ((s, e), img) in enumerate(zip(listed, thumbs))
        if img is not None
    ]

    payload = {
        "duration": round(duration, 3),
        "fps": round(stream_fps(vs), 3),
        "resolution": f"{vs.get('width')}x{vs.get('height')}",
        "threshold": threshold,
        "shots": [{"index": i, "start": round(s, 3), "end": round(e, 3),
                   "duration": round(e - s, 3)}
                  for i, (s, e) in enumerate(shots)],
    }
    sidecar = _merge_sidecar(path, "video", payload)
    await _notify_file_written(str(sidecar))

    result = [TextContent(type="text", text=_format_video_report(path, payload, len(listed)))]
    if cells:
        sheet = _grid(cells, columns=int(args.get("columns", 4)))
        sheet_path = _sidecar_path(path, ".shots.png")
        sheet_path.write_bytes(sheet)
        await _notify_file_written(str(sheet_path))
        result.insert(0, _png_content(sheet))
    return result


def _format_video_report(path: str, payload: dict, listed: int) -> str:
    shots = payload["shots"]
    lines = [
        f"# Shot analysis — {_to_agents_relative(path)}",
        f"{payload['resolution']} @ {payload['fps']} fps · "
        f"{payload['duration']:.2f}s · {len(shots)} shot(s) "
        f"(threshold {payload['threshold']})",
    ]
    for s in shots[:listed]:
        lines.append(f"  #{s['index']:>3}  {s['start']:>8.2f} → {s['end']:>8.2f}"
                     f"  ({s['duration']:.2f}s)")
    if len(shots) > listed:
        lines.append(f"  … {len(shots) - listed} more shots — full list in the sidecar")
    lines.append(f"sidecar: {_to_agents_relative(str(_sidecar_path(path, '.analysis.json')))} · "
                 f"contact sheet: {_to_agents_relative(str(_sidecar_path(path, '.shots.png')))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# analyze_audio — beats, energy, loudness, silences, waveform
# ---------------------------------------------------------------------------


def _librosa_analysis(wav_path: str) -> dict:
    import librosa
    import numpy as np

    y, sr = librosa.load(wav_path, sr=22050, mono=True)
    duration = float(len(y) / sr)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    times = librosa.frames_to_time(range(len(rms)), sr=sr, hop_length=512)
    peak = float(rms.max()) or 1.0
    step = max(1, len(rms) // max(1, int(duration * 4)))  # ~4 samples/sec
    energy = [[round(float(t), 2), round(float(v) / peak, 3)]
              for t, v in zip(times[::step], rms[::step])]
    return {
        "duration": round(duration, 3),
        "tempo_bpm": round(tempo, 1),
        "beats": [round(float(b), 3) for b in beats],
        "energy": energy,
        "samples": y[:: max(1, len(y) // 2400)].tolist(),  # waveform envelope
    }


def _draw_waveform(samples: list[float], duration: float,
                   beats: list[float]) -> bytes:
    from PIL import Image, ImageDraw

    W, H = 1200, 260
    img = Image.new("RGB", (W, H), "#101014")
    draw = ImageDraw.Draw(img)
    mid = H // 2
    n = len(samples)
    peak = max(abs(min(samples)), abs(max(samples))) or 1.0
    for x in range(W):
        i0, i1 = int(x / W * n), max(int((x + 1) / W * n), int(x / W * n) + 1)
        seg = samples[i0:i1]
        if not seg:
            continue
        lo = min(seg) / peak
        hi = max(seg) / peak
        draw.line([(x, mid - hi * (mid - 24)), (x, mid - lo * (mid - 24))],
                  fill="#3aa7a0")
    for b in beats:
        x = int(b / duration * W) if duration else 0
        draw.line([(x, H - 18), (x, H - 8)], fill="#ffd400")
    tick = 5 if duration <= 90 else 15
    t = 0.0
    while t <= duration:
        x = int(t / duration * W) if duration else 0
        draw.line([(x, 0), (x, 6)], fill="#555")
        draw.text((x + 2, 6), f"{int(t)}s", fill="#888")
        t += tick
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_SILENCE_RE = re.compile(r"silence_(start|end): ([\d.]+)")
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


async def handle_analyze_audio(args: dict):
    path = _resolve_path(args["path"])
    info = await probe(path)
    if audio_stream(info) is None:
        return f"Error: '{args['path']}' has no audio stream"

    with tempfile.TemporaryDirectory(prefix="vt-audio-") as tmp:
        wav = str(Path(tmp) / "mono.wav")
        await run_ffmpeg(["-i", path, "-vn", "-ac", "1", "-ar", "22050", wav],
                         timeout=600, heavy=False)
        lib = await asyncio.to_thread(_librosa_analysis, wav)

    # Silence map + broadcast loudness measured on the original file.
    _, stderr = await run_ffmpeg(
        ["-i", path, "-vn",
         "-af", "silencedetect=noise=-35dB:d=0.4,loudnorm=print_format=json",
         "-f", "null", "-"], timeout=600, heavy=False)
    silences = []
    pending = None
    for kind, value in _SILENCE_RE.findall(stderr):
        if kind == "start":
            pending = float(value)
        elif pending is not None:
            silences.append([round(pending, 2), round(float(value), 2)])
            pending = None
    loudness = {}
    matches = list(_LOUDNORM_JSON.finditer(stderr))
    if matches:
        meas = json.loads(matches[-1].group(0))
        loudness = {
            "integrated_lufs": float(meas.get("input_i", 0)),
            "true_peak_dbtp": float(meas.get("input_tp", 0)),
            "lra": float(meas.get("input_lra", 0)),
        }

    samples = lib.pop("samples")
    payload = {**lib, "silences": silences, "loudness": loudness}
    sidecar = _merge_sidecar(path, "audio", payload)
    await _notify_file_written(str(sidecar))

    wave_png = _draw_waveform(samples, lib["duration"], lib["beats"])
    wave_path = _sidecar_path(path, ".waveform.png")
    wave_path.write_bytes(wave_png)
    await _notify_file_written(str(wave_path))

    beats = payload["beats"]
    beat_preview = ", ".join(f"{b:.2f}" for b in beats[:24])
    lines = [
        f"# Audio analysis — {_to_agents_relative(path)}",
        f"duration {payload['duration']:.2f}s · tempo {payload['tempo_bpm']} BPM · "
        f"{len(beats)} beats",
        f"beat grid starts: {beat_preview}{' …' if len(beats) > 24 else ''}",
    ]
    if loudness:
        lines.append(f"loudness: {loudness['integrated_lufs']:.1f} LUFS · "
                     f"peak {loudness['true_peak_dbtp']:.1f} dBTP · "
                     f"LRA {loudness['lra']:.1f}")
    if silences:
        shown = ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in silences[:15])
        lines.append(f"silences (> 0.4s below -35dB): {shown}"
                     f"{' …' if len(silences) > 15 else ''}")
    else:
        lines.append("silences: none detected")
    lines.append(
        "full beat grid + energy curve in sidecar: "
        f"{_to_agents_relative(str(sidecar))} — cut on these timestamps to beat-sync")
    return [_png_content(wave_png), TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# align_audio — offset between two recordings of the same event
# ---------------------------------------------------------------------------
#
# GCC-PHAT on the 8 kHz mono waveforms: the cross-power spectrum is
# whitened (phase only), so a stationary ambience — wind, sea, room tone —
# yields a sharp peak at the true lag just like a click does. An
# onset-envelope correlation was tried first and failed exactly there
# (drone ambience: −0.019 s at a flat 0.46 for a true +1.750 s), which PHAT
# recovers at 85× its runner-up. 8 kHz gives 0.125 ms resolution — far
# below a frame. Sign convention: target_time = ref_time + offset.

_ALIGN_SR = 8000
# Bound the FFT (two 20-min signals → 2^25 points, ~0.5 GB of spectra).
_ALIGN_MAX_SECONDS = 20 * 60.0
# Runner-up search excludes this radius around the peak (side lobes).
_RUNNER_UP_RADIUS = 0.02


def _align_waveforms(wav_ref: str, wav_tgt: str, max_offset: float) -> dict:
    import librosa
    import numpy as np

    sr = _ALIGN_SR
    yr, _ = librosa.load(wav_ref, sr=None, mono=True)
    yt, _ = librosa.load(wav_tgt, sr=None, mono=True)
    if len(yr) < sr or len(yt) < sr:
        raise ValueError("both files need at least 1 s of audio")
    if float(np.abs(yr).max()) < 1e-4 or float(np.abs(yt).max()) < 1e-4:
        raise ValueError("no usable audio energy in one of the files (silent?)")
    cap = int(_ALIGN_MAX_SECONDS * sr)
    truncated = len(yr) > cap or len(yt) > cap
    yr, yt = yr[:cap], yt[:cap]

    size = 1 << (len(yr) + len(yt) - 1).bit_length()
    fr = np.fft.rfft(yr, size)
    ft = np.fft.rfft(yt, size)
    cross = ft * np.conj(fr)
    cross /= (np.abs(cross) + 1e-9)
    # cc[k] peaks where yt[n] ≈ yr[n − k]: the target runs k samples LATE.
    # Lags beyond either signal's length are meaningless and, with the
    # zero-padded circular correlation, would alias onto real lags of the
    # opposite sign — clamp them.
    cc = np.fft.irfft(cross, size)
    max_lag = max(1, min(int(round(max_offset * sr)), len(yr) - 1, len(yt) - 1))
    lags = np.arange(-max_lag, max_lag + 1)
    vals = cc[lags % size]
    best = int(np.argmax(vals))
    lag = int(lags[best])
    peak = float(vals[best])
    away = np.abs(lags - lag) > int(_RUNNER_UP_RADIUS * sr)
    second = float(vals[away].max()) if away.any() else 0.0
    lo = max(0, -lag)
    hi = min(len(yr), len(yt) - lag)
    return {
        "offset": lag / sr, "peak": peak,
        "ratio": peak / max(second, 1e-6),
        "ref_duration": len(yr) / sr, "target_duration": len(yt) / sr,
        "overlap": max(0.0, (hi - lo) / sr), "truncated": truncated,
    }


def _align_grade(ratio: float) -> str:
    if ratio >= 8:
        return "strong"
    if ratio >= 3:
        return "fair"
    return "weak"


async def handle_align_audio(args: dict):
    ref = _resolve_path(args["ref"])
    target = _resolve_path(args["target"])
    for label, raw, p in (("ref", args["ref"], ref), ("target", args["target"], target)):
        if not Path(p).exists():
            return f"Error: {label} file not found: {raw}"
    try:
        max_offset = float(args.get("max_offset", 60.0))
    except (TypeError, ValueError):
        return "Error: max_offset must be a number of seconds"
    max_offset = min(600.0, max(1.0, max_offset))
    infos = {}
    for label, p in (("ref", ref), ("target", target)):
        infos[label] = await probe(p)
        if audio_stream(infos[label]) is None:
            return f"Error: {label} '{args[label]}' has no audio stream"

    with tempfile.TemporaryDirectory(prefix="vt-align-") as tmp:
        wavs = {}
        for label, p in (("ref", ref), ("target", target)):
            wav = str(Path(tmp) / f"{label}.wav")
            await run_ffmpeg(["-i", p, "-vn", "-ac", "1", "-ar", str(_ALIGN_SR), wav],
                             timeout=900, heavy=False)
            wavs[label] = wav
        try:
            res = await asyncio.to_thread(_align_waveforms, wavs["ref"], wavs["target"],
                                          max_offset)
        except ValueError as exc:
            return f"Error: {exc}"

    off = res["offset"]
    grade = _align_grade(res["ratio"])
    ref_rel, tgt_rel = _to_agents_relative(ref), _to_agents_relative(target)
    lines = [
        "# Audio alignment",
        f"ref:    {ref_rel} ({res['ref_duration']:.2f}s)",
        f"target: {tgt_rel} ({res['target_duration']:.2f}s)",
        f"offset: {off:+.4f} s  — target_time = ref_time + offset "
        f"(the moment at ref@10.000 is at target@{10.0 + off:.4f})",
        f"confidence: {grade} — PHAT peak {res['peak']:.2f}, "
        f"{res['ratio']:.0f}× the runner-up (overlap {res['overlap']:.1f}s; "
        f"search window ±{max_offset:g}s"
        + ("; only the first 20 min of each file were compared" if res["truncated"] else "")
        + ")",
        "To sync in a composition:",
        f"  - same timeline start as the ref clip: target clip in = ref.in {off:+.4f}"
        + (" (a negative result means the target starts later — use the start form)"
           if off < 0 else ""),
        f"  - same in-point: target start = ref.start {-off:+.4f}"
        + (" (audio/overlay clips need start ≥ 0 — trim the ref instead when this "
           "goes negative)" if off > 0 else ""),
    ]
    if grade == "weak":
        lines.append("Weak peak: the two recordings may not contain the same "
                     "event, or the true offset exceeds max_offset — check the "
                     "files, raise max_offset, or align a shorter excerpt.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sample_frames — look at a source file at given timestamps
# ---------------------------------------------------------------------------


async def handle_sample_frames(args: dict):
    from PIL import Image

    path = _resolve_path(args["path"])
    timestamps = args.get("timestamps") or []
    if not timestamps:
        return "Error: pass timestamps=[…] (seconds) to sample"
    if len(timestamps) > 24:
        return "Error: at most 24 timestamps per call"
    info = await probe(path)
    if video_stream(info) is None:
        return f"Error: '{args['path']}' has no video stream"
    duration = media_duration(info)

    cells = []
    with tempfile.TemporaryDirectory(prefix="vt-sample-") as tmp:
        for i, t in enumerate(timestamps):
            t = max(0.0, min(float(t), max(0.0, duration - 0.04)))
            out = str(Path(tmp) / f"s{i}.png")
            await run_ffmpeg(
                ["-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                 "-vf", "scale=-2:360", out],
                timeout=300, heavy=False)
            cells.append((f"t={t:.2f}s", Image.open(out).convert("RGB")))
        png = _grid(cells, columns=int(args.get("columns", 4)))
    return [_png_content(png),
            TextContent(type="text",
                        text=f"{len(cells)} frame(s) from {_to_agents_relative(path)}")]
