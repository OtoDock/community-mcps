"""End-to-end execution tests against a real ffmpeg.

Synthesizes deterministic assets (test pattern clips, a 120 BPM click track,
an alpha logo, a transcript), renders a full composition — cuts, an xfade,
an overlay, ducked music, karaoke captions, two-pass loudnorm — and asserts
on the OUTPUT: probed streams/durations and measured loudness. Skipped
when no ffmpeg binary is available.
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import HAVE_FFMPEG

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")

import analysis  # noqa: E402
import composition as comp_mod  # noqa: E402
import quickops  # noqa: E402
import renderer  # noqa: E402
import stab  # noqa: E402
from fftools import FFMPEG, audio_stream, colr_box, media_duration, probe, stream_color, video_stream  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=300)


@pytest.fixture(scope="session")
def assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("assets")
    clip1 = root / "clip1.mp4"
    clip2 = root / "clip2.mp4"
    cutcat = root / "cutcat.mp4"
    music = root / "music.wav"
    logo = root / "logo.png"
    transcript = root / "demo.transcript.json"

    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-pix_fmt", "yuv420p", str(clip1))
    _ff("-f", "lavfi", "-i", "smptehdbars=size=640x360:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-pix_fmt", "yuv420p", str(clip2))
    # Hard-cut concat for shot detection.
    _ff("-i", str(clip1), "-i", str(clip2), "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", str(cutcat))
    # 120 BPM click track: an 880 Hz blip every 0.5s.
    _ff("-f", "lavfi", "-i",
        "aevalsrc=0.9*sin(2*PI*880*t)*lt(mod(t\\,0.5)\\,0.06):s=44100:d=30",
        "-c:a", "pcm_s16le", str(music))

    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (220, 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 219, 79], radius=16, fill=(20, 160, 150, 230))
    d.text((24, 30), "OtoDock", fill=(255, 255, 255, 255))
    img.save(logo)

    transcript.write_text(json.dumps({"text": "demo", "words": [
        {"word": "Meet", "start": 0.3, "end": 0.6},
        {"word": "OtoDock", "start": 0.6, "end": 1.2},
        {"word": "agents", "start": 1.4, "end": 1.9},
        {"word": "everywhere.", "start": 1.9, "end": 2.6},
        {"word": "Self-hosted.", "start": 3.4, "end": 4.2},
    ]}))
    return {"root": root, "clip1": clip1, "clip2": clip2, "cutcat": cutcat,
            "music": music, "logo": logo, "transcript": transcript}


@pytest.fixture()
def demo_comp(assets):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(assets["clip1"]), "in": 0.5, "out": 3.5,
         "color": {"lut": "clean-punch"}},
        {"src": str(assets["clip2"]), "in": 0, "out": 3,
         "transition_in": {"type": "circleopen", "duration": 0.5}},
    ]
    comp["tracks"].append({"kind": "overlay", "clips": [
        {"image": str(assets["logo"]), "duration": 2.0, "start": 0.8,
         "fade_in": 0.3, "fade_out": 0.3,
         "transform": {"pos": [0, -100]}},
    ]})
    comp["tracks"].append({"kind": "audio", "clips": [
        {"src": str(assets["music"]), "start": 0, "out": 8,
         "gain_db": -6, "duck": True},
    ]})
    comp["captions"] = {"source": str(assets["transcript"]),
                        "preset": "karaoke", "position": "lower_third"}
    comp["audio_master"] = {"gain_db": 0, "loudnorm": {"target_lufs": -16}}
    path = assets["root"] / "demo.vproj.json"
    comp_mod.save_composition(str(path), comp)
    return path


def test_validate_demo_composition(demo_comp):
    comp = comp_mod.load_composition(str(demo_comp))
    _, _, issues = _run(renderer.prepare(comp, lambda p: p))
    assert [i for i in issues if i["level"] == "error"] == []


def test_preview_render_and_frames(demo_comp):
    result = _run(renderer.render_composition(
        str(demo_comp), lambda p: p, mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.name == "demo.preview.mp4"
    assert abs(result["duration"] - 5.5) < 0.2  # 3 + 3 − 0.5 xfade
    info = _run(probe(str(out)))
    vs = video_stream(info)
    assert vs["codec_name"] == "h264"
    assert audio_stream(info)["codec_name"] == "aac"

    png, note = _run(renderer.render_frames(
        str(demo_comp), lambda p: p, [1.2, 2.75, 4.5]))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    (demo_comp.parent / "frames.png").write_bytes(png)


def test_final_render_hits_loudness_target(demo_comp):
    result = _run(renderer.render_composition(
        str(demo_comp), lambda p: p, mode="final"))
    out = Path(result["output"])
    assert out.exists() and out.name == "demo.mp4"

    # Measure what actually landed on disk.
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(out), "-vn",
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    m = list(re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S))
    assert m, proc.stderr[-500:]
    measured = float(json.loads(m[-1].group(0))["input_i"])
    assert abs(measured - (-16.0)) < 1.5, f"final render measured {measured} LUFS"


def test_range_render(demo_comp):
    result = _run(renderer.render_composition(
        str(demo_comp), lambda p: p, mode="preview",
        output_path=str(demo_comp.parent / "slice.mp4"),
        time_range=(2.0, 3.5)))
    assert abs(result["duration"] - 1.5) < 0.15


@pytest.fixture(scope="session")
def fold_assets(tmp_path_factory):
    """Eight distinct 6 s pattern clips — a long-enough multi-clip fold
    that a single-graph render measurably buffers the timeline (the OOM
    mechanism), while staying tiny at 320x180."""
    root = tmp_path_factory.mktemp("fold")
    clips = []
    for i in range(8):
        p = root / f"fold{i}.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(p))
        clips.append(p)
    return {"root": root, "clips": clips}


@pytest.mark.skipif(not Path("/usr/bin/time").exists(),
                    reason="GNU time not available")
def test_segmented_render_bounds_memory_and_matches_single_pass(
        fold_assets, tmp_path, monkeypatch):
    """The low-RAM path: a small budget must (a) split the render into
    windows at hard cuts, (b) produce the same timeline as the single
    graph, and (c) hold ffmpeg's peak RSS well under the single-graph
    render, which buffers ~the whole decoded timeline in filtergraph
    queues (the original OOM: a 70 s 1080p comp at 12.7 GB on a 15 GB
    host — scaled down here to 48 s at 320x180)."""
    import fftools

    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(p), "in": 0, "out": 6, "mute": True}
        for p in fold_assets["clips"]]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = fold_assets["root"] / "fold.vproj.json"
    comp_mod.save_composition(str(path), comp)

    log = tmp_path / "mem.log"
    log.write_text("")
    wrap = tmp_path / "ffmpeg-memwrap.sh"
    wrap.write_text('#!/bin/bash\nexec /usr/bin/time -f "RSSKB=%M" -a '
                    f'-o "{log}" "{FFMPEG}" "$@"\n')
    wrap.chmod(0o755)
    monkeypatch.setattr(fftools, "FFMPEG", str(wrap))

    def peak_rss_kb():
        vals = [int(line.split("=")[1])
                for line in log.read_text().splitlines()
                if line.startswith("RSSKB=")]
        log.write_text("")
        return max(vals)

    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "100000")
    single = _run(renderer.render_composition(
        str(path), lambda p: p, mode="preview",
        output_path=str(fold_assets["root"] / "single.mp4")))
    single_peak = peak_rss_kb()
    assert not any("windows" in w["message"] for w in single["warnings"])

    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "40")
    seg = _run(renderer.render_composition(
        str(path), lambda p: p, mode="preview",
        output_path=str(fold_assets["root"] / "seg.mp4")))
    seg_peak = peak_rss_kb()
    assert any("windows" in w["message"] and "memory budget" in w["message"]
               for w in seg["warnings"]), seg["warnings"]

    assert abs(seg["duration"] - single["duration"]) < 0.1
    assert abs(seg["duration"] - 48.0) < 0.2

    # The A/B that matters: single-graph buffers ~48 s x 30 fps x 86 KB
    # ≈ 190 MB over a ~70 MB floor; per-window buffering caps at a quarter
    # of the timeline, so the peak must drop by a wide, stable margin.
    assert seg_peak < single_peak * 0.75, (single_peak, seg_peak)

    # Frame equivalence across a window boundary (12 s): same content,
    # same encoder settings on both sides of the lossless concat.
    import numpy as np
    for t in (11.9, 12.1):
        a = _mean_rgb(fold_assets["root"] / "single.mp4", t)
        b = _mean_rgb(fold_assets["root"] / "seg.mp4", t)
        assert float(np.abs(a - b).mean()) < 8.0, (t, a, b)


def test_segmented_final_render_keeps_loudness_and_streams(
        assets, fold_assets, monkeypatch):
    """Segmented mode must not change delivery semantics: the separately
    rendered audio carries the two-pass loudnorm of the FULL mix, an
    overlay living inside one window still lands, and the lossless concat
    yields one continuous H.264/AAC file of the right duration."""
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(fold_assets["clips"][i]), "in": 0, "out": 4, "mute": True}
        for i in range(3)]
    comp["tracks"].append({"kind": "overlay", "clips": [
        {"image": str(assets["logo"]), "duration": 2.0, "start": 5.0,
         "fade_in": 0.2, "fade_out": 0.2}]})
    comp["tracks"].append({"kind": "audio", "clips": [
        {"src": str(assets["music"]), "start": 0, "out": 12, "gain_db": -3}]})
    comp["audio_master"] = {"gain_db": 0, "loudnorm": {"target_lufs": -16}}
    path = fold_assets["root"] / "seg-final.vproj.json"
    comp_mod.save_composition(str(path), comp)

    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "20")
    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="final"))
    assert any("windows" in w["message"] and "memory budget" in w["message"]
               for w in result["warnings"]), result["warnings"]

    out = result["output"]
    info = _run(probe(out))
    assert video_stream(info)["codec_name"] == "h264"
    assert audio_stream(info)["codec_name"] == "aac"
    assert abs(media_duration(info) - 12.0) < 0.15

    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", out, "-vn",
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    m = list(re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S))
    assert m, proc.stderr[-500:]
    measured = float(json.loads(m[-1].group(0))["input_i"])
    assert abs(measured - (-16.0)) < 1.5, measured

    # The overlay (teal logo, centered, 5–7 s) lives only in the middle
    # window: its rect at t=6 must read teal-dominant over the pattern.
    import numpy as np
    from PIL import Image
    png = Path(out).parent / "seg-final-logo.png"
    _ff("-ss", "6.0", "-i", out, "-frames:v", "1", str(png))
    px = np.asarray(Image.open(png).convert("RGB"), dtype=float)
    h, w = px.shape[:2]
    rect = px[(h - 80) // 2:(h + 80) // 2, (w - 220) // 2:(w + 220) // 2]
    assert float(rect[..., 1].mean() - rect[..., 0].mean()) > 15, \
        (rect[..., 0].mean(), rect[..., 1].mean())


def test_image_clip_with_transition_renders(assets):
    """Regression: an image clip xfading into a video. The image chain used
    to apply setpts after fps, wiping the CFR metadata xfade requires —
    ffmpeg aborted with EINVAL (-22) and wrote nothing."""
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"image": str(assets["logo"]), "duration": 2.0},
        {"src": str(assets["clip1"]), "in": 0, "out": 1.5,
         "transition_in": {"type": "fade", "duration": 0.4}},
    ]
    path = assets["root"] / "image-transition.vproj.json"
    comp_mod.save_composition(str(path), comp)
    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0
    assert abs(result["duration"] - 3.1) < 0.2   # 2.0 + 1.5 - 0.4 xfade
    info = _run(probe(str(out)))
    assert video_stream(info)["codec_name"] == "h264"


def test_transition_after_concat_renders(assets):
    """Regression: a transition on the LAST clip of a ≥3-clip base track.
    The pairwise fold feeds a concat-produced accumulator into xfade — before
    timebase normalization (settb=AVTB) ffmpeg aborted the graph with EINVAL
    and the render produced nothing."""
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(assets["clip1"]), "in": 0, "out": 1.5},
        {"src": str(assets["clip2"]), "in": 0, "out": 1.5},
        {"src": str(assets["clip1"]), "in": 2.0, "out": 3.5,
         "transition_in": {"type": "dissolve", "duration": 0.4}},
    ]
    path = assets["root"] / "trailing-transition.vproj.json"
    comp_mod.save_composition(str(path), comp)
    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0
    assert abs(result["duration"] - 4.1) < 0.2   # 1.5*3 − 0.4 xfade
    info = _run(probe(str(out)))
    assert video_stream(info)["codec_name"] == "h264"


def test_analyze_audio_finds_the_click_tempo(assets, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    result = _run(analysis.handle_analyze_audio({"path": str(assets["music"])}))
    text = next(c.text for c in result if getattr(c, "type", "") == "text")
    assert "BPM" in text
    sidecar = json.loads((assets["root"] / "music.analysis.json").read_text())
    tempo = sidecar["audio"]["tempo_bpm"]
    assert 110 <= tempo <= 130, f"click track detected as {tempo} BPM"
    beats = sidecar["audio"]["beats"]
    assert len(beats) >= 40
    gaps = [round(b2 - b1, 2) for b1, b2 in zip(beats, beats[1:])]
    assert abs(sorted(gaps)[len(gaps) // 2] - 0.5) < 0.05  # median gap ≈ beat


def test_analyze_video_detects_the_cut(assets, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    result = _run(analysis.handle_analyze_video({"path": str(assets["cutcat"])}))
    assert any(getattr(c, "type", "") == "image" for c in result)
    sidecar = json.loads((assets["root"] / "cutcat.analysis.json").read_text())
    shots = sidecar["video"]["shots"]
    assert len(shots) >= 2
    # The hard cut sits at 5.0s.
    assert any(abs(s["start"] - 5.0) < 0.2 for s in shots[1:])


def test_sample_frames_grid(assets, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    result = _run(analysis.handle_sample_frames(
        {"path": str(assets["clip1"]), "timestamps": [0.5, 2.0, 4.0]}))
    img = next(c for c in result if getattr(c, "type", "") == "image")
    assert img.mimeType == "image/png"


def test_quickops_trim_speed_gif(assets, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(assets["root"] / "quick.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(assets["clip1"]),
        "operations": [{"type": "trim", "start": 0.5, "end": 3.5},
                       {"type": "speed", "factor": 2.0}],
        "output_path": out,
    }))
    assert "error" not in text.splitlines()[0]
    info = _run(probe(out))
    assert abs(media_duration(info) - 1.5) < 0.15

    gif_text = _run(quickops.handle_edit_video({
        "path": out,
        "operations": [{"type": "to_gif", "fps": 10, "width": 320}],
    }))
    assert "saved:" in gif_text
    gif = assets["root"] / "quick_edited.gif"
    assert gif.exists() and gif.stat().st_size > 1000


def test_quickops_stops_on_bad_op(assets, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    text = _run(quickops.handle_edit_video({
        "path": str(assets["clip1"]),
        "operations": [{"type": "trim", "start": 5, "end": 1}],
    }))
    assert "error: trim" in text and "no output written" in text.lower()


def test_duck_dips_music_under_voiceover_clip(tmp_path):
    """A ducked music bed must dip under a separate VO clip even when the
    base video's own audio is silent (the sidechain keys off every
    non-ducked audio clip, not just the base bus)."""
    base = tmp_path / "base.mp4"
    music = tmp_path / "music200.wav"
    vo = tmp_path / "vo2k.wav"
    _ff("-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30:duration=6",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=6",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-pix_fmt", "yuv420p", str(base))
    _ff("-f", "lavfi", "-i", "sine=frequency=200:duration=6",
        "-c:a", "pcm_s16le", str(music))
    _ff("-f", "lavfi", "-i", "sine=frequency=2000:duration=2",
        "-c:a", "pcm_s16le", str(vo))

    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [{"src": str(base), "in": 0, "out": 6}]
    comp["tracks"].append({"kind": "audio", "clips": [
        {"src": str(music), "start": 0, "duck": True},
        {"src": str(vo), "start": 2},
    ]})
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = tmp_path / "duckvo.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(
        str(path), lambda p: p, mode="final"))
    out = result["output"]

    def band200_mean_volume(t0, t1):
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", out, "-vn",
             "-af", (f"atrim=start={t0}:end={t1},asetpts=PTS-STARTPTS,"
                     "bandpass=f=200:w=100,volumedetect"),
             "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", proc.stderr)
        assert m, proc.stderr[-800:]
        return float(m.group(1))

    during_vo = band200_mean_volume(2.4, 3.8)   # VO active (2.0–4.0)
    after_vo = band200_mean_volume(4.6, 5.8)    # VO gone, release settled
    assert after_vo - during_vo >= 3.0, (during_vo, after_vo)


def _frame_diffs(path: str) -> list:
    """Per-pair luma difference between consecutive frames (YAVG of a
    difference blend): a motion-energy proxy. Camera shake inflates it;
    duplicated frames zero it."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path),
         "-vf", "tblend=all_mode=difference,signalstats,metadata=print:file=-",
         "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    vals = [float(m.group(1))
            for m in re.finditer(r"YAVG=([\d.]+)", proc.stdout)]
    assert vals, proc.stderr[-500:]
    return vals


def _mean_frame_diff(path: str) -> float:
    vals = _frame_diffs(path)
    return sum(vals) / len(vals)


@pytest.fixture(scope="session")
def shaky_clip(tmp_path_factory):
    """Deterministic handheld shake: a jittery crop walk over a padded
    test pattern (two incommensurate sine sums ≈ hand tremor)."""
    root = tmp_path_factory.mktemp("shaky")
    path = root / "shaky.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=800x480:rate=30:duration=4",
        "-vf",
        ("crop=640:360:"
         "x='80+45*sin(n/2.1)+25*sin(n/0.9)':"
         "y='60+35*cos(n/1.7)+20*sin(n/1.1)'"),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(path))
    return path


def test_stabilize_in_composition_reduces_shake_and_caches(shaky_clip):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(shaky_clip), "in": 0.2, "out": 3.8,
         "stabilize": {"strength": "high"}},
    ]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = shaky_clip.parent / "stab.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0

    # A/B: the stabilized render must carry clearly less motion energy.
    shaky = _mean_frame_diff(str(shaky_clip))
    stabilized = _mean_frame_diff(str(out))
    assert stabilized < shaky * 0.8, (shaky, stabilized)

    # The detect pass cached a sidecar next to the source…
    sidecars = list(shaky_clip.parent.glob("shaky.stab-*.trf"))
    assert len(sidecars) == 1

    # …and a second render reuses it (no new vidstabdetect run).
    detect_runs = []
    real_run = stab.run_ffmpeg

    async def counting_run(args, **kw):
        detect_runs.append(args)
        return await real_run(args, **kw)

    stab.run_ffmpeg = counting_run
    try:
        _run(renderer.render_composition(str(path), lambda p: p,
                                         mode="preview"))
    finally:
        stab.run_ffmpeg = real_run
    assert detect_runs == []


def test_quickops_stabilize(shaky_clip, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(shaky_clip.parent / "quick_stab.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(shaky_clip),
        "operations": [{"type": "stabilize", "strength": "medium"}],
        "output_path": out,
    }))
    assert "ok: stabilize" in text, text
    assert _mean_frame_diff(out) < _mean_frame_diff(str(shaky_clip)) * 0.8


def _dup_fraction(path: str) -> float:
    """Fraction of consecutive-frame pairs that are (near-)identical —
    ≈0.75 for 0.25× duplicate slow motion, ≈0 for real synthesis."""
    diffs = _frame_diffs(path)
    return sum(1 for d in diffs if d < 0.05) / len(diffs)


def test_flow_slowmo_synthesizes_frames_and_caches(assets, monkeypatch, tmp_path):
    monkeypatch.setattr(renderer.slowmo_mod, "CACHE_DIR", tmp_path / "mezz")

    def _slow_comp(interpolate):
        comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
        clip = {"src": str(assets["clip1"]), "in": 0.5, "out": 2.0,
                "speed": 0.25, "mute": True}
        if interpolate:
            clip["interpolate"] = interpolate
        comp["tracks"][0]["clips"] = [clip]
        comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
        name = f"slomo-{interpolate or 'dup'}.vproj.json"
        path = assets["root"] / name
        comp_mod.save_composition(str(path), comp)
        return path

    dup = _run(renderer.render_composition(
        str(_slow_comp(None)), lambda p: p, mode="preview"))
    flow_path = _slow_comp("flow")
    flow = _run(renderer.render_composition(
        str(flow_path), lambda p: p, mode="preview"))

    # 1.5s span at 0.25× → 6s timeline, both modes.
    assert abs(dup["duration"] - 6.0) < 0.2
    assert abs(flow["duration"] - 6.0) < 0.2

    # Duplicate mode repeats each frame ~4×; flow synthesizes real frames.
    assert _dup_fraction(dup["output"]) > 0.6
    assert _dup_fraction(flow["output"]) < 0.2

    mezz = list((tmp_path / "mezz").glob("mezz-*.mp4"))
    assert len(mezz) == 1
    assert any("mezzanine: 1 built" in w["message"] for w in flow["warnings"])

    # Second render: cache hit — no new mezzanine encode.
    calls = []
    real_run = renderer.slowmo_mod.run_ffmpeg

    async def counting_run(args, **kw):
        calls.append(args)
        return await real_run(args, **kw)

    monkeypatch.setattr(renderer.slowmo_mod, "run_ffmpeg", counting_run)
    flow2 = _run(renderer.render_composition(
        str(flow_path), lambda p: p, mode="preview"))
    assert calls == []
    assert any("1 reused" in w["message"] for w in flow2["warnings"])


def test_native_high_fps_slowmo_skips_interpolation(assets, monkeypatch, tmp_path):
    """A 60 fps source at 0.5× on a 30 fps timeline retimes natively —
    no mezzanine, no synthesis, and the render says so."""
    monkeypatch.setattr(renderer.slowmo_mod, "CACHE_DIR", tmp_path / "mezz")
    src60 = assets["root"] / "action60.mp4"
    if not src60.exists():
        _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=3",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(src60))
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(src60), "in": 0, "out": 2, "speed": 0.5,
         "interpolate": "flow"}]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = assets["root"] / "native60.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    assert abs(result["duration"] - 4.0) < 0.2
    assert not list((tmp_path / "mezz").glob("mezz-*.mp4"))
    assert any("natively" in w["message"] for w in result["warnings"])
    # Every output frame is a distinct native frame — no duplication judder.
    assert _dup_fraction(result["output"]) < 0.2


def test_quickops_speed_blend_interpolates(assets, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(assets["root"] / "quick_slomo.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(assets["clip1"]),
        "operations": [{"type": "trim", "start": 0, "end": 1.5},
                       {"type": "speed", "factor": 0.25,
                        "interpolate": "blend"}],
        "output_path": out,
    }))
    assert "blend-interpolated to 30 fps" in text, text
    info = _run(probe(out))
    assert abs(media_duration(info) - 6.0) < 0.3
    assert _dup_fraction(out) < 0.2


@pytest.fixture(scope="session")
def noisy_vo(tmp_path_factory):
    """Interview proxy: 300 Hz 'speech' bursts (0.5 s on per second) over a
    constant white-noise bed — the 0.55–0.95 window of every second is pure
    noise floor."""
    root = tmp_path_factory.mktemp("noisy")
    path = root / "noisy_vo.wav"
    _ff("-f", "lavfi", "-i",
        "aevalsrc=0.4*sin(2*PI*300*t)*lt(mod(t\\,1)\\,0.5):s=48000:d=4",
        "-f", "lavfi", "-i",
        "anoisesrc=color=white:amplitude=0.03:d=4:r=48000",
        "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", str(path))
    return path


def _window_volume(path: str, t0: float, t1: float, stat="mean_volume") -> float:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path),
         "-af", f"atrim=start={t0}:end={t1},asetpts=PTS-STARTPTS,volumedetect",
         "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    m = re.search(rf"{stat}:\s*(-?[\d.]+) dB", proc.stderr)
    assert m, proc.stderr[-800:]
    return float(m.group(1))


def test_denoise_drops_the_noise_floor(noisy_vo):
    def render(with_fx):
        comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
        comp["tracks"][0]["clips"] = [{"fill": "#101010", "duration": 4.0}]
        clip = {"src": str(noisy_vo), "start": 0}
        if with_fx:
            clip["audio"] = {"denoise": True, "eq": {"preset": "voice"}}
        comp["tracks"].append({"kind": "audio", "clips": [clip]})
        comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
        name = f"vo-{'fx' if with_fx else 'raw'}.vproj.json"
        path = noisy_vo.parent / name
        comp_mod.save_composition(str(path), comp)
        return _run(renderer.render_composition(
            str(path), lambda p: p, mode="final"))["output"]

    raw = render(False)
    cleaned = render(True)
    # Noise-floor window (speech burst off) must drop markedly; the speech
    # window must NOT be gutted with it.
    raw_floor = _window_volume(raw, 2.55, 2.95)
    cleaned_floor = _window_volume(cleaned, 2.55, 2.95)
    assert raw_floor - cleaned_floor >= 6.0, (raw_floor, cleaned_floor)
    raw_speech = _window_volume(raw, 2.05, 2.45)
    cleaned_speech = _window_volume(cleaned, 2.05, 2.45)
    assert cleaned_speech > raw_speech - 3.0, (raw_speech, cleaned_speech)


def test_master_limiter_caps_true_peak(assets):
    def render(limiter):
        comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
        comp["tracks"][0]["clips"] = [{"fill": "#101010", "duration": 3.0}]
        comp["tracks"].append({"kind": "audio", "clips": [
            {"src": str(assets["music"]), "start": 0, "out": 3, "gain_db": 6}]})
        comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
        if limiter:
            comp["audio_master"]["limiter"] = {"ceiling_db": -3}
        name = f"limited-{'on' if limiter else 'off'}.vproj.json"
        path = assets["root"] / name
        comp_mod.save_composition(str(path), comp)
        return _run(renderer.render_composition(
            str(path), lambda p: p, mode="final"))["output"]

    # The click track peaks near 0.9 FS, so +6 dB slams 0 dBFS unlimited.
    unlimited = _window_volume(render(False), 0, 3, stat="max_volume")
    limited = _window_volume(render(True), 0, 3, stat="max_volume")
    assert unlimited >= -1.0, unlimited
    # AAC decode overshoot costs a few tenths of a dB over the -3 ceiling.
    assert limited <= -2.0, limited
    assert unlimited - limited >= 1.5, (unlimited, limited)


def test_quickops_enhance_audio_voice(noisy_vo, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(noisy_vo.parent / "enhanced.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(noisy_vo),
        "operations": [{"type": "enhance_audio", "preset": "voice"}],
        "output_path": out,
    }))
    assert "enhanced (voice" in text, text
    assert audio_stream(_run(probe(out)))["codec_name"] == "aac"
    # The NN voice model should flatten the noise floor hard.
    assert _window_volume(str(noisy_vo), 2.55, 2.95) - \
        _window_volume(out, 2.55, 2.95) >= 8.0


@pytest.fixture(scope="session")
def tinted_clips(tmp_path_factory):
    """The same test pattern under three different grades — a stand-in for
    footage from different cameras / an AI bridge that missed the grade."""
    root = tmp_path_factory.mktemp("tinted")
    out = {}
    for name, mix in (("warm", "rr=1.25:bb=0.8"),
                      ("cold", "rr=0.75:bb=1.25"),
                      ("green", "gg=1.25:rr=0.85")):
        path = root / f"{name}.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=4",
            "-vf", f"colorchannelmixer={mix}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(path))
        out[name] = path
    out["root"] = root
    return out


def _mean_rgb(path, t):
    import numpy as np
    from PIL import Image
    png = Path(str(path)).parent / f"frame-{Path(str(path)).stem}-{t:.2f}.png"
    _ff("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(png))
    return np.asarray(Image.open(png).convert("RGB"), dtype=float).mean(axis=(0, 1))


def _cut_delta(path, t_cut, eps=0.15):
    import numpy as np
    a = _mean_rgb(path, t_cut - eps)
    b = _mean_rgb(path, t_cut + eps)
    return float(np.linalg.norm(a - b))


def test_match_color_closes_the_gap_at_a_hard_cut(tinted_clips):
    def render(matched):
        comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
        second = {"src": str(tinted_clips["cold"]), "in": 0, "out": 2}
        if matched:
            second["color"] = {"match": {
                "ref": f"{tinted_clips['warm']}@1.9"}}
        comp["tracks"][0]["clips"] = [
            {"src": str(tinted_clips["warm"]), "in": 0, "out": 2}, second]
        comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
        name = f"match-{'on' if matched else 'off'}.vproj.json"
        path = tinted_clips["root"] / name
        comp_mod.save_composition(str(path), comp)
        return _run(renderer.render_composition(
            str(path), lambda p: p, mode="preview"))["output"]

    unmatched = _cut_delta(render(False), 2.0)
    matched = _cut_delta(render(True), 2.0)
    # The warm→cold jump must collapse once the second shot is matched.
    assert matched < unmatched * 0.5, (unmatched, matched)


def test_match_ramp_grades_bridge_toward_both_neighbors(tinted_clips):
    def render(matched):
        comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
        bridge = {"src": str(tinted_clips["cold"]), "in": 0, "out": 2}
        if matched:
            bridge["color"] = {"match": {
                "ramp_from": f"{tinted_clips['warm']}@1.9",
                "ramp_to": f"{tinted_clips['green']}@0.1"}}
        comp["tracks"][0]["clips"] = [
            {"src": str(tinted_clips["warm"]), "in": 0, "out": 2},
            bridge,
            {"src": str(tinted_clips["green"]), "in": 0, "out": 2}]
        comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
        name = f"ramp-{'on' if matched else 'off'}.vproj.json"
        path = tinted_clips["root"] / name
        comp_mod.save_composition(str(path), comp)
        return _run(renderer.render_composition(
            str(path), lambda p: p, mode="preview"))["output"]

    raw = render(False)
    fixed = render(True)
    # Both hard cuts must collapse: entry matched toward the warm clip,
    # exit ramped toward the green one — one clip, two grades, no fades.
    assert _cut_delta(fixed, 2.0) < _cut_delta(raw, 2.0) * 0.5
    assert _cut_delta(fixed, 4.0) < _cut_delta(raw, 4.0) * 0.5


def test_quickops_match_color(tinted_clips, monkeypatch):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(tinted_clips["root"] / "quick_match.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(tinted_clips["cold"]),
        "operations": [{"type": "match_color",
                        "ref": f"{tinted_clips['warm']}@1.0"}],
        "output_path": out,
    }))
    assert "color-matched to warm.mp4@1" in text, text
    import numpy as np
    want = _mean_rgb(tinted_clips["warm"], 1.0)
    before = float(np.linalg.norm(_mean_rgb(tinted_clips["cold"], 1.0) - want))
    after = float(np.linalg.norm(_mean_rgb(out, 1.0) - want))
    assert after < before * 0.5, (before, after)


def test_render_composition_handler_notifies_file_written(demo_comp, monkeypatch):
    """The render handler must push the output through the file-written
    hook — without it, satellite installs never receive the deliverable
    and the advertised display_video step 400s (hit live)."""
    import project

    notified = []

    async def fake_notify(path):
        notified.append(path)
        return True

    monkeypatch.setattr(project, "_resolve_path", lambda p: p)
    monkeypatch.setattr(project, "_notify_file_written", fake_notify)
    text = _run(project.handle_render_composition(
        {"path": str(demo_comp), "mode": "preview"}))
    assert "Rendered (preview)" in text
    assert len(notified) == 1 and notified[0].endswith("demo.preview.mp4")


def test_wow_preset_reel_renders_all_presets(assets):
    """One reel through every wow preset: hard cuts throughout (no xfade
    overlap → duration is the plain sum), and the flash_cut frame right
    after its cut must actually be bright."""
    presets = ["whip_pan", "zoom_punch", "flash_cut", "glitch", "spin",
               "shake", "zoom_out", "zoom_in"]
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    clips = [{"src": str(assets["clip1"]), "in": 0, "out": 1.5, "mute": True}]
    for i, preset in enumerate(presets):
        src = assets["clip2"] if i % 2 == 0 else assets["clip1"]
        tr = {"type": preset, "duration": 0.3}
        if preset == "flash_cut":
            tr["flash"] = "white"   # the luma assertion below reads the pop
        clips.append({"src": str(src), "in": 0, "out": 1.5, "mute": True,
                      "transition_in": tr})
    comp["tracks"][0]["clips"] = clips
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = assets["root"] / "wow-reel.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0
    assert abs(result["duration"] - 13.5) < 0.2       # 9 × 1.5s, no overlap

    # flash_cut is the 3rd transition → its cut sits at t = 4.5s. The very
    # next frame is the white flash; compare its luma to a calm frame.
    flash = float(_mean_rgb(out, 4.53).mean())
    calm = float(_mean_rgb(out, 5.2).mean())
    assert flash > calm + 60, (flash, calm)


def test_whip_and_luma_wipe_render(assets):
    """The overlapping premium presets execute end-to-end: whip_left's
    slide core + blur, and luma_wipe's custom xfade expr (a bad expr kills
    the whole graph, so a successful render IS the assertion)."""
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(assets["clip1"]), "in": 0, "out": 2, "mute": True},
        {"src": str(assets["clip2"]), "in": 0, "out": 2, "mute": True,
         "transition_in": {"type": "whip_left", "duration": 0.4}},
        {"src": str(assets["clip1"]), "in": 2.5, "out": 4.5, "mute": True,
         "transition_in": {"type": "luma_wipe", "duration": 0.8}},
        # An xfade AFTER the wipe: the wipe's concat reassembly must keep
        # CFR metadata or this xfade kills the graph with EINVAL (-22) —
        # hit live on the premium reel.
        {"src": str(assets["clip2"]), "in": 2.5, "out": 4.5, "mute": True,
         "transition_in": {"type": "fadeblack", "duration": 0.5}},
    ]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = assets["root"] / "premium-transitions.vproj.json"
    comp_mod.save_composition(str(path), comp)
    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0
    assert abs(result["duration"] - 6.3) < 0.2      # 8 − 0.4 − 0.8 − 0.5
    # COLOR must survive: an unpinned wipe graph once let the mask's gray
    # constraint back-propagate and grayscale the whole timeline. Judge by
    # per-pixel saturation (mean RGB of a pattern averages to near-gray).
    import numpy as np
    from PIL import Image
    for t in (1.0, 2.0, 5.5):
        png = Path(out).parent / f"sat-{t:.1f}.png"
        _ff("-ss", f"{t:.3f}", "-i", str(out), "-frames:v", "1", str(png))
        px = np.asarray(Image.open(png).convert("RGB"), dtype=float)
        sat = float((px.max(axis=2) - px.min(axis=2)).mean())
        assert sat > 15, (t, sat)
    # Mid-whip must be a coherent (wrapped, blurred) image, not static:
    # st/ld in the per-pixel whip expr once raced across threads and
    # rendered full-frame noise. Noise has huge neighbor gradients.
    png = Path(out).parent / "midwhip.png"
    _ff("-ss", "1.80", "-i", str(out), "-frames:v", "1", str(png))
    px = np.asarray(Image.open(png).convert("L"), dtype=float)
    grad = float(np.abs(px[:, 1:] - px[:, :-1]).mean())
    assert grad < 25, grad


def test_final_render_on_silent_timeline_skips_loudnorm(assets):
    """All-silent mix (fill clips synthesize anullsrc audio): loudnorm
    pass 1 measures input_i = -inf, which pass 2 rejects outright ("Value
    -inf for parameter 'measured_I' out of range" → ffmpeg exit 222 killed
    the whole render; hit live on silent drone footage). The
    renderer must skip normalization, flag it, and still deliver."""
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"fill": "#204060", "duration": 1.0},
        {"fill": "#602040", "duration": 1.0,
         "transition_in": {"type": "fade", "duration": 0.3}},
    ]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": {"target_lufs": -16}}
    path = assets["root"] / "silent.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(
        str(path), lambda p: p, mode="final"))
    out = Path(result["output"])
    assert out.exists() and out.stat().st_size > 0
    assert any("loudness normalization skipped" in w["message"]
               for w in result["warnings"]), result["warnings"]
    # The delivered file still carries a (silent) AAC track — players and
    # concat tooling expect an audio stream to exist.
    assert audio_stream(_run(probe(str(out))))["codec_name"] == "aac"


def test_speed_ramp_slows_progressively(assets):
    """A 4x→0.5x linear ramp on constant-motion footage: per-output-frame
    motion energy is proportional to the instantaneous speed, so the head
    of the render must carry several times the motion of the tail — the
    A/B that separates a real ramp from a constant retime. Duration must
    match the segment math."""
    import speedramp

    ramp = {"from": 4.0, "to": 0.5, "curve": "linear"}
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(assets["clip1"]), "in": 0.5, "out": 4.5, "mute": True,
         "speed_ramp": ramp},
    ]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = assets["root"] / "ramp.vproj.json"
    comp_mod.save_composition(str(path), comp)

    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    out = Path(result["output"])
    expected = speedramp.output_duration(4.0, ramp)
    # ±½ output frame of fps-quantization per segment join.
    assert abs(result["duration"] - expected) < 0.2, (result["duration"],
                                                      expected)
    assert any(w["where"] == "speed_ramp" for w in result["warnings"])

    diffs = _frame_diffs(str(out))
    n = len(diffs)
    head = sum(diffs[: n // 5]) / (n // 5)          # ~3.7x segment
    tail = sum(diffs[-n // 3:]) / (n // 3)          # ~0.79x segment
    assert head > tail * 2, (head, tail)


def test_vfr_source_duration_pinned(tmp_path):
    """VFR reality check (phone/drone footage): a trimmed span of a VFR
    source decodes SHORT of span/speed — the deficit divided by the speed —
    so xfade offsets fired late, the final fade truncated, and the
    (sample-exact) audio drifted against the video. Found live on a
    6-segment ramp of 24.3fps VFR drone footage (0.8 s short). The
    duration pin (tpad clone + trim) must make the video stream match
    compute_timeline exactly."""
    src = tmp_path / "vfr.mp4"
    # Drop every 7th frame keeping timestamps → true VFR.
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
        "-vf", "select='not(eq(mod(n,7),3))'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(src))

    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": str(src), "in": 0.5, "out": 4.5, "speed": 0.5, "mute": True},
        {"fill": "#000000", "duration": 1.0,
         "transition_in": {"type": "fadeblack", "duration": 0.5}},
    ]
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = tmp_path / "vfr.vproj.json"
    comp_mod.save_composition(str(path), comp)

    expected = comp_mod.compute_timeline(comp, None)["duration"]
    assert expected == pytest.approx(8.5)
    result = _run(renderer.render_composition(str(path), lambda p: p,
                                              mode="preview"))
    vs = video_stream(_run(probe(result["output"])))
    vdur = float(vs.get("duration") or 0)
    assert abs(vdur - expected) < 0.1, (vdur, expected)


# ---------------------------------------------------------------------------
# Colour management (0.4.0): 709 timeline contract, HLG convert, grade mix,
# sharpening, LUT chains, probe report, audio alignment
# ---------------------------------------------------------------------------

_HLG_TAG = ("setparams=colorspace=bt2020nc:range=tv:color_primaries=bt2020"
            ":color_trc=arib-std-b67")
# SDR → HLG: the exact inverse of the convert preset's anchor (SDR white at
# 203 nits inside a 1000-nit HLG container).
_SDR_TO_HLG = ("setparams=colorspace=bt709:range=tv:color_primaries=bt709"
               ":color_trc=bt709,zscale=t=linear:npl=100,format=gbrpf32le,"
               "exposure=exposure=-2.3,"
               "zscale=p=bt2020:t=arib-std-b67:m=bt2020nc:r=tv:npl=1000,"
               "format=yuv420p")


def _stored_yuv(path, x, y, t=0.2, size=8):
    """Stored (un-converted) yuv420p values of a size×size patch."""
    import numpy as np
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
         "-i", str(path), "-frames:v", "1",
         "-vf", f"crop={size}:{size}:{x}:{y},format=yuv420p",
         "-f", "rawvideo", "-"], capture_output=True, check=True)
    b = np.frombuffer(proc.stdout, np.uint8)
    n = size * size
    return (float(b[:n].mean()), float(b[n:n + n // 4].mean()),
            float(b[n + n // 4:].mean()))


def _stream_tags(path):
    return stream_color(video_stream(_run(probe(str(path)))))


def _frame_rgb(path, t):
    import numpy as np
    from PIL import Image
    png = Path(str(path)).parent / f"rgb-{Path(str(path)).stem}-{t:.2f}.png"
    _ff("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(png))
    return np.asarray(Image.open(png).convert("RGB"), dtype=float)


@pytest.fixture(scope="session")
def color_assets(assets):
    root = assets["root"]
    # A softened SDR copy (testsrc2 is nearly all saturated primaries, which
    # a highlight knee legitimately compresses) is the round-trip reference.
    soft = root / "clip1_soft.mp4"
    _ff("-i", str(assets["clip1"]), "-vf",
        "eq=brightness=-0.15:saturation=0.5,setparams=colorspace=bt709:range=tv"
        ":color_primaries=bt709:color_trc=bt709,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-an", str(soft))
    hlg = root / "clip1_hlg.mp4"          # tagged in-graph: VUI + colr
    _ff("-i", str(soft), "-vf", _SDR_TO_HLG, "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "12", "-an", str(hlg))
    vui = root / "bars_vui.mp4"           # VUI-only tags, no colr (camera shape)
    _ff("-f", "lavfi", "-i", "smptehdbars=size=320x180:rate=30:duration=1",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-x264-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
        str(vui))
    tc = root / "bars_tc.mp4"             # colr + tmcd
    _ff("-f", "lavfi", "-i", "smptehdbars=size=320x180:rate=30:duration=1",
        "-vf", _HLG_TAG + ",format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
        "-timecode", "01:02:03:04", str(tc))
    green = root / "green.png"
    from PIL import Image
    Image.new("RGB", (64, 64), (0, 255, 0)).save(green)
    return {"hlg": hlg, "soft": soft, "vui": vui, "tc": tc, "green": green}


def _render_clip(assets, name, clip, project_color=None):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = [clip]
    if project_color:
        comp["project"]["color"] = project_color
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = assets["root"] / f"{name}.vproj.json"
    comp_mod.save_composition(str(path), comp)
    return Path(_run(renderer.render_composition(
        str(path), lambda p: p, mode="preview"))["output"])


def test_output_is_tagged_rec709_with_colr(assets):
    out = _render_clip(assets, "tags709", {"src": str(assets["clip1"]), "in": 0, "out": 1})
    assert _stream_tags(out) == {"color_space": "bt709", "color_transfer": "bt709",
                                 "color_primaries": "bt709", "color_range": "tv"}
    assert (colr_box(str(out)) or {}).get("matrix") == "bt709"


def test_fill_and_still_use_the_709_matrix(assets, color_assets):
    # Pure green stores as Y≈173/U≈42/V≈26 through the 709 matrix; the old
    # 601 path stored 145/54/34 (then played back as 709 — desaturated).
    fill = _render_clip(assets, "fill709", {"fill": "#00ff00", "duration": 1.0})
    still = _render_clip(assets, "still709", {"image": str(color_assets["green"]),
                                              "duration": 1.0, "fit": "contain"})
    for out in (fill, still):
        y, u, v = _stored_yuv(out, 316, 176)
        assert 165 <= y <= 180 and u < 48 and v < 32, (out, y, u, v)


def test_hlg_convert_round_trip(assets, color_assets):
    import numpy as np
    converted = _render_clip(assets, "hlgconv", {
        "src": str(color_assets["hlg"]), "in": 0, "out": 2,
        "color": {"convert": "hlg->rec709"}})
    raw = _render_clip(assets, "hlgraw", {"src": str(color_assets["hlg"]), "in": 0, "out": 2})
    original = _render_clip(assets, "sdrref", {"src": str(color_assets["soft"]), "in": 0, "out": 2})
    ref = _frame_rgb(original, 1.0)
    conv = _frame_rgb(converted, 1.0)
    untouched = _frame_rgb(raw, 1.0)
    # Below the highlight knee (every channel under ~0.5 linear) the
    # SDR→HLG→SDR round trip is exact up to codec noise — the knee acts on
    # a pixel's brightest channel, so the mask is on the max channel; the
    # unconverted HLG frame is far off.
    mask = ref.max(axis=2) < 150
    assert mask.mean() > 0.5
    diff_conv = float(np.abs(conv - ref)[mask].mean())
    diff_raw = float(np.abs(untouched - ref)[mask].mean())
    assert diff_conv < 6.0, diff_conv
    assert diff_raw > diff_conv * 3, (diff_raw, diff_conv)
    assert float(np.abs(untouched - ref).mean()) > float(np.abs(conv - ref).mean()) * 2
    assert _stream_tags(converted)["color_transfer"] == "bt709"


def test_grade_strength_mixes_halfway(assets):
    lum = {}
    for s in (0.0, 0.5, 1.0):
        out = _render_clip(assets, f"strength{s}", {
            "src": str(assets["clip1"]), "in": 0, "out": 1,
            "color": {"exposure": 1.0, "strength": s}})
        lum[s] = float(_frame_rgb(out, 0.5).mean())
    # testsrc2 is mostly saturated primaries, so +1 EV moves the frame mean
    # only a few levels — the mix must still land on the midpoint.
    assert lum[1.0] - lum[0.0] > 4, lum
    assert abs(lum[0.5] - (lum[0.0] + lum[1.0]) / 2) < 1.5, lum


def _laplacian_var(path, t):
    import cv2
    gray = cv2.cvtColor(_frame_rgb(path, t).astype("uint8"), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def test_sharpness_and_clarity_raise_detail(assets):
    base = _laplacian_var(_render_clip(assets, "sharp0", {
        "src": str(assets["clip1"]), "in": 0, "out": 1}), 0.5)
    sharp = _laplacian_var(_render_clip(assets, "sharp1", {
        "src": str(assets["clip1"]), "in": 0, "out": 1,
        "color": {"sharpness": 1.0}}), 0.5)
    clear = _laplacian_var(_render_clip(assets, "clarity1", {
        "src": str(assets["clip1"]), "in": 0, "out": 1,
        "color": {"clarity": 1.0}}), 0.5)
    assert sharp > base * 1.15, (base, sharp)
    assert clear > base * 1.05, (base, clear)


def test_lut_chain_strength_lands_between(assets):
    import numpy as np

    def chroma(path):
        f = _frame_rgb(path, 0.5)
        return float(np.abs(f - f.mean(axis=2, keepdims=True)).mean())

    plain = chroma(_render_clip(assets, "lutc0", {"src": str(assets["clip1"]), "in": 0, "out": 1}))
    full = chroma(_render_clip(assets, "lutc1", {
        "src": str(assets["clip1"]), "in": 0, "out": 1,
        "color": {"lut": ["clean-punch", "bw-classic"]}}))
    half = chroma(_render_clip(assets, "lutc5", {
        "src": str(assets["clip1"]), "in": 0, "out": 1,
        "color": {"lut": ["clean-punch", {"lut": "bw-classic", "strength": 0.5}]}}))
    assert full < plain * 0.15, (plain, full)
    assert abs(half - plain / 2) < plain * 0.15, (plain, half)


def test_probe_media_reports_color_colr_and_timecode(assets, color_assets, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    tc = _run(analysis.handle_probe_media({"path": str(color_assets["tc"])}))
    assert "transfer=arib-std-b67 (HLG)" in tc and "matrix=bt2020nc" in tc, tc
    assert "colr box: present (nclx: primaries=bt2020" in tc, tc
    assert "timecode: 01:02:03:04 (tmcd track)" in tc, tc
    assert 'color.convert "hlg->rec709"' in tc
    vui = _run(analysis.handle_probe_media({"path": str(color_assets["vui"])}))
    assert "transfer=arib-std-b67 (HLG)" in vui and "colr box: absent" in vui, vui
    assert "timecode" not in vui
    sdr = _run(analysis.handle_probe_media({"path": str(assets["clip1"])}))
    assert "color: matrix=unknown" in sdr and "HLG source" not in sdr, sdr


@pytest.fixture(scope="session")
def sync_pair(tmp_path_factory):
    """An aperiodic gated-noise 'event' and two re-recordings of it: one
    delayed 1.234 s under added pink noise at half level, one that starts
    0.8 s late (trimmed)."""
    root = tmp_path_factory.mktemp("sync")
    ref = root / "ref.wav"
    _ff("-f", "lavfi", "-i",
        "aevalsrc=0.6*random(0)*gt(sin(2*PI*0.37*t)+sin(2*PI*0.61*t)+sin(2*PI*1.1*t)\\,1.2)"
        ":s=22050:d=20",
        "-c:a", "pcm_s16le", str(ref))
    late = root / "late.wav"
    _ff("-i", str(ref), "-f", "lavfi", "-i",
        "anoisesrc=color=pink:seed=3:amplitude=0.05:duration=25:sample_rate=22050",
        "-filter_complex",
        "[0:a]adelay=1234:all=1,volume=0.5[a];"
        "[a][1:a]amix=inputs=2:duration=first:normalize=0[o]",
        "-map", "[o]", "-c:a", "pcm_s16le", str(late))
    early = root / "early.wav"
    _ff("-i", str(ref), "-af", "atrim=start=0.8,asetpts=PTS-STARTPTS",
        "-c:a", "pcm_s16le", str(early))
    return {"ref": ref, "late": late, "early": early}


def test_align_audio_recovers_known_offsets(sync_pair, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    text = _run(analysis.handle_align_audio({
        "ref": str(sync_pair["ref"]), "target": str(sync_pair["late"])}))
    m = re.search(r"offset: ([+-]\d+\.\d+) s", text)
    assert m, text
    assert abs(float(m.group(1)) - 1.234) < 0.001, text
    assert "confidence: strong" in text, text
    assert "target clip in = ref.in +1.2340" in text, text
    text2 = _run(analysis.handle_align_audio({
        "ref": str(sync_pair["ref"]), "target": str(sync_pair["early"]),
        "max_offset": 5}))
    m2 = re.search(r"offset: ([+-]\d+\.\d+) s", text2)
    assert m2 and abs(float(m2.group(1)) + 0.8) < 0.001, text2
    assert "confidence: strong" in text2, text2


def test_align_audio_on_stationary_ambience(sync_pair, tmp_path, monkeypatch):
    """Sea/wind-style ambience has no onsets — the case that broke an
    envelope-based estimator live (drone clip: −0.019 s for a true
    +1.750 s). Pink noise band-limited into a slowly breathing wash, re-
    recorded 1.75 s late at −6 dB under independent noise."""
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    ref = tmp_path / "amb_ref.wav"
    _ff("-f", "lavfi", "-i",
        "anoisesrc=color=pink:seed=21:amplitude=0.4:duration=12:sample_rate=22050",
        "-af", "lowpass=f=1800,tremolo=f=0.23:d=0.6", "-c:a", "pcm_s16le", str(ref))
    late = tmp_path / "amb_late.wav"
    _ff("-i", str(ref), "-f", "lavfi", "-i",
        "anoisesrc=color=pink:seed=5:amplitude=0.08:duration=15:sample_rate=22050",
        "-filter_complex",
        "[0:a]adelay=1750:all=1,volume=-6dB[a];"
        "[a][1:a]amix=inputs=2:duration=first:normalize=0[o]",
        "-map", "[o]", "-c:a", "pcm_s16le", str(late))
    text = _run(analysis.handle_align_audio({"ref": str(ref), "target": str(late)}))
    m = re.search(r"offset: ([+-]\d+\.\d+) s", text)
    assert m and abs(float(m.group(1)) - 1.75) < 0.001, text
    assert "confidence: strong" in text, text


def test_quickops_speed_ramp(assets, monkeypatch):
    import speedramp

    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(assets["root"] / "quick_ramp.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(assets["clip1"]),
        "operations": [{"type": "speed_ramp", "from": 2.0, "to": 0.5,
                        "curve": "ease_out"}],
        "output_path": out,
    }))
    assert "ok: speed_ramp" in text, text
    expected = speedramp.output_duration(
        5.0, {"from": 2.0, "to": 0.5, "curve": "ease_out"})
    dur = media_duration(_run(probe(out)))
    assert abs(dur - expected) < 0.25, (dur, expected)
