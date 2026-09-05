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


_COLOR_FIELDS = ("color_space", "color_transfer", "color_primaries", "color_range")


def stream_color(stream: dict) -> dict:
    """The KNOWN colour tags of a video stream — ffprobe reports them from
    the container's colr box or the bitstream VUI (omitting unknown ones,
    or printing 'unknown')."""
    out: dict[str, str] = {}
    for field in _COLOR_FIELDS:
        val = stream.get(field)
        if val and str(val).lower() not in ("unknown", "unspecified"):
            out[field] = str(val)
    return out


def timecode(info: dict) -> tuple[str, str] | None:
    """Standard container timecode → (value, where). MP4/MOV carry it as a
    `tmcd` data track (ffprobe mirrors it onto the video stream's tags);
    MXF/others may carry a plain stream or format tag."""
    has_tmcd = any(s.get("codec_tag_string") == "tmcd" for s in info.get("streams", []))
    for s in info.get("streams", []):
        tc = (s.get("tags") or {}).get("timecode")
        if tc:
            return str(tc), ("tmcd track" if has_tmcd else "stream tag")
    tc = (info.get("format", {}).get("tags") or {}).get("timecode")
    if tc:
        return str(tc), "container tag"
    return None


# ISO-BMFF (MP4/MOV) `colr` atom — the container-level colour declaration.
# ffprobe merges colr and the bitstream VUI into one report, so it cannot
# say whether the CONTAINER declares anything: that is what a colour
# pipeline needs to know about camera files (Sony XAVC writes none).

_BMFF_TOP_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide",
                   b"uuid", b"moof", b"mfra", b"meta", b"styp", b"sidx"}
_MOOV_READ_CAP = 64 << 20
# VisualSampleEntry: 8 bytes (reserved[6] + data_reference_index) + 70 bytes
# of fixed fields before the child boxes (avcC/hvcC/colr/pasp/...).
_VISUAL_ENTRY_FIXED = 78
_COLR_NAMES = {
    "primaries": {1: "bt709", 4: "bt470m", 5: "bt470bg", 6: "smpte170m",
                  7: "smpte240m", 8: "film", 9: "bt2020", 10: "smpte428",
                  11: "smpte431", 12: "smpte432", 22: "ebu3213"},
    "transfer": {1: "bt709", 4: "bt470m", 5: "bt470bg", 6: "smpte170m",
                 7: "smpte240m", 8: "linear", 9: "log100", 10: "log316",
                 11: "iec61966-2-4", 12: "bt1361e", 13: "iec61966-2-1",
                 14: "bt2020-10", 15: "bt2020-12", 16: "smpte2084",
                 17: "smpte428", 18: "arib-std-b67"},
    "matrix": {0: "gbr", 1: "bt709", 4: "fcc", 5: "bt470bg", 6: "smpte170m",
               7: "smpte240m", 8: "ycgco", 9: "bt2020nc", 10: "bt2020c",
               11: "smpte2085", 12: "chroma-derived-nc",
               13: "chroma-derived-c", 14: "ictcp"},
}


def _bmff_boxes(buf: bytes, start: int, end: int):
    """(type, payload_start, box_end) for the boxes packed in buf[start:end]."""
    import struct

    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _read_moov(path: str) -> bytes | None:
    """The bytes of the top-level `moov` box, seeking over everything else
    (mdat is never read). None when the file is not ISO-BMFF or has no moov."""
    import struct

    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        total = fh.tell()
        fh.seek(0)
        pos, first = 0, True
        while pos + 8 <= total:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                return None
            size, typ = struct.unpack(">I4s", head[:8])
            hdr = 8
            if size == 1:
                if len(head) < 16:
                    return None
                size = struct.unpack(">Q", head[8:16])[0]
                hdr = 16
            elif size == 0:
                size = total - pos
            if first and typ not in _BMFF_TOP_BOXES:
                return None
            first = False
            if size < hdr:
                return None
            if typ == b"moov":
                if size - hdr > _MOOV_READ_CAP:
                    return None
                fh.seek(pos + hdr)
                return fh.read(size - hdr)
            pos += size
    return None


def colr_box(path: str) -> dict | None:
    """The `colr` atom of the first visual sample entry of an MP4/MOV:
    {"type": "nclx"|"nclc"|..., "primaries", "transfer", "matrix",
    "full_range"} (names as ffprobe prints them, ints when unmapped), or
    None when the container declares no colour (or the file is not BMFF)."""
    import struct

    try:
        moov = _read_moov(path)
    except OSError:
        return None
    if moov is None:
        return None
    end = len(moov)
    for t1, a1, b1 in _bmff_boxes(moov, 0, end):
        if t1 != b"trak":
            continue
        for t2, a2, b2 in _bmff_boxes(moov, a1, b1):
            if t2 != b"mdia":
                continue
            for t3, a3, b3 in _bmff_boxes(moov, a2, b2):
                if t3 != b"minf":
                    continue
                for t4, a4, b4 in _bmff_boxes(moov, a3, b3):
                    if t4 != b"stbl":
                        continue
                    for t5, a5, b5 in _bmff_boxes(moov, a4, b4):
                        if t5 != b"stsd":
                            continue
                        # stsd: version/flags (4) + entry_count (4), then entries.
                        for t6, a6, b6 in _bmff_boxes(moov, a5 + 8, b5):
                            if t6 in (b"mp4a", b"tmcd", b"text", b"tx3g", b"ac-3", b"ec-3"):
                                continue
                            for t7, a7, b7 in _bmff_boxes(moov, a6 + _VISUAL_ENTRY_FIXED, b6):
                                if t7 != b"colr":
                                    continue
                                ctype = moov[a7:a7 + 4].decode("latin-1")
                                out: dict = {"type": ctype}
                                if ctype in ("nclx", "nclc") and b7 - a7 >= 10:
                                    pri, trc, mat = struct.unpack(">HHH", moov[a7 + 4:a7 + 10])
                                    out["primaries"] = _COLR_NAMES["primaries"].get(pri, pri)
                                    out["transfer"] = _COLR_NAMES["transfer"].get(trc, trc)
                                    out["matrix"] = _COLR_NAMES["matrix"].get(mat, mat)
                                    if ctype == "nclx" and b7 - a7 >= 11:
                                        out["full_range"] = bool(moov[a7 + 10] >> 7)
                                return out
    return None


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
