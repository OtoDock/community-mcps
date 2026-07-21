"""FFmpeg/ffprobe process layer.

All ffmpeg execution funnels through here: binary discovery, a render
semaphore (renders are CPU/RAM-heavy — never run unbounded), probe helpers,
and filter-argument escaping. Binaries come from PATH inside the container;
FFMPEG_PATH/FFPROBE_PATH env overrides exist for local test runs.
"""

import asyncio
import json
import os
import re
import shutil

from shared import logger

FFMPEG = os.environ.get("FFMPEG_PATH", "") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_PATH", "") or shutil.which("ffprobe") or "ffprobe"

# Renders buffer frames at every xfade/overlay junction; two concurrent heavy
# renders on a 4g container is the safe ceiling. Analysis/probe calls are
# cheap and skip the semaphore.
_render_semaphore = asyncio.Semaphore(
    max(1, int(os.environ.get("VIDEO_TOOLS_MAX_RENDERS", "2")))
)


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Carries a trimmed stderr tail for the agent."""

    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


def _stderr_tail(stderr: bytes, limit: int = 1200) -> str:
    text = stderr.decode(errors="replace").strip()
    # ffmpeg repeats progress lines; keep the tail where errors live.
    return text[-limit:] if len(text) > limit else text


async def run_ffmpeg(
    args: list[str],
    timeout: float = 600.0,
    heavy: bool = True,
    capture_stdout: bool = False,
) -> tuple[bytes, str]:
    """Run ffmpeg with the given args (after the binary). Returns
    ``(stdout, stderr_text)``; raises FFmpegError on non-zero exit or timeout.
    """
    cmd = [FFMPEG, "-hide_banner", "-y", *args]
    logger.info("ffmpeg: %s", " ".join(cmd[:24]) + (" …" if len(cmd) > 24 else ""))

    async def _run() -> tuple[bytes, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if capture_stdout else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise FFmpegError(f"ffmpeg timed out after {int(timeout)}s")
        if proc.returncode != 0:
            tail = _stderr_tail(stderr)
            raise FFmpegError(
                f"ffmpeg failed (exit {proc.returncode}): {tail[-400:]}", tail
            )
        return stdout or b"", _stderr_tail(stderr, 4000)

    if heavy:
        async with _render_semaphore:
            return await _run()
    return await _run()


async def probe(path: str) -> dict:
    """ffprobe → parsed JSON with format + streams."""
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-hide_banner", "-loglevel", "error",
        "-print_format", "json", "-show_format", "-show_streams", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError("ffprobe timed out (30s)")
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {_stderr_tail(stderr, 400)}")
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned unparseable output: {exc}")


def video_stream(info: dict) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1:
            return s
    return None


def audio_stream(info: dict) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def media_duration(info: dict) -> float:
    """Container duration in seconds (0.0 when unknown, e.g. still images)."""
    try:
        return max(0.0, float(info.get("format", {}).get("duration", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def stream_fps(stream: dict) -> float:
    """Parse a stream's average frame rate ('30000/1001' → 29.97)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key, "")
        m = re.fullmatch(r"(\d+)/(\d+)", str(raw))
        if m and int(m.group(2)) != 0:
            fps = int(m.group(1)) / int(m.group(2))
            if fps > 0:
                return fps
    return 0.0


# ---------------------------------------------------------------------------
# Filter-argument escaping
# ---------------------------------------------------------------------------
#
# Levels of escaping in ffmpeg filtergraphs (in order applied):
#   1. filter-option value: ':' separates options, so values containing
#      ':' or ',' or '[' etc. must be quoted with '...' or backslash-escaped.
#   2. the graph itself: ';' and '[' ']' are structural.
# We sidestep most of it by (a) writing graphs to a -filter_complex_script
# file (no shell involved), and (b) copying caption/LUT files to tmp paths WE
# name (no user-controlled characters). esc_filter_value covers the rest.


def esc_filter_value(value: str) -> str:
    """Escape a string for use as a filter option value."""
    out = value.replace("\\", "\\\\")
    for ch in (":", "'", ",", ";", "[", "]", "="):
        out = out.replace(ch, "\\" + ch)
    return out


def ff_color(hex_or_name: str) -> str:
    """Normalize a color to ffmpeg syntax.

    '#RRGGBB' → '0xRRGGBB' ('#' starts a comment inside a
    -filter_complex_script file, so it must never reach the graph).
    Named colors pass through.
    """
    c = (hex_or_name or "").strip()
    if c.startswith("#"):
        return "0x" + c[1:]
    return c or "black"


def atempo_chain(speed: float) -> list[str]:
    """Decompose a speed factor into valid atempo stages (each 0.5–100)."""
    if speed <= 0:
        raise ValueError("speed must be > 0")
    stages: list[float] = []
    remaining = speed
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    while remaining > 100.0:
        stages.append(100.0)
        remaining /= 100.0
    stages.append(remaining)
    return [f"atempo={s:.6g}" for s in stages if abs(s - 1.0) > 1e-9] or []
