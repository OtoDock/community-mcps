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

import asyncio
import contextlib
import hashlib
import os
import secrets
import tempfile
from pathlib import Path

from fftools import _f, run_ffmpeg
from shared import PROCESS_START, logger, own_folder

INTERP_MODES = ("flow", "blend", "duplicate")

FLOW_CHAIN = "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir"
BLEND_CHAIN = "minterpolate=fps={fps}:mi_mode=blend"

# Mezzanines are big (crf-10 slowed spans) — a bounded container-local cache,
# NOT a user-visible sidecar like .trf/.analysis.json (pushing 100 MB
# intermediates through satellite file sync would hurt more than a re-render).
CACHE_DIR = Path(os.environ.get("VIDEO_TOOLS_CACHE_DIR", "")
                 or Path(tempfile.gettempdir()) / "vt-mezzanine")
own_folder(CACHE_DIR)
CACHE_MAX_BYTES = int(
    float(os.environ.get("VIDEO_TOOLS_MEZZANINE_CACHE_GB", "4")) * 1e9)


def native_sufficient(src_fps: float, speed: float, timeline_fps: float) -> bool:
    """True when retimed native frames already fill the timeline rate."""
    return bool(src_fps) and src_fps * speed >= timeline_fps - 0.01


def cache_key(src: str, span: tuple[float, float], speed: float,
              timeline_fps: float, stab: dict | None) -> str:
    """The mezzanine's key: the source file itself (device, inode, size and
    both its times, read with no link followed) and the session's agent, so
    the shared cache never serves one agent's clip to another, and the span
    and settings. Blocking: a thread runs it."""
    from shared import _agent_var, read_identity

    # The call's later opens of the source are held to the file this reads.
    try:
        st = read_identity(src)
    except FileNotFoundError:
        raise ValueError(f"file not found: {src}") from None
    stab_s = "none"
    if stab:
        stab_s = (f"{stab.get('shakiness')}-{stab.get('smoothing')}"
                  f"-{stab.get('zoom')}")
    raw = (f"{_agent_var.get()}:{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
           f":{span[0]:.3f}-{span[1]:.3f}:{speed:.4f}:{timeline_fps:.3f}:{stab_s}")
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _prune_cache() -> None:
    """Drop the temps of a process that ended during a build (last written
    before this one started, ``PROCESS_START``, the sweep's own rule), then
    the least-recently-used mezzanines over the size budget. A pruned file
    that an in-flight render already opened stays readable (POSIX unlink
    keeps open fds valid)."""
    try:
        for temp in CACHE_DIR.glob(".mezz-*.mp4"):
            with contextlib.suppress(OSError):
                if temp.stat().st_mtime < PROCESS_START:
                    temp.unlink()
                    logger.info("mezzanine cache removed %s, a build that never ended", temp.name)
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
    escaping concerns). Two renders that miss on one key encode to temps of
    their own (a random part in each name), and the atomic rename makes the
    last one win: double work at worst, never one file written by two. A
    temp is removed on any failure. The prune never counts one, and it
    removes the temp of a process that ended during its build.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = await asyncio.to_thread(cache_key, src, span, speed, timeline_fps, stab_params)
    out = CACHE_DIR / f"mezz-{key}.mp4"
    if out.exists():
        os.utime(out)  # refresh LRU position
        return str(out), True

    filters = [f"trim=start={_f(span[0])}:end={_f(span[1])}",
               f"setpts=(PTS-STARTPTS)/{_f(speed)}"]
    filters += stab_filters or []
    filters.append(FLOW_CHAIN.format(fps=_f(timeline_fps)))
    tmp = CACHE_DIR / f".mezz-{key}.{secrets.token_hex(8)}.mp4"
    try:
        await run_ffmpeg(
            ["-i", src, "-vf", ",".join(filters),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-an",
             str(tmp)],
            timeout=3600, heavy=True)
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _prune_cache()
    return str(out), False
