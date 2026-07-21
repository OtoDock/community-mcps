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

from fftools import FFPROBE, audio_stream, media_duration, probe, run_ffmpeg, stream_fps, video_stream
from shared import _notify_file_written, _resolve_path, _to_agents_relative, logger

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
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            lines.append(
                f"video: {s.get('codec_name')} {s.get('width')}x{s.get('height')} "
                f"@ {stream_fps(s):.3g} fps · pix_fmt {s.get('pix_fmt')}")
        elif s.get("codec_type") == "audio":
            lines.append(
                f"audio: {s.get('codec_name')} {s.get('sample_rate')} Hz · "
                f"{s.get('channels')}ch")
    return "\n".join(lines)


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
