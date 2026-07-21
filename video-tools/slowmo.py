"""Real slow motion: optical-flow / blend frame synthesis for speed < 1.

Modes (clip field ``interpolate``, only meaningful when ``speed`` < 1):
  duplicate — current behavior: the fps filter repeats frames (judder).
  blend     — minterpolate mi_mode=blend inline in the graph (fast,
              motion-smear look).
  flow      — minterpolate mci (motion-compensated). Minutes-per-second
              slow, so the renderer pre-renders a MEZZANINE: the trimmed
              span with speed + interpolation (and stabilization, when the
              clip has both) baked in at timeline fps, near-lossless
              (crf 10), cached under a bounded LRU dir. The graph then
              consumes the mezzanine like ordinary media.

Native-first: when source_fps × speed ≥ timeline fps the retimed native
frames already fill every output frame (shoot 60/120 for
planned slow-mo) — no interpolation runs at all, whatever the mode.

Baking the stretch into the mezzanine keeps minterpolate's target at the
timeline fps — interpolating in the source domain instead would mean
synthesizing (and encoding) a fps/speed monster (0.1× on a 30fps timeline
= 300 fps).
"""

import hashlib
import os
import tempfile
from pathlib import Path

from fftools import run_ffmpeg
from shared import logger

INTERP_MODES = ("flow", "blend", "duplicate")

FLOW_CHAIN = "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir"
BLEND_CHAIN = "minterpolate=fps={fps}:mi_mode=blend"

# Mezzanines are big (crf-10 slowed spans) — a bounded container-local cache,
# NOT a user-visible sidecar like .trf/.analysis.json (pushing 100 MB
# intermediates through satellite file sync would hurt more than a re-render).
CACHE_DIR = Path(os.environ.get("VIDEO_TOOLS_CACHE_DIR", "")
                 or Path(tempfile.gettempdir()) / "vt-mezzanine")
CACHE_MAX_BYTES = int(
    float(os.environ.get("VIDEO_TOOLS_MEZZANINE_CACHE_GB", "4")) * 1e9)


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def native_sufficient(src_fps: float, speed: float, timeline_fps: float) -> bool:
    """True when retimed native frames already fill the timeline rate."""
    return bool(src_fps) and src_fps * speed >= timeline_fps - 0.01


def cache_key(src: str, span: tuple[float, float], speed: float,
              timeline_fps: float, stab: dict | None) -> str:
    st = os.stat(src)
    stab_s = "none"
    if stab:
        stab_s = (f"{stab.get('shakiness')}-{stab.get('smoothing')}"
                  f"-{stab.get('zoom')}")
    raw = (f"{st.st_size}:{st.st_mtime_ns}:{span[0]:.3f}-{span[1]:.3f}"
           f":{speed:.4f}:{timeline_fps:.3f}:{stab_s}")
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _prune_cache() -> None:
    """Drop least-recently-used mezzanines over the size budget. A pruned
    file that an in-flight render already opened stays readable (POSIX
    unlink keeps open fds valid)."""
    try:
        files = sorted(CACHE_DIR.glob("mezz-*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        # > 1: never evict the newest entry — it is the one just written.
        while len(files) > 1 and total > CACHE_MAX_BYTES:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink()
            logger.info("mezzanine cache evicted %s", victim.name)
    except OSError:
        pass


async def ensure_mezzanine(src: str, span: tuple[float, float], speed: float,
                           timeline_fps: float,
                           stab_filters: list[str] | None = None,
                           stab_params: dict | None = None) -> tuple[str, bool]:
    """Return ``(mezzanine_path, cache_hit)`` for a flow slow-mo span.

    The mezzanine is consumed via ``-i`` (an exec arg, no filtergraph
    escaping concerns). Concurrent renders may race on a miss — both encode
    to unique tmp names and the atomic rename makes the last one win
    (double work at worst, never corruption).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = cache_key(src, span, speed, timeline_fps, stab_params)
    out = CACHE_DIR / f"mezz-{key}.mp4"
    if out.exists():
        os.utime(out)  # refresh LRU position
        return str(out), True

    filters = [f"trim=start={_f(span[0])}:end={_f(span[1])}",
               f"setpts=(PTS-STARTPTS)/{_f(speed)}"]
    filters += stab_filters or []
    filters.append(FLOW_CHAIN.format(fps=_f(timeline_fps)))
    tmp = out.with_suffix(f".tmp{os.getpid()}.mp4")
    await run_ffmpeg(
        ["-i", src, "-vf", ",".join(filters),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-an",
         str(tmp)],
        timeout=3600, heavy=True)
    os.replace(tmp, out)
    _prune_cache()
    return str(out), False
