"""Frame-grid regression suite (the 2026-08 windowed-render bug).

The windowed renderer substitutes out-of-window clips with background fills;
an unpinned `color=…:d=X` source emits ceil(X·fps) frames while the media
pin emits round-half-away(X·fps) — one extra frame per off-grid duration,
drifting every window head onto bare background. These tests lock down:
(a) the shared frame-count helper's rounding, (b) compute_timeline's
quantization, (c) end-to-end: a windowed render of an off-grid composition
(speeded clip, exact-half duration, spanning alpha overlay) is
frame-count-identical and visually identical at window heads to the
single-graph render. The existing smoke tests never caught this because
every duration there sat exactly on the frame grid.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from conftest import HAVE_FFMPEG

import composition as comp_mod  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Unit: rounding + quantization (no ffmpeg needed)
# ---------------------------------------------------------------------------


def test_frames_for_half_away_rounding():
    # Duration values from the production render that exposed the bug (fps 30).
    assert comp_mod.frames_for(2.81, 30) == 84    # 84.3 → 84
    assert comp_mod.frames_for(3.27, 30) == 98    # 98.1 → 98
    assert comp_mod.frames_for(3.77, 30) == 113   # 113.1 → 113
    assert comp_mod.frames_for(3.26, 30) == 98    # 97.8 → 98
    # Exact half MUST round away from zero like ffmpeg — Python's round()
    # is banker's and would give 112.
    assert comp_mod.frames_for(3.75, 30) == 113   # 112.5 → 113
    assert comp_mod.frames_for(11.25, 30) == 338  # 337.5 → 338
    # Speeded duration: 5.4 / 1.65 → 3.2727…
    assert comp_mod.frames_for(5.4 / 1.65, 30) == 98


def test_compute_timeline_quantizes_to_frame_grid():
    comp = {
        "project": {"width": 320, "height": 180, "fps": 30},
        "tracks": [{"kind": "video", "clips": [
            {"fill": "#050f1a", "duration": 2.81},
            {"fill": "#050f1a", "duration": 3.27},
            {"fill": "#050f1a", "duration": 3.75},
        ]}],
    }
    tl = comp_mod.compute_timeline(comp)
    frames = [round(e["duration"] * 30) for e in tl["base"]]
    assert frames == [84, 98, 113]
    # Entries store 6-decimal-rounded seconds, so "on the grid" means within
    # ~3e-5 frames — far below the half-frame the pre-fix drift produced;
    # exact frame COUNTS are guaranteed by the fill pin, not entry floats.
    for e in tl["base"]:
        for key in ("start", "end", "duration"):
            v = e[key] * 30
            assert abs(v - round(v)) < 1e-3, (key, e)
    # A sub-half-frame clip clamps to one frame instead of vanishing.
    tiny = {
        "project": {"fps": 30},
        "tracks": [{"kind": "video",
                    "clips": [{"fill": "#000", "duration": 0.01}]}],
    }
    assert comp_mod.compute_timeline(tiny)["base"][0]["duration"] == \
        pytest.approx(1 / 30, abs=1e-6)


# ---------------------------------------------------------------------------
# End-to-end: windowed == single graph on an off-grid composition
# ---------------------------------------------------------------------------

pytestmark_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG,
                                       reason="ffmpeg not available")


def _ff(*args):
    from fftools import FFMPEG
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    *args], check=True, timeout=300)


def _count_frames(path) -> int:
    probe = os.environ.get("FFPROBE_PATH", "ffprobe")
    out = subprocess.run(
        [probe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
         str(path)],
        capture_output=True, text=True, check=True, timeout=300)
    return int(out.stdout.strip())


def _mean_rgb(path, t):
    import numpy as np
    from PIL import Image
    png = Path(str(path)).parent / f"fg-{Path(str(path)).stem}-{t:.3f}.png"
    _ff("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(png))
    return np.asarray(Image.open(png).convert("RGB"), dtype=float).mean(
        axis=(0, 1))


@pytest.fixture(scope="module")
def offgrid_assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("offgrid")
    src = root / "src.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(src))
    overlay = root / "overlay.webm"
    _ff("-f", "lavfi", "-i",
        "color=c=red@0.4:s=64x64:d=17,format=yuva420p",
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", str(overlay))
    return {"root": root, "src": src, "overlay": overlay}


@pytestmark_ffmpeg
def test_windowed_render_matches_single_on_offgrid_durations(
        offgrid_assets, monkeypatch):
    src = str(offgrid_assets["src"])
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 30})
    comp["tracks"][0]["clips"] = [
        {"src": src, "in": 0, "out": 2.81, "mute": True},
        {"src": src, "in": 0, "out": 3.27, "mute": True},
        {"src": src, "in": 0, "out": 3.77, "mute": True},
        {"src": src, "in": 0, "out": 3.75, "mute": True},          # exact half
        {"src": src, "in": 0, "out": 5.4, "speed": 1.65, "mute": True},
    ]
    comp["tracks"].append({"kind": "overlay", "clips": [
        {"src": str(offgrid_assets["overlay"]), "start": 0.5}]})
    comp["audio_master"] = {"gain_db": 0, "loudnorm": False}
    path = offgrid_assets["root"] / "offgrid.vproj.json"
    comp_mod.save_composition(str(path), comp)

    import renderer

    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "100000")
    single = _run(renderer.render_composition(
        str(path), lambda p: p, mode="preview",
        output_path=str(offgrid_assets["root"] / "single.mp4")))
    assert not any("windows" in w["message"] for w in single["warnings"])

    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "8")
    seg = _run(renderer.render_composition(
        str(path), lambda p: p, mode="preview",
        output_path=str(offgrid_assets["root"] / "seg.mp4")))
    assert any("windows" in w["message"] for w in seg["warnings"]), \
        seg["warnings"]

    # 84 + 98 + 113 + 113 + 98 frames — media rounding, fills pinned to it.
    expected = 506
    n_single = _count_frames(offgrid_assets["root"] / "single.mp4")
    n_seg = _count_frames(offgrid_assets["root"] / "seg.mp4")
    assert n_single == expected, n_single
    assert n_seg == expected, n_seg
    assert abs(seg["duration"] - single["duration"]) < 0.05

    # The bug signature: window heads opened on bare-background frames.
    # Compare the first frames after every quantized cut against the
    # single-graph render — testsrc2 content is busy, background is flat,
    # so a drifted fill frame diverges by far more than encode noise.
    import numpy as np
    cuts = [84 / 30, 182 / 30, 295 / 30, 408 / 30]
    for t_cut in cuts:
        for dt in (0.5 / 30, 2.5 / 30):
            a = _mean_rgb(offgrid_assets["root"] / "single.mp4", t_cut + dt)
            b = _mean_rgb(offgrid_assets["root"] / "seg.mp4", t_cut + dt)
            assert float(np.abs(a - b).mean()) < 8.0, (t_cut, dt, a, b)
