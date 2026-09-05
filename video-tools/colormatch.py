"""Shot color matching (match_color): make one shot's color sit with
another's — drone vs camera, AI bridge vs real footage.

Method: sample frames from the target and the reference and map each RGB
channel's quantile curve onto the reference's (a smooth monotone transfer
through ~9 quantile pairs), baked as a .cube LUT applied via lut3d.
Quantile curves recover multiplicative casts (white balance, channel
gain) AND tone differences (gamma, lifted blacks) — a Lab mean/std affine
was measured to overshoot badly on saturated content because a tint is
multiplicative in RGB, not additive in Lab. The sparse quantile set keeps
the curve smooth (no histogram-matching posterization). Assumption:
target and reference frames show SIMILAR content — true at bridge
junctions and for same-scene shot matching, which is the use case.

For AI-bridge joins the renderer requests TWO LUTs (bridge start matched
to clip A's endpoint, bridge end to clip B's) and the compiler dissolves
between the two GRADES of the same footage — an invisible grade ramp; the
cuts themselves stay hard cuts.

Frames are sampled through ffmpeg with the file's colour declaration (the
same head tag the render chain carries), so the LUT is built in the RGB it
is applied in. numpy is imported lazily — parse_ref stays import-light for
the validator.
"""

MATCH_KEYS = ("ref", "ramp_from", "ramp_to", "strength", "target_time")

_QUANTILES = (0.0, 0.02, 0.1, 0.25, 0.5, 0.75, 0.9, 0.98, 1.0)
_STATS_MAX_WIDTH = 480


def parse_ref(ref: str) -> tuple[str, float]:
    """'path@seconds' → (path, seconds). Raises ValueError on bad syntax."""
    if not isinstance(ref, str) or "@" not in ref:
        raise ValueError(
            f"match reference must be 'path@seconds' (got {ref!r})")
    path, _, t = ref.rpartition("@")
    try:
        time = float(t)
    except ValueError:
        raise ValueError(f"match reference time must be a number (got {t!r})")
    if not path or time < 0:
        raise ValueError(f"match reference must be 'path@seconds' (got {ref!r})")
    return path, time


def _frame_size(path: str) -> tuple[int, int]:
    import subprocess

    from fftools import FFPROBE

    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    try:
        w, h = [int(x) for x in out.stdout.strip().split(",")[:2]]
    except ValueError:
        raise ValueError(f"could not read the frame size of '{path}'")
    return w, h


def _grab_frames(path: str, times: list[float], head: str = ""):
    """Sample BGR frames at the given seconds through ffmpeg (downscaled
    for stats), decoded in the RGB the LUT is later applied in: `head` is
    the chain's setparams declaration for the fields the file leaves
    untagged. (cv2's decoder honours tags but reads an untagged HD file
    with the 601 matrix — measured — which is not what lut3d sees after
    the head tag.) Returns at least one frame or raises ValueError."""
    import subprocess

    import numpy as np

    from fftools import FFMPEG

    w, h = _frame_size(path)
    ow = min(_STATS_MAX_WIDTH, w)
    oh = max(2, int(round(h * ow / w / 2)) * 2)
    vf = ",".join(f for f in (head, f"scale={ow}:{oh}:flags=area", "format=bgr24") if f)
    frames = []
    for t in times:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-ss", f"{max(0.0, t):.3f}", "-i", path, "-vf", vf,
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True)
        buf = proc.stdout
        if proc.returncode != 0 or len(buf) != ow * oh * 3:
            continue   # past the end / unreadable — like a failed cv2 read
        frames.append(np.frombuffer(buf, np.uint8).reshape(oh, ow, 3).copy())
    if not frames:
        raise ValueError(f"could not read frames from '{path}' at {times}")
    return frames


def _rgb_quantiles(frames):
    """(len(_QUANTILES), 3) per-channel RGB quantiles in [0,1] over all
    pixels of all frames."""
    import numpy as np

    px = np.concatenate(
        [f.reshape(-1, 3)[:, ::-1].astype(np.float64) for f in frames],
        axis=0) / 255.0
    return np.quantile(px, _QUANTILES, axis=0)


def _build_lut(tgt_q, ref_q, strength: float, size: int):
    """(size³, 3) RGB float grid in [0,1], red axis fastest (.cube order).
    Separable: each channel gets the monotone quantile-pair curve."""
    import numpy as np

    axis = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)

    matched = np.empty_like(grid)
    # Tiny strictly-increasing nudge: np.interp needs increasing xp, and a
    # near-flat frame collapses several quantiles onto one value.
    eps = np.linspace(0.0, 1e-6, len(_QUANTILES))
    for c in range(3):
        matched[:, c] = np.interp(
            grid[:, c], tgt_q[:, c] + eps, np.clip(ref_q[:, c], 0.0, 1.0))
    s = min(max(float(strength), 0.0), 1.0)
    return grid * (1.0 - s) + matched * s


def write_cube(lut, size: int, path: str) -> None:
    lines = [
        "# Generated by OtoDock video-tools match_color",
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    lines.extend(f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in lut)
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines) + "\n")


def generate_match_lut(target_path: str, target_times: list[float],
                       ref_path: str, ref_times: list[float],
                       out_path: str, strength: float = 1.0,
                       size: int = 33, *, target_head: str = "",
                       ref_head: str = "") -> None:
    """Sample both sides, build the target→reference LUT, write .cube.
    `target_head` / `ref_head` are the two files' colour declarations
    (color.head_tags → tag_filter) so the samples are read in the RGB the
    LUT is applied in. Blocking (ffmpeg decode + numpy) — call via
    asyncio.to_thread."""
    tgt = _rgb_quantiles(_grab_frames(target_path, target_times, target_head))
    ref = _rgb_quantiles(_grab_frames(ref_path, ref_times, ref_head))
    write_cube(_build_lut(tgt, ref, strength, size), size, out_path)


def sample_window(t: float, lo: float, hi: float) -> list[float]:
    """Three sample times around t clamped to [lo, hi] — a small window
    beats a single frame (motion blur, flicker)."""
    return [max(lo, t - 0.15), min(max(lo, t), hi), min(hi, t + 0.15)]
