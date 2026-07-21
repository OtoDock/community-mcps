"""smart_reframe: planning logic, step expressions, and an end-to-end render
where a fake detector tracks a synthetic red subject — asserting the crop
window actually follows it across the shot boundary."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from conftest import HAVE_FFMPEG

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")

import quickops  # noqa: E402
import reframe  # noqa: E402
from fftools import FFMPEG, probe, video_stream  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _fake_detect(frame, detector=None):
    """Stand-in subject detector: centroid of strongly-red pixels."""
    import numpy as np

    r = frame[:, :, 2].astype(int)
    g = frame[:, :, 1].astype(int)
    b = frame[:, :, 0].astype(int)
    mask = (r > 180) & (g < 90) & (b < 90)
    if not mask.any():
        return []
    ys, xs = np.nonzero(mask)
    return [(float(xs.mean()), float(ys.mean()), 1000.0)]


@pytest.fixture(scope="module")
def two_shot(tmp_path_factory):
    """6s 640x360: navy shot with a red subject LEFT (center x=113), then a
    dark-green shot with the subject RIGHT (center x=513)."""
    root = tmp_path_factory.mktemp("reframe")
    a, b, out = root / "a.mp4", root / "b.mp4", root / "two.mp4"

    def ff(*args):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        *args], check=True, timeout=300)

    ff("-f", "lavfi", "-i",
       "color=c=0x000040:s=640x360:r=30:d=3,"
       "drawbox=x=88:y=130:w=50:h=100:color=red:t=fill", str(a))
    ff("-f", "lavfi", "-i",
       "color=c=0x004000:s=640x360:r=30:d=3,"
       "drawbox=x=488:y=130:w=50:h=100:color=red:t=fill", str(b))
    ff("-i", str(a), "-i", str(b), "-filter_complex",
       "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
       "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", str(out))
    return out


def test_yunet_model_loads():
    det = reframe._detector(640, 360)
    assert det is not None


def test_step_expr_piecewise_constant():
    segs = [{"end": 3.0, "x": 12.0}, {"end": 6.0, "x": 412.0}]
    assert reframe.step_expr(segs, "x") == "if(lt(t,3),12,412)"
    assert reframe.step_expr([{"end": 5, "x": 7.5}], "x") == "7.5"


def test_plan_segments_tracks_subject(two_shot, monkeypatch):
    monkeypatch.setattr(reframe, "detect_faces_bgr", _fake_detect)
    segs = reframe.plan_segments(str(two_shot), [(0.0, 3.0), (3.0, 6.0)],
                                 202, 360, 640, 360)
    assert len(segs) == 2
    assert segs[0]["faces"] > 0 and segs[1]["faces"] > 0
    assert abs(segs[0]["x"] - (113 - 101)) < 8      # subject left → window left
    assert abs(segs[1]["x"] - (513 - 101)) < 8      # subject right → window right


def test_plan_segments_center_fallback(two_shot, monkeypatch):
    monkeypatch.setattr(reframe, "detect_faces_bgr", lambda f, detector=None: [])
    segs = reframe.plan_segments(str(two_shot), [(0.0, 6.0)], 202, 360, 640, 360)
    assert segs[0]["faces"] == 0
    assert abs(segs[0]["x"] - (320 - 101)) < 1      # dead center


def test_smart_reframe_end_to_end(two_shot, monkeypatch, tmp_path):
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    monkeypatch.setattr(reframe, "detect_faces_bgr", _fake_detect)
    out = str(tmp_path / "vertical.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(two_shot),
        "operations": [{"type": "smart_reframe", "aspect": "9:16"}],
        "output_path": out,
    }))
    assert "error" not in text.splitlines()[0], text
    assert "subject-tracked" in text
    info = _run(probe(out))
    vs = video_stream(info)
    assert (vs["width"], vs["height"]) == (202, 360)

    # The subject must sit near the horizontal CENTER of the crop in BOTH
    # shots — i.e. the window moved with it across the cut.
    import cv2

    cap = cv2.VideoCapture(out)
    try:
        for t in (1.5, 4.5):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            assert ok
            faces = _fake_detect(frame)
            assert faces, f"subject not visible in crop at t={t}"
            cx = faces[0][0]
            assert abs(cx - 101) < 25, f"subject off-center at t={t}: {cx}"
    finally:
        cap.release()
