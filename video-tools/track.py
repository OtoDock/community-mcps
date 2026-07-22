"""Object tracking: the `track_object` tool + tracked-crop/blur engines.

Tracker: cv2.TrackerVit with the vendored VitTracker ONNX (Apache-2.0,
opencv_zoo — see models/README.md); cv2.TrackerMIL is the model-free
fallback. Trackers need CONSECUTIVE frames, so a span is always walked
frame by frame (no striding); a hard cut inside the span makes any
tracker drift — spans should stay within one shot.

Three consumers:
  track_object tool — smoothed subject path → transform.keyframes JSON the
      agent pastes onto an overlay (tracked callouts/labels).
  smart_reframe follow mode (quickops) — the subject box drives an
      animated crop x/y (damped + velocity-clamped: follows without
      wobble). This is the faceless-subject answer (boats/cars/products)
      that static per-shot crops can't give.
  blur_region / blur_faces (quickops) — tracked (or detected) boxes are
      blurred per frame by a cv2 loop piped straight into ffmpeg (rawvideo
      stdin), original audio muxed back in.
"""

import asyncio
import json
import math
import os
import statistics
from pathlib import Path

from fftools import FFMPEG
from reframe import MODELS_DIR
from shared import _resolve_path, _to_agents_relative, logger

_VIT = "object_tracking_vittrack_2023sep.onnx"

MAX_SPAN_S = 300.0          # tracking walks every frame — bound the work
_LOST_SCORE = 0.30          # VitTracker score below this = target lost


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def make_tracker():
    """(tracker, engine_name). MIL by default: cv2 5.0.0's new DNN graph
    engine MANGLES the vendored VitTracker ONNX (zero boxes, score ~0.11
    on real and synthetic footage alike; it warns 'Targets are not
    supported by the new graph engine', and no engine opt-out env works —
    measured 2026-07-22). The model stays vendored; set
    VIDEO_TOOLS_TRACKER=vit to re-enable once opencv fixes it. MIL's
    trade-offs: no scale adaptation (the box keeps its initial size) and
    no confidence score — fine for follow crops, callouts, and blur
    patches on smooth-moving subjects."""
    import cv2

    model = Path(MODELS_DIR) / _VIT
    if os.environ.get("VIDEO_TOOLS_TRACKER") == "vit" and model.exists():
        params = cv2.TrackerVit_Params()
        params.net = str(model)
        return cv2.TrackerVit_create(params), "vit"
    return cv2.TrackerMIL_create(), "mil"


def box_distinctiveness(gray, x: int, y: int, w: int, h: int) -> float:
    """How subject-like the box is: (Sobel energy density vs the MEDIAN of
    its 8 same-size neighbors) × (peak-subwindow energy concentration
    inside the box). ≈1.5 or less means the box looks like its
    surroundings AND spreads its texture evenly — open water, sky,
    asphalt — where ANY tracker drifts silently while reporting success.
    Measured live on the speedboat clip: a mis-placed empty-water box
    reads 1.5, the actual boat 2.8. Advisory, not a classifier — its job
    is catching grossly wrong boxes."""
    import cv2
    import numpy as np

    H, W = gray.shape[:2]

    def energy_map(px: int, py: int):
        px = max(0, min(W - w, px))
        py = max(0, min(H - h, py))
        roi = gray[py:py + h, px:px + w].astype(np.float32)
        gx = cv2.Sobel(roi, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1)
        return cv2.magnitude(gx, gy)

    box_mag = energy_map(x, y)
    e = float(box_mag.mean())
    neighbors = [float(energy_map(x + dx * w, y + dy * h).mean())
                 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                 if (dx, dy) != (0, 0)]
    ratio = e / max(float(np.median(neighbors)), 1e-6)

    mag = cv2.GaussianBlur(box_mag, (0, 0), 3)
    sw = max(8, int(w * 0.4))
    sh = max(8, int(h * 0.4))
    conc = 1.0
    if sw < w or sh < h:
        ii = cv2.integral(mag)
        best = 0.0
        for yy in range(0, h - sh + 1, max(2, h // 16)):
            for xx in range(0, w - sw + 1, max(2, w // 16)):
                s = float(ii[yy + sh, xx + sw] - ii[yy, xx + sw]
                          - ii[yy + sh, xx] + ii[yy, xx])
                best = max(best, s)
        total = float(mag.sum()) or 1e-6
        conc = (best / (sw * sh)) / max(total / (w * h), 1e-6)
    return ratio * conc


DISTINCT_FLOOR = 1.9   # below this the box probably isn't a subject


def distinct_warning(d: float) -> str | None:
    if d >= DISTINCT_FLOOR:
        return None
    return (f"WARNING: the box reads only {d:g}x as distinct as its "
            "surroundings — it may not contain a trackable subject (a box "
            "on open water/sky 'tracks' fine while drifting). Double-check "
            "the coordinates are SOURCE pixels at the right time "
            "(sample_frames + probe_media).")


def track_span(path: str, box: tuple[float, float, float, float],
               start: float = 0.0, end: float | None = None) -> dict:
    """Walk [start, end] frame by frame following `box` (source pixels,
    x/y/w/h from the top-left at time `start`).

    Returns {"fps", "width", "height", "engine", "points":
    [{"t", "x", "y", "w", "h", "score"}], "lost_at": float|None} with t in
    ABSOLUTE source seconds. Stops early when the tracker loses the
    target (lost_at set) — the points up to that moment stay valid.
    """
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"cannot open video: {path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        x, y, w, h = (float(v) for v in box)
        if w < 8 or h < 8:
            raise ValueError("box must be at least 8x8 source pixels")
        if x < 0 or y < 0 or x + w > W + 1 or y + h > H + 1:
            raise ValueError(
                f"box {box} is outside the {W}x{H} source frame")
        if start > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        ok, frame = cap.read()
        if not ok:
            raise ValueError(f"no frame at start={start}s")

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        distinct = box_distinctiveness(gray, int(x), int(y),
                                       int(max(8, w)), int(max(8, h)))
        tracker, engine = make_tracker()
        tracker.init(frame, (int(x), int(y), int(max(1, w)), int(max(1, h))))
        stop = min(t + MAX_SPAN_S, end if end is not None else float("inf"))
        points = [{"t": round(t, 4), "x": x, "y": y, "w": w, "h": h,
                   "score": 1.0}]
        lost_at = None
        while True:
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if t > stop + 1e-6:
                break
            ok, frame = cap.read()
            if not ok:
                break
            ok2, (bx, by, bw, bh) = tracker.update(frame)
            score = (float(tracker.getTrackingScore())
                     if engine == "vit" else (1.0 if ok2 else 0.0))
            if not ok2 or score < _LOST_SCORE:
                lost_at = round(t, 3)
                break
            points.append({"t": round(t, 4), "x": float(bx), "y": float(by),
                           "w": float(bw), "h": float(bh),
                           "score": round(score, 3)})
        return {"fps": fps, "width": W, "height": H, "engine": engine,
                "points": points, "lost_at": lost_at,
                "distinctiveness": round(distinct, 2)}
    finally:
        cap.release()


def _median_filter(vals: list[float], win: int = 5) -> list[float]:
    half = win // 2
    return [statistics.median(vals[max(0, i - half):i + half + 1])
            for i in range(len(vals))]


def smooth_path(points: list[dict], strength: float = 0.5) -> list[dict]:
    """Median-5 (kills single-frame tracker jumps) then EMA damping.

    strength 0..1: 0 ≈ raw with outliers removed, 1 ≈ heavy cinematic
    damping (the follow-crop setting). Returns [{"t","cx","cy","w","h"}].
    """
    if not points:
        return []
    comps = {}
    for key, get in (("cx", lambda p: p["x"] + p["w"] / 2),
                     ("cy", lambda p: p["y"] + p["h"] / 2),
                     ("w", lambda p: p["w"]), ("h", lambda p: p["h"])):
        vals = _median_filter([get(p) for p in points])
        alpha = max(0.06, 0.5 - 0.44 * float(strength))
        ema, out = vals[0], []
        for v in vals:
            ema += alpha * (v - ema)
            out.append(ema)
        comps[key] = out
    return [{"t": p["t"], "cx": comps["cx"][i], "cy": comps["cy"][i],
             "w": comps["w"][i], "h": comps["h"][i]}
            for i, p in enumerate(points)]


def _sample(smoothed: list[dict], interval: float) -> list[dict]:
    out, next_t = [], smoothed[0]["t"]
    for p in smoothed:
        if p["t"] + 1e-9 >= next_t:
            out.append(p)
            next_t = p["t"] + interval
    if out[-1] is not smoothed[-1]:
        out.append(smoothed[-1])
    return out


def to_keyframes(smoothed: list[dict], src_w: int, src_h: int,
                 interval: float = 0.25) -> list[dict]:
    """→ transform.keyframes entries [{"t", "pos": [dx, dy]}].

    pos = subject-center offset from the FRAME center in SOURCE pixels
    (exactly overlay `pos` semantics on a same-size canvas — scale by
    canvas_w/src_w otherwise); t is relative to the span start, so the
    list drops onto an overlay whose `start` is the span start.
    """
    t0 = smoothed[0]["t"]
    return [{"t": round(p["t"] - t0, 3),
             "pos": [round(p["cx"] - src_w / 2), round(p["cy"] - src_h / 2)]}
            for p in _sample(smoothed, interval)]


_BACK_CHUNK_S = 6.0      # buffered lead-in chunk (bounds memory)
_BACK_LONG_SIDE = 720    # tracking resolution for the buffered pass


def _salient_subbox(gray, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """The ~55%-size sub-window of (x,y,w,h) with the highest gradient
    energy — the subject's structure, not the slack around it."""
    import cv2
    import numpy as np

    roi = gray[y:y + h, x:x + w].astype(np.float32)
    gx = cv2.Sobel(roi, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1)
    mag = cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 3)
    sw = max(8, int(w * 0.55))
    sh = max(8, int(h * 0.55))
    if sw >= w and sh >= h:
        return x, y, w, h
    ii = cv2.integral(mag)
    best, bx, by = -1.0, 0, 0
    step = max(2, min(w, h) // 16)
    for yy in range(0, h - sh + 1, step):
        for xx in range(0, w - sw + 1, step):
            s = float(ii[yy + sh, xx + sw] - ii[yy, xx + sw]
                      - ii[yy + sh, xx] + ii[yy, xx])
            if s > best:
                best, bx, by = s, xx, yy
    return x + bx, y + by, sw, sh


def _track_backward(path: str, box, box_time: float, start: float,
                    fps: float) -> list[dict]:
    """Track BACKWARD from box_time toward `start`.

    h264 can't decode in reverse, and per-frame POS_MSEC seeks are a trap
    on VFR sources (phone/drone footage): the seek quantizes to
    keyframes, the tracker gets fed near-duplicate frames, then a jump —
    it slid off the real speedboat onto static water while reporting
    success. So: decode FORWARD once per chunk, buffer the frames
    DOWNSCALED (≤720 long side — bounds memory at ~130 MB/chunk and
    speeds MIL up ~4×), and run the tracker across the REVERSED buffer
    (appearance trackers are direction-agnostic). Boxes scale back to
    source pixels on output. Returns points in ascending-t order."""
    import cv2

    cap = cv2.VideoCapture(path)
    try:
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        scale = min(1.0, _BACK_LONG_SIDE / max(W, H, 1))
        sw = max(2, int(W * scale) // 2 * 2)
        sh = max(2, int(H * scale) // 2 * 2)
        scale_x, scale_y = sw / W, sh / H

        cap.set(cv2.CAP_PROP_POS_MSEC, box_time * 1000)
        ok, frame = cap.read()
        if not ok:
            return []
        x, y, w, h = (float(v) for v in box)
        # Fixed grayscale template from the box_time frame. Backward MIL
        # slid onto the boat's own WAKE (a white streak it retraces into,
        # matching the appearance model) and rode it to the frame edge
        # while reporting success. NCC against the subject's fixed
        # template peaks on its STRUCTURE, scores low on the wake, and a
        # threshold stops the pass HONESTLY (covered_from says how far it
        # got) — the template stays fixed because updating it is how
        # drift creeps in, and a lead-in is short.
        gray0 = cv2.cvtColor(cv2.resize(frame, (sw, sh)),
                             cv2.COLOR_BGR2GRAY)
        bx0, by0 = int(x * scale_x), int(y * scale_y)
        bw0 = max(8, int(w * scale_x))
        bh0 = max(8, int(h * scale_y))
        # Tighten to the most STRUCTURED sub-patch (Sobel energy): user
        # boxes carry slack water around the subject, and water texture
        # decorrelates NCC within a few frames (measured: score collapsed
        # in 7 frames on the full box, holds on the boat itself).
        tx, ty, tw, th = _salient_subbox(gray0, bx0, by0, bw0, bh0)
        template = gray0[ty:ty + th, tx:tx + tw]
        if template.shape[0] < 8 or template.shape[1] < 8:
            return []
        # Output boxes keep the ORIGINAL size, anchored by the subpatch's
        # constant offset inside the user's box.
        dx0, dy0 = tx - bx0, ty - by0
        px, py = tx, ty

        newest_first: list[dict] = []
        chunk_end = box_time
        lost = False
        while chunk_end > start + 1e-3 and not lost:
            chunk_start = max(start, chunk_end - _BACK_CHUNK_S)
            cap.set(cv2.CAP_PROP_POS_MSEC, chunk_start * 1000)
            buf: list[tuple[float, object]] = []
            while True:
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                ok, frame = cap.read()
                if not ok or t >= chunk_end - 1e-6:
                    break
                buf.append((t, cv2.resize(frame, (sw, sh))))
            for t, small in reversed(buf):
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                x0 = max(0, px - tw)
                y0 = max(0, py - th)
                x1 = min(sw, px + 2 * tw)
                y1 = min(sh, py + 2 * th)
                win = gray[y0:y1, x0:x1]
                if win.shape[0] <= th or win.shape[1] <= tw:
                    lost = True
                    break
                res = cv2.matchTemplate(win, template, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                if score < 0.45:
                    lost = True
                    break
                px, py = x0 + int(loc[0]), y0 + int(loc[1])
                newest_first.append(
                    {"t": round(t, 4), "x": (px - dx0) / scale_x,
                     "y": (py - dy0) / scale_y, "w": bw0 / scale_x,
                     "h": bh0 / scale_y, "score": round(float(score), 3)})
            chunk_end = chunk_start
        newest_first.reverse()
        return newest_first
    finally:
        cap.release()


def track_span_full(path: str, box, box_time: float = 0.0,
                    end: float | None = None) -> dict:
    """Whole-clip coverage: forward from box_time (track_span) PLUS a
    backward pass box_time → 0 when the box was drawn mid-clip. The box
    is simply 'where you identified the subject' — the returned path
    spans as much of [0, end] as the tracker could hold."""
    fwd = track_span(path, box, box_time, end)
    if box_time > 0.05:
        back = _track_backward(path, box, box_time, 0.0, fwd["fps"])
        merged = back + [p for p in fwd["points"]
                         if not back or p["t"] > back[-1]["t"] + 1e-6]
        # cv2's POS_MSEC can stamp the first frames identically — keep
        # the sequence strictly increasing.
        dedup: list[dict] = []
        for p in merged:
            if not dedup or p["t"] > dedup[-1]["t"] + 1e-6:
                dedup.append(p)
        fwd = {**fwd, "points": dedup}
    fwd["covered_from"] = fwd["points"][0]["t"] if fwd["points"] else box_time
    return fwd


def follow_crop_keypoints(smoothed: list[dict], crop_w: int, crop_h: int,
                          src_w: int, src_h: int, smoothness: float = 0.7,
                          interval: float = 0.4) -> tuple[list, list]:
    """Follow-crop path → ([(t, x)], [(t, y)]) top-left keypoints from a
    DEADZONE controller: the window holds perfectly still while the
    subject sits near its center, accelerates proportionally once the
    subject drifts past the deadzone, and a hard SAFETY pull guarantees
    the subject center never leaves the crop's central 80% — smooth when
    possible, fast when necessary. `smoothness` 0-1 trades deadzone width
    + glide speed against responsiveness (a plain velocity clamp lagged an
    accelerating subject right out of frame — caught in review)."""
    s = min(1.0, max(0.0, float(smoothness)))
    samples = _sample(smoothed, interval)

    def plan(key: str, crop: float, src: float) -> list[tuple[float, float]]:
        dead = crop * (0.06 + 0.14 * s)          # hold-still half-width
        vmax = crop * (1.6 - 1.15 * s)           # glide speed cap, px/s
        margin = crop * 0.40                     # safety band half-width
        pos = max(0.0, min(samples[0][key] - crop / 2, src - crop))
        out = [(samples[0]["t"], round(pos, 1))]
        for prev, p in zip(samples, samples[1:]):
            dt = max(1e-6, p["t"] - prev["t"])
            c = p[key]
            err = c - (pos + crop / 2)
            if abs(err) > dead:
                want = err - math.copysign(dead, err)
                pos += max(-vmax * dt, min(vmax * dt, want))
            off = c - (pos + crop / 2)
            if abs(off) > margin:                # safety overrides the cap
                pos += off - math.copysign(margin, off)
            pos = max(0.0, min(pos, src - crop))
            out.append((p["t"], round(pos, 1)))
        return out

    return plan("cx", crop_w, src_w), plan("cy", crop_h, src_h)


def union_window(per_frame: list[list], radius: int = 3) -> list[list]:
    """Privacy smoothing for per-frame detections: each frame blurs every
    box seen within ±radius frames, so single-frame detector dropouts
    stay covered (over-blur beats a face flashing through)."""
    out = []
    for i in range(len(per_frame)):
        boxes = []
        for j in range(max(0, i - radius), min(len(per_frame), i + radius + 1)):
            boxes.extend(per_frame[j])
        out.append(boxes)
    return out


# ---------------------------------------------------------------------------
# Blurred-region rendering (blur_faces / blur_region engines)
# ---------------------------------------------------------------------------


def _blur_frame(frame, boxes, pixelate: bool = False):
    """Blur each (x, y, w, h) ROI in place. Gaussian by default; pixelate
    gives the deliberate mosaic look."""
    import cv2

    H, W = frame.shape[:2]
    for bx, by, bw, bh in boxes:
        # 12% dilation: privacy over-covers, never under-covers.
        dx, dy = bw * 0.12, bh * 0.12
        x0 = max(0, int(bx - dx)); y0 = max(0, int(by - dy))
        x1 = min(W, int(bx + bw + dx)); y1 = min(H, int(by + bh + dy))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        roi = frame[y0:y1, x0:x1]
        if pixelate:
            fw = max(2, (x1 - x0) // 14)
            fh = max(2, (y1 - y0) // 14)
            small = cv2.resize(roi, (fw, fh), interpolation=cv2.INTER_LINEAR)
            frame[y0:y1, x0:x1] = cv2.resize(
                small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
        else:
            sigma = max(6.0, (y1 - y0) / 5.0)
            frame[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (0, 0), sigma)
    return frame


def _render_blurred_blocking(src: str, out: str, boxes_at, has_audio: bool,
                             pixelate: bool) -> int:
    """cv2 decode loop → blur ROIs → rawvideo pipe into ffmpeg (x264
    crf 18), original audio muxed back. Fully blocking (Popen + pipe
    writes) — the async wrapper runs it in a worker thread."""
    import subprocess

    import cv2

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise ValueError(f"cannot open video: {src}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
            "-r", _f(fps), "-i", "-", "-i", src,
            "-map", "0:v"] \
        + (["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"] if has_audio else []) \
        + ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", out]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    n = 0
    try:
        while True:
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            ok, frame = cap.read()
            if not ok:
                break
            boxes = boxes_at(n, t)
            if boxes:
                frame = _blur_frame(frame, boxes, pixelate)
            proc.stdin.write(frame.tobytes())
            n += 1
        proc.stdin.close()
        stderr = proc.stderr.read()
        rc = proc.wait(timeout=600)
    except BrokenPipeError:
        stderr = proc.stderr.read()
        proc.wait(timeout=60)
        raise RuntimeError("ffmpeg blur encode died mid-stream: "
                           + stderr.decode(errors="replace")[-400:])
    finally:
        cap.release()
        if proc.poll() is None:
            proc.kill()
    if rc != 0:
        raise RuntimeError("ffmpeg blur encode failed: "
                           + stderr.decode(errors="replace")[-400:])
    return n


async def render_blurred(src: str, out: str, boxes_at, has_audio: bool,
                         pixelate: bool = False) -> int:
    """Async wrapper: `boxes_at(frame_index, t) -> [(x,y,w,h)]` decides
    what gets blurred on each frame. Returns frames written."""
    return await asyncio.to_thread(
        _render_blurred_blocking, src, out, boxes_at, has_audio, pixelate)


# ---------------------------------------------------------------------------
# track_object handler
# ---------------------------------------------------------------------------


async def handle_track_object(args: dict) -> str:
    path = _resolve_path(args["path"])
    if not Path(path).exists():
        return f"Error: file not found: {args['path']}"
    box = args.get("box")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(isinstance(v, (int, float)) for v in box)):
        return ("Error: box must be [x, y, width, height] in SOURCE pixels "
                "at the start time — read the frame with sample_frames and "
                "the dimensions with probe_media to estimate it")
    start = float(args.get("start", 0.0))
    end = args.get("end")
    end = float(end) if end is not None else None
    smoothing = min(1.0, max(0.0, float(args.get("smoothing", 0.5))))
    interval = min(2.0, max(0.1, float(args.get("keyframe_interval", 0.25))))

    result = await asyncio.to_thread(track_span, path, box, start, end)
    points = result["points"]
    if len(points) < 2:
        return ("Error: tracking failed immediately — the box may not cover "
                "a distinct subject at the start time")
    smoothed = smooth_path(points, smoothing)
    kfs = to_keyframes(smoothed, result["width"], result["height"], interval)

    span = points[-1]["t"] - points[0]["t"]
    sizes = [p["w"] * p["h"] for p in points]
    drift = (sizes[-1] / sizes[0]) if sizes[0] else 1.0
    lines = [
        f"# tracked '{_to_agents_relative(path)}' "
        f"[{points[0]['t']:.2f}s → {points[-1]['t']:.2f}s]",
        f"engine: {'VitTracker (NN)' if result['engine'] == 'vit' else 'MIL'} · "
        f"{len(points)} frames · subject size x{drift:.2f} over the span",
    ]
    warn = distinct_warning(result["distinctiveness"])
    if warn:
        lines.append(warn)
    if result["lost_at"] is not None:
        lines.append(
            f"WARNING: target lost at {result['lost_at']:.2f}s — keyframes "
            "stop there. A cut inside the span, occlusion, or the subject "
            "leaving frame will do this; track shot by shot.")
    lines += [
        "",
        "transform.keyframes (paste onto an overlay whose `start` is "
        f"{points[0]['t']:.2f}s; t is relative to that; pos is the subject "
        "center's offset from the FRAME center in source pixels — if the "
        "project canvas differs from "
        f"{result['width']}x{result['height']}, scale pos by "
        "canvas_width/source_width; add a constant offset to hover a label "
        "above the subject):",
        json.dumps(kfs, separators=(",", ":")),
        "",
        f"Subject box at end: x={points[-1]['x']:.0f} y={points[-1]['y']:.0f} "
        f"w={points[-1]['w']:.0f} h={points[-1]['h']:.0f}. "
        f"Span walked frame-by-frame ({span:.2f}s); smoothing={smoothing:g}.",
        "Also available: edit_video ops blur_region {box, start?, end?} "
        "(tracked blur patch) and smart_reframe {aspect, track_box} "
        "(subject-following vertical crop).",
    ]
    return "\n".join(lines)
