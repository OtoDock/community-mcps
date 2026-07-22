"""Object tracking: path smoothing math (pure) + real tracker/blur
execution on a synthesized moving-subject clip."""

import asyncio
import json
import re
import subprocess

import pytest

from conftest import HAVE_FFMPEG

import quickops  # noqa: E402
import reframe  # noqa: E402
import track  # noqa: E402
from fftools import FFMPEG  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def _pts(centers, size=40.0, fps=25.0):
    return [{"t": i / fps, "x": c - size / 2, "y": 100.0, "w": size,
             "h": size, "score": 1.0} for i, c in enumerate(centers)]


def test_smooth_path_kills_spikes_and_damps():
    centers = [100.0 + i for i in range(20)]
    centers[10] = 400.0  # single-frame tracker jump
    smoothed = track.smooth_path(_pts(centers), strength=0.0)
    cx10 = smoothed[10]["cx"]
    assert cx10 < 130, cx10           # the median filter removed the spike
    heavy = track.smooth_path(_pts([100.0] * 10 + [300.0] * 10), strength=1.0)
    # Heavy damping: right after the step the path has barely moved.
    assert heavy[10]["cx"] < 140


def test_to_keyframes_offsets_and_relative_t():
    smoothed = track.smooth_path(_pts([320.0] * 6), strength=0.0)
    for p in smoothed:
        p["t"] += 2.0  # span starting at 2s
    kfs = track.to_keyframes(smoothed, 640, 360, interval=0.1)
    assert kfs[0]["t"] == 0.0                       # relative to span start
    # cx = 320 → dx 0; cy = y 100 + h/2 20 = 120 → dy 120-180 = -60.
    assert kfs[0]["pos"] == [0, -60]
    assert kfs[-1]["t"] == pytest.approx(0.2, abs=0.01)


def test_follow_controller_deadzone_holds_still():
    # Wobble inside the deadzone → the crop never moves at all.
    centers = [320.0 + (5 if i % 2 else -5) for i in range(26)]
    smoothed = track.smooth_path(_pts(centers), strength=0.0)
    xs, _ = track.follow_crop_keypoints(smoothed, 200, 360, 640, 360,
                                        smoothness=0.7, interval=0.2)
    assert len({x for _, x in xs}) == 1


def test_follow_controller_safety_beats_the_speed_cap():
    # Subject sprints 400 px/s — even at max smoothness the SAFETY pull
    # keeps its center inside the crop's central 80% at every keypoint
    # (a plain velocity clamp let an accelerating subject leave frame).
    centers = [100.0 + 400.0 * (i / 25.0) for i in range(26)]
    smoothed = track.smooth_path(_pts(centers), strength=0.0)
    xs, ys = track.follow_crop_keypoints(smoothed, 200, 360, 640, 360,
                                         smoothness=1.0, interval=0.2)
    lookup = dict(xs)
    for p in track._sample(smoothed, 0.2)[1:]:
        x = lookup[p["t"]]
        assert 0 <= x <= 440
        # +0.1: keypoints are rounded to 0.1 px after the margin check.
        assert abs(p["cx"] - (x + 100)) <= 80.1, (p["t"], p["cx"], x)
    assert all(y == 0 for _, y in ys)               # crop_h == src_h


def test_box_distinctiveness_flags_empty_boxes():
    import numpy as np

    rng = np.random.default_rng(7)
    # Uniform noise everywhere = "open water": box looks like its
    # surroundings and spreads energy evenly.
    sea = (rng.normal(120, 6, (400, 600))).astype(np.uint8)
    empty = track.box_distinctiveness(sea, 200, 150, 120, 80)
    assert empty < track.DISTINCT_FLOOR, empty
    # Drop a high-contrast subject into the box → clearly distinct.
    scene = sea.copy()
    scene[170:210, 240:300] = 250
    scene[180:200, 255:285] = 20
    subject = track.box_distinctiveness(scene, 200, 150, 120, 80)
    assert subject > empty * 1.5, (empty, subject)
    assert subject > track.DISTINCT_FLOOR, subject
    # And the warning text fires only on the empty one.
    assert track.distinct_warning(empty) and "WARNING" in track.distinct_warning(empty)
    assert track.distinct_warning(subject) is None


def test_union_window_covers_dropouts():
    per_frame = [[(0, 0, 10, 10)], [], [], [(5, 5, 10, 10)], []]
    out = track.union_window(per_frame, radius=2)
    assert out[1] and out[2] and out[4]             # gaps covered
    assert len(out[2]) == 2                         # sees both neighbors


# ---------------------------------------------------------------------------
# Execution against a real moving subject
# ---------------------------------------------------------------------------

pytestmark_exec = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")


def _ff(*args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=300)


@pytest.fixture(scope="session")
def moving_box(tmp_path_factory):
    """A distinctive multicolor blob gliding left→right at 80 px/s over a
    STATIC textured background (SMPTE bars) — known ground truth in a fair
    tracking scene. Flat/noisy synthetic frames are pathological for
    appearance trackers (MIL slid freely inside a flat square); real
    footage and this fixture both give it gradients to hold."""
    from PIL import Image, ImageDraw

    root = tmp_path_factory.mktemp("track")
    patch = root / "subject.png"
    img = Image.new("RGB", (60, 60), (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 56, 56], fill=(200, 40, 40))
    d.rectangle([22, 10, 38, 30], fill=(30, 60, 200))
    d.ellipse([30, 34, 52, 54], fill=(20, 160, 60))
    img.save(patch)
    path = root / "moving.mp4"
    _ff("-f", "lavfi", "-i", "smptehdbars=size=640x360:rate=25:duration=4",
        "-i", str(patch),
        "-filter_complex",
        "[0:v][1:v]overlay=x='40+80*t':y=140,format=yuv420p[v]",
        "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", str(path))
    return path


def _frame_at(path, t):
    import cv2

    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    cap.release()
    assert ok
    return frame


@pytestmark_exec
def test_track_span_follows_the_ground_truth(moving_box):
    result = track.track_span(str(moving_box), (40, 140, 60, 60), 0.0, None)
    pts = result["points"]
    assert result["lost_at"] is None and len(pts) > 80
    # Ground truth: center x = 70 + 80t. Allow generous tracker slack.
    for p in (pts[len(pts) // 2], pts[-1]):
        expected = 70 + 80 * p["t"]
        got = p["x"] + p["w"] / 2
        assert abs(got - expected) < 40, (p["t"], got, expected)


@pytestmark_exec
def test_track_span_full_covers_before_the_box_time(moving_box):
    """Box drawn MID-clip (t=2, subject cx=230): the backward pass must
    extend coverage to ~0 — the whole-clip guarantee behind follow crops
    (the first demo parked the crop for the untracked lead-in)."""
    result = track.track_span_full(str(moving_box), (200, 140, 60, 60), 2.0)
    assert result["covered_from"] < 0.15, result["covered_from"]
    pts = result["points"]
    first = pts[0]
    assert abs((first["x"] + first["w"] / 2) - (70 + 80 * first["t"])) < 40
    ts = [p["t"] for p in pts]
    assert all(b > a for a, b in zip(ts, ts[1:]))   # clean back+fwd merge


@pytestmark_exec
def test_track_object_handler_emits_pastable_keyframes(moving_box, monkeypatch):
    monkeypatch.setattr(track, "_resolve_path", lambda p: p)
    text = _run(track.handle_track_object({
        "path": str(moving_box), "box": [40, 140, 60, 60], "end": 3.0}))
    assert "transform.keyframes" in text
    kfs = json.loads(re.search(r"^\[\{.*\}\]$", text, re.M).group(0))
    assert kfs[0]["t"] == 0.0
    # Subject starts left of center (cx 70 vs 320) and moves right.
    assert kfs[0]["pos"][0] < -200
    assert kfs[-1]["pos"][0] > kfs[0]["pos"][0] + 120


@pytestmark_exec
def test_blur_region_blurs_the_moving_target(moving_box, monkeypatch, tmp_path):
    import cv2
    import numpy as np

    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(tmp_path / "blurred.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(moving_box),
        "operations": [{"type": "blur_region", "box": [40, 140, 60, 60]}],
        "output_path": out,
    }))
    assert "ok: blur_region" in text, text
    t = 2.0
    a = _frame_at(moving_box, t)
    b = _frame_at(out, t)
    cx = int(70 + 80 * t)
    inside_a = a[140:200, cx - 30:cx + 30].astype(int)
    inside_b = b[140:200, cx - 30:cx + 30].astype(int)
    outside_a = a[240:340, 40:600].astype(int)
    outside_b = b[240:340, 40:600].astype(int)
    inside = float(np.abs(inside_a - inside_b).mean())
    outside = float(np.abs(outside_a - outside_b).mean())
    # The subject's crisp edges are gone inside the tracked box; the rest
    # differs only by encode noise.
    assert inside > outside * 3, (inside, outside)


@pytestmark_exec
def test_smart_reframe_follow_keeps_subject_in_crop(moving_box, monkeypatch, tmp_path):
    import numpy as np

    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(tmp_path / "follow.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(moving_box),
        "operations": [{"type": "smart_reframe", "aspect": "9:16",
                        "track_box": [40, 140, 60, 60]}],
        "output_path": out,
    }))
    assert "subject-FOLLOW" in text, text
    for t in (0.5, 1.5, 3.0):
        frame = _frame_at(out, t)   # BGR
        h, w = frame.shape[:2]
        assert (w, h) == (202, 360)
        red = np.argwhere((frame[:, :, 2] > 150) & (frame[:, :, 1] < 90)
                          & (frame[:, :, 0] < 90))
        assert len(red) > 50, f"subject missing from crop at t={t}"
        cx = float(red[:, 1].mean())
        # Damped follow: the subject stays well inside, near center.
        assert abs(cx - w / 2) < 65, (t, cx)


@pytestmark_exec
def test_blur_faces_uses_detections_with_smoothing(moving_box, monkeypatch, tmp_path):
    import numpy as np

    # Fake detector returns the known subject box — exercises the full
    # two-pass pipeline (detect-all → union smoothing → blur render).
    def fake_detect(frame, detector=None):
        return [(300.0, 140.0, 60.0, 60.0)]

    monkeypatch.setattr(reframe, "detect_face_boxes_bgr", fake_detect)
    monkeypatch.setattr(reframe, "_detector", lambda w, h: None)
    monkeypatch.setattr(quickops, "_resolve_path", lambda p: p)
    out = str(tmp_path / "faces.mp4")
    text = _run(quickops.handle_edit_video({
        "path": str(moving_box),
        "operations": [{"type": "blur_faces", "pixelate": True}],
        "output_path": out,
    }))
    assert "ok: blur_faces" in text, text
    a = _frame_at(moving_box, 2.88)   # subject passes x=300 at ~2.88s
    b = _frame_at(out, 2.88)
    inside = float(np.abs(a[140:200, 300:360].astype(int)
                          - b[140:200, 300:360].astype(int)).mean())
    assert inside > 3, inside
