"""smart_reframe: subject-aware aspect conversion (e.g. 16:9 → 9:16).

AutoFlip-style planning: every shot gets a STATIC crop center chosen from
face detections sampled across the shot — stable framing with no wobble —
and the crop window repositions only at shot boundaries (which are cuts
anyway, so the jump is invisible). Shots with no detected subject fall back
to a center crop. Detection is YuNet (vendored MIT-licensed ONNX, see
models/README.md) via OpenCV.
"""

import os
from pathlib import Path

_YUNET = "face_detection_yunet_2023mar.onnx"

# In the image the models ship at /app/models; local dev runs use the
# repo-relative copy next to this module.
MODELS_DIR = os.environ.get("VIDEO_TOOLS_MODELS_DIR", "") or (
    "/app/models" if Path("/app/models").exists()
    else str(Path(__file__).parent / "models"))

_SCORE_THRESHOLD = 0.6
_SAMPLES_PER_SHOT = 8
_MIN_SAMPLE_STEP = 0.4   # seconds between detection samples within a shot


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def _detector(width: int, height: int):
    import cv2

    model = Path(MODELS_DIR) / _YUNET
    if not model.exists():
        raise RuntimeError(
            f"YuNet model not found at {model} — smart_reframe needs the "
            "vendored models/ directory")
    det = cv2.FaceDetectorYN.create(str(model), "", (320, 320), _SCORE_THRESHOLD)
    det.setInputSize((width, height))
    return det


def detect_faces_bgr(frame, detector=None) -> list[tuple[float, float, float]]:
    """BGR frame → [(cx, cy, weight)] for each detected face.

    Weight is face area × confidence, so near/confident subjects dominate
    the crop-center vote. Module-level so tests can substitute a fake.
    """
    h, w = frame.shape[:2]
    det = detector or _detector(w, h)
    _, faces = det.detect(frame)
    out = []
    if faces is not None:
        for row in faces:
            fx, fy, fw, fh = row[0], row[1], row[2], row[3]
            score = float(row[-1])
            out.append((float(fx + fw / 2), float(fy + fh / 2),
                        float(max(1.0, fw * fh) * score)))
    return out


def plan_segments(path: str, shots: list[tuple[float, float]],
                  crop_w: int, crop_h: int, src_w: int, src_h: int) -> list[dict]:
    """Per-shot crop plan → [{"start", "end", "x", "y", "faces"}].

    x/y are the crop window's top-left, clamped in bounds; the weighted mean
    of face centers across the shot's samples sets the window center.
    """
    import cv2

    cap = cv2.VideoCapture(path)
    try:
        detector = _detector(src_w, src_h)
    except RuntimeError:
        cap.release()
        raise
    segments = []
    try:
        for start, end in shots:
            span = max(0.0, end - start)
            n = min(_SAMPLES_PER_SHOT, max(1, int(span / _MIN_SAMPLE_STEP)))
            times = [start + span * (i + 0.5) / n for i in range(n)]
            sum_w = sum_x = sum_y = 0.0
            hits = 0
            for t in times:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                for cx, cy, weight in detect_faces_bgr(frame, detector):
                    sum_x += cx * weight
                    sum_y += cy * weight
                    sum_w += weight
                    hits += 1
            if sum_w > 0:
                cx, cy = sum_x / sum_w, sum_y / sum_w
            else:
                cx, cy = src_w / 2, src_h / 2
            x = max(0.0, min(cx - crop_w / 2, src_w - crop_w))
            y = max(0.0, min(cy - crop_h / 2, src_h - crop_h))
            segments.append({"start": round(start, 3), "end": round(end, 3),
                             "x": round(x, 1), "y": round(y, 1),
                             "faces": hits})
    finally:
        cap.release()
    return segments


def step_expr(segments: list[dict], key: str) -> str:
    """Piecewise-CONSTANT ffmpeg expression over t: the segment value holds
    until the segment's end. Caller must single-quote the option value."""
    if not segments:
        return "0"
    expr = _f(segments[-1][key])
    for seg in reversed(segments[:-1]):
        expr = f"if(lt(t,{_f(seg['end'])}),{_f(seg[key])},{expr})"
    return expr
