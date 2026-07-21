"""Stabilization (vidstab two-pass): presets, filter builders, and the
detect-pass runner with a sidecar transform cache.

vidstab needs two passes over the SAME frame sequence: ``vidstabdetect``
writes a per-frame transform file (.trf), ``vidstabtransform`` consumes it.
The detect pass is pure decode+analysis, so its result is cached as a
sidecar next to the source (like the analysis sidecars) keyed by the file's
identity, the trimmed span, and the shakiness setting — preview, final, and
frame renders all reuse one detect run.

The filter builders are pure string functions (importable by the compiler);
only ``ensure_trf`` touches ffmpeg/the filesystem.
"""

import hashlib
import os
import shutil
from pathlib import Path

from fftools import run_ffmpeg
from shared import logger

# Strength presets: shakiness drives detection sensitivity, smoothing the
# camera-path low-pass radius (frames). optzoom=1 (static optimal zoom)
# handles border compensation — stronger smoothing costs a bigger zoom-in.
PRESETS = {
    "low": {"shakiness": 4, "smoothing": 8, "zoom": 0.0},
    "medium": {"shakiness": 6, "smoothing": 15, "zoom": 0.0},
    "high": {"shakiness": 9, "smoothing": 25, "zoom": 0.0},
}


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def spec_params(spec) -> dict:
    """Normalize a ``stabilize`` value (true | options object) to
    {shakiness, smoothing, zoom}. Unknown keys are ignored (the composition
    validator warns on them); a bad strength raises ValueError."""
    if not isinstance(spec, dict):
        spec = {}
    strength = spec.get("strength", "medium")
    if strength not in PRESETS:
        raise ValueError(
            f"stabilize strength must be one of {sorted(PRESETS)} (got {strength!r})")
    params = dict(PRESETS[strength])
    if spec.get("smoothing") is not None:
        params["smoothing"] = int(spec["smoothing"])
    if spec.get("zoom") is not None:
        params["zoom"] = float(spec["zoom"])
    return params


def transform_filters(trf_path: str, params: dict) -> list[str]:
    """Pass-2 filter chain. ``trf_path`` must be a safe path WE named (tmp
    staging) — user-controlled characters must never reach the filtergraph.
    Ends with the standard vidstab unsharp: stabilization resampling softens
    frames slightly."""
    opts = [f"input={trf_path}",
            f"smoothing={int(params.get('smoothing', 15))}",
            "optzoom=1", "interpol=bicubic"]
    zoom = float(params.get("zoom", 0) or 0)
    if zoom:
        opts.append(f"zoom={_f(zoom)}")
    return ["vidstabtransform=" + ":".join(opts), "unsharp=5:5:0.8:3:3:0.4"]


def sidecar_path(src: str, span: tuple[float, float] | None, shakiness: int) -> Path:
    """Cache location next to the source. Keyed by size+mtime (a replaced
    file must not reuse stale transforms) + span + shakiness."""
    st = os.stat(src)
    span_s = "full" if span is None else f"{span[0]:.3f}-{span[1]:.3f}"
    raw = f"{st.st_size}:{st.st_mtime_ns}:{span_s}:{shakiness}"
    key = hashlib.md5(raw.encode()).hexdigest()[:10]
    p = Path(src)
    return p.parent / (p.stem + f".stab-{key}.trf")


async def ensure_trf(src: str, span: tuple[float, float] | None,
                     shakiness: int, staged: str) -> tuple[bool, str | None]:
    """Stage the transform file for (src, span, shakiness) at ``staged``.

    Cache-first: on a sidecar hit the detect pass is skipped entirely.
    Otherwise runs vidstabdetect over exactly the frames pass 2 will see
    (same trim + setpts prefix as the clip chain — the .trf indexes frames
    by order, so the sequences must match 1:1) and writes the sidecar
    best-effort. Returns ``(cache_hit, sidecar_path_or_None)``.
    """
    cache = sidecar_path(src, span, shakiness)
    if cache.exists():
        shutil.copyfile(cache, staged)
        return True, str(cache)

    pre = ""
    if span is not None:
        pre = f"trim=start={_f(span[0])}:end={_f(span[1])},"
    vf = (f"{pre}setpts=PTS-STARTPTS,"
          f"vidstabdetect=shakiness={int(shakiness)}:result={staged}")
    await run_ffmpeg(["-i", src, "-vf", vf, "-f", "null", "-"],
                     timeout=1800, heavy=True)
    try:
        shutil.copyfile(staged, cache)
        return False, str(cache)
    except OSError as exc:
        logger.warning("stab cache write failed (non-fatal): %s", exc)
        return False, None
