"""FFmpeg/ffprobe process layer.

All ffmpeg execution funnels through here: binary discovery, the gate heavy
runs pass (renders are CPU/RAM-heavy — never run unbounded: a count, and the
memory budget they share), probe helpers, and filter-argument escaping.
Binaries come from PATH inside the container; FFMPEG_PATH/FFPROBE_PATH env
overrides exist for local test runs.
"""

import asyncio
import collections
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

import safe_fs
from shared import (
    OUTSIDE_REFUSED,
    _write_root,
    beneath_mount,
    logger,
    own_place,
    safe_open_read,
    safe_open_write,
    safe_read_fd,
)

FFMPEG = os.environ.get("FFMPEG_PATH", "") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_PATH", "") or shutil.which("ffprobe") or "ffprobe"

# Renders buffer frames at every xfade/overlay junction; two concurrent heavy
# renders on a 4g container is the safe ceiling, and then only when their
# memory fits the budget together. Analysis/probe calls are cheap and skip
# the gate.
MAX_RENDERS = max(1, int(os.environ.get("VIDEO_TOOLS_MAX_RENDERS", "2")))

OUT_OF_MEMORY = ("This step ran out of memory and was stopped. Work on a shorter part, "
                 "or ask an admin to give Video Tools more memory.")


def _cgroup_memory_limit_bytes() -> float | None:
    """Container memory limit when one applies (cgroup v2, then v1); None when
    unlimited or unreadable. sysconf reports the HOST's RAM even inside a
    mem_limit'ed container, so the budget must honor the cgroup cap or
    windowed planning sizes windows the container cannot hold."""
    for path, unlimited in (
        ("/sys/fs/cgroup/memory.max", "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None),
    ):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == unlimited:
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        if val >= 1 << 60:  # v1 reports a huge sentinel when unlimited
            return None
        return float(val)
    return None


def cpu_count() -> int:
    """The cores this container may use: its affinity, bounded by a cgroup
    CPU quota when one is set (``cpu.max``). ffmpeg sizes its decoder and
    encoder threads by the cores, and each thread holds frames."""
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            cores = min(cores, max(1, -(-int(quota) // int(period))))
    except (OSError, ValueError):
        pass
    return max(1, cores)


# libavcodec's own cap on the threads it picks by itself.
_MAX_AUTO_THREADS = 16


def render_threads() -> int:
    """The threads each decoder and the x264 encoder of a render run with:
    the container's cores and one, as libavcodec picks by itself, at most
    its own cap of 16. A render passes this count to ffmpeg, and the memory
    estimate counts the frames those threads hold by it."""
    return min(cpu_count() + 1, _MAX_AUTO_THREADS)


def render_budget_bytes() -> float:
    """The decoded-frame memory the heavy runs share: a render alone plans its
    windows against all of it, and ``heavy_run`` keeps the claims of the runs
    that go at once within it. Override with VIDEO_TOOLS_RENDER_BUDGET_MB;
    default 40% of physical RAM, bounded by the container's cgroup limit when
    one is set — the filtergraph pile is only part of the process (decoders,
    x264 and the canvas-side chains ride on top), and other services share
    the host."""
    env = os.environ.get("VIDEO_TOOLS_RENDER_BUDGET_MB")
    if env:
        try:
            return max(1, int(env)) * 1e6
        except ValueError:
            logger.warning(
                "ignoring non-integer VIDEO_TOOLS_RENDER_BUDGET_MB=%r", env)
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return 4e9
    limit = _cgroup_memory_limit_bytes()
    if limit is not None:
        total = min(total, limit)
    return max(min(1.5e9, total * 0.6), total * 0.4)


# Equal shares of a budget can sum a few millionths of a byte past it in
# floating point (three thirds of about one budget in twenty): one byte of
# slack lets the last share in.
_SLACK = 1.0


class _Gate:
    """At most ``MAX_RENDERS`` heavy runs at once, and the bytes they claim
    within ``render_budget_bytes()``. A run alone is always let in, whatever
    it claims: a window over the budget still renders, by itself. First come,
    first served: a run waits behind every earlier waiter, so a big claim is
    never passed over by smaller ones."""

    def __init__(self) -> None:
        self.held = 0
        self.claimed = 0.0
        self.line: collections.deque[tuple[float, asyncio.Future]] = collections.deque()

    def _fits(self, claim: float, budget: float) -> bool:
        return self.held < MAX_RENDERS and (
            self.held == 0 or self.claimed + claim <= budget + _SLACK)

    def _take(self, claim: float) -> None:
        self.held += 1
        self.claimed += claim

    def _wake(self) -> None:
        budget = render_budget_bytes()
        while self.line:
            claim, fut = self.line[0]
            if fut.done():
                # A cancelled waiter: its task leaves the line on its next step.
                self.line.popleft()
                continue
            if not self._fits(claim, budget):
                break
            self.line.popleft()
            self._take(claim)
            fut.set_result(None)

    async def acquire(self, claim: float) -> None:
        if not self.line and self._fits(claim, render_budget_bytes()):
            self._take(claim)
            return
        entry = (claim, asyncio.get_running_loop().create_future())
        self.line.append(entry)
        try:
            await entry[1]
        except asyncio.CancelledError:
            if entry[1].cancelled():
                with contextlib.suppress(ValueError):
                    self.line.remove(entry)
                self._wake()
            else:
                # Let in just before the cancel arrived.
                self.release(claim)
            raise

    def release(self, claim: float) -> None:
        self.held -= 1
        self.claimed = self.claimed - claim if self.held else 0.0
        self._wake()


_gate = _Gate()


@contextlib.asynccontextmanager
async def heavy_run(claim: float | None = None):
    """Hold the gate for one heavy ffmpeg run claiming ``claim`` bytes of the
    shared budget. Without a claim a run takes an equal share of it, so two
    such runs go at once."""
    bytes_ = render_budget_bytes() / MAX_RENDERS if claim is None else max(0.0, claim)
    await _gate.acquire(bytes_)
    try:
        yield
    finally:
        _gate.release(bytes_)


# ---------------------------------------------------------------------------
# The door every child passes: a person's file reaches ffmpeg, ffprobe and the
# libraries that read by name only as a descriptor this process opened
# beneath the mount with no link followed (``shared.safe_read_fd``), and an
# output only through the atomic writer (``shared.safe_open_write``).
# ---------------------------------------------------------------------------

# The demuxers a person's file is read with: the video, audio and picture
# containers the tool takes. Whatever its content says once the descriptor is
# open, a file cannot become a list that opens other names (HLS, concat,
# DASH), and no input opens anything over a network.
INPUT_FORMATS = ",".join((
    "mov", "matroska", "avi", "mpegts", "mpeg", "flv", "asf", "mxf", "ogg", "dv", "m4v", "h264",
    "hevc", "mp3", "wav", "w64", "aiff", "flac", "aac", "ac3", "eac3", "caf", "amr",
    "image2", "png_pipe", "jpeg_pipe", "webp_pipe", "bmp_pipe", "tiff_pipe", "gif", "apng",
    "jpegxl_pipe", "exr_pipe", "dpx_pipe", "qoi_pipe", "pam_pipe", "pgm_pipe", "ppm_pipe",
))
READ_GUARD = ("-protocol_whitelist", "file,pipe,fd", "-format_whitelist", INPUT_FORMATS)
# OpenCV's FFmpeg reads a file under the same two lists (scenedetect reads
# through OpenCV); it takes the options from its environment at each open.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"protocol_whitelist;file,pipe,fd|format_whitelist;{INPUT_FORMATS}"
# OpenCV reads through its FFmpeg reader alone. Every other reader of the
# pinned build is off for the process before its first capture (it reads
# these once): a reader it falls back to, the picture sequence one first,
# builds names of its own from the one it was given. scenedetect opens by
# itself, so naming the reader at the tool's own opens is not enough.
OPENCV_OTHER_READERS = ("GSTREAMER", "INTEL_MFX", "V4L2", "CV_IMAGES", "CV_MJPEG", "UEYE")
for _reader in OPENCV_OTHER_READERS:
    os.environ[f"OPENCV_VIDEOIO_PRIORITY_{_reader}"] = "0"
# The muxer of an output by its final name's suffix: a descriptor's name has
# none for ffmpeg to pick one by.
_MUXERS = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".webm": "webm", ".mkv": "matroska",
    ".gif": "gif", ".png": "image2", ".jpg": "image2", ".jpeg": "image2", ".webp": "webp",
    ".wav": "wav", ".mp3": "mp3", ".m4a": "ipod", ".aac": "adts", ".flac": "flac",
    ".ogg": "ogg", ".opus": "opus",
}
MOUNT_REFUSED = "a file of the agents tree reached the run other than as its input or its output"


def _muxer_of(name: str) -> list[str]:
    muxer = _MUXERS.get(Path(name).suffix.lower())
    if muxer is None:
        raise FFmpegError(f"{Path(name).name}: the tool does not write this kind of file")
    return ["-f", muxer, "-update", "1"] if muxer == "image2" else ["-f", muxer]


def _names_mount(arg: str) -> bool:
    root = _write_root().rstrip("/")
    return arg == root or (root + "/") in arg


def _own_input(value: str) -> None:
    """An input outside the mount: the tool's own pipe, or a file in one of
    its own folders (``own_place``). Anything else is refused before the
    run, a generated ``-f lavfi`` source too: a graph can open files of its
    own (``movie=``), and the tool builds none."""
    if value == "-" or value.startswith("pipe:"):
        return
    if own_place(value) is None:
        raise PermissionError(f"Cannot read '{value}': {OUTSIDE_REFUSED}")


def _own_output(value: str) -> None:
    """An output outside the mount, held as an input is: the tool's own
    pipe, or a file in one of its own folders. Anything else is refused
    before the run."""
    if value.startswith("pipe:"):
        return
    if own_place(value) is None:
        raise PermissionError(f"Cannot write '{value}': {OUTSIDE_REFUSED}")


@contextlib.contextmanager
def bound_files(argv: list[str], *, last_is_input: bool = False):
    """``(argv, fds)`` for a child: each ``-i`` value beneath the mount
    replaced by ``/proc/self/fd/N`` of the descriptor opened for it (with
    ``READ_GUARD`` before it), the last argument beneath the mount (the
    output, or with ``last_is_input`` ffprobe's input) handled the same way,
    an output through ``safe_open_write`` with its muxer named; ``fds`` is
    what the child keeps (``pass_fds``). An input or an output outside the
    mount is the tool's own or refused (``_own_input``, ``_own_output``),
    and any other argument that names the mount is refused, before the run. On a clean exit the
    output's temp lands on its name; on an error it is removed and nothing
    is written. The descriptors close with the block. Blocking: a thread
    runs it."""
    out: list[str] = []
    fds: list[int] = []
    last = len(argv) - 1
    with contextlib.ExitStack() as stack:
        i = 0
        while i <= last:
            arg = argv[i]
            is_input = arg == "-i" and i < last and beneath_mount(argv[i + 1])
            if is_input or (i == last and last_is_input and beneath_mount(arg)):
                name = argv[i + 1] if is_input else arg
                fd, _ = safe_read_fd(name)
                stack.callback(os.close, fd)
                fds.append(fd)
                out += [*READ_GUARD, *(["-i"] if is_input else []), safe_fs.fd_path(fd)]
                i += 2 if is_input else 1
                continue
            if arg == "-i" and i < last:
                _own_input(argv[i + 1])
            elif i == last and last_is_input:
                _own_input(arg)
            if i == last and beneath_mount(arg):
                fh = stack.enter_context(safe_open_write(arg))
                fds.append(fh.fileno())
                named = len(out) >= 2 and out[-2] == "-f"
                out += [*([] if named else _muxer_of(arg)), safe_fs.fd_path(fh.fileno())]
                i += 1
                continue
            if i == last and not last_is_input and not arg.startswith("-"):
                # An option at the end (``-version``) names no output.
                _own_output(arg)
            if _names_mount(arg):
                raise FFmpegError(MOUNT_REFUSED)
            out.append(arg)
            i += 1
        yield out, fds


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Carries a trimmed stderr tail for the agent."""

    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


class FFmpegTimeout(FFmpegError):
    """ffmpeg ran past its time and was stopped: its output was not written."""


def _stderr_tail(stderr: bytes, limit: int = 1200) -> str:
    text = stderr.decode(errors="replace").strip()
    # ffmpeg repeats progress lines; keep the tail where errors live.
    return text[-limit:] if len(text) > limit else text


async def run_ffmpeg(
    args: list[str],
    timeout: float = 600.0,
    heavy: bool = True,
    capture_stdout: bool = False,
    progress=None,
    claim: float | None = None,
    landing=None,
) -> tuple[bytes, str]:
    """Run ffmpeg with the given args (after the binary). Returns
    ``(stdout, stderr_text)``; raises FFmpegError on non-zero exit or timeout.
    ``progress(seconds)`` is called with the output time ffmpeg reports. A
    cancelled caller kills the process before the cancel goes on: a
    cancelled render never keeps encoding. A heavy run passes the gate first,
    claiming ``claim`` bytes of the shared budget (``heavy_run``).
    ``landing()`` is called once the child exited cleanly, in the same step
    of the loop that starts its output's landing: from then on the run is
    done and its output is written.
    """
    cmd = [FFMPEG, "-hide_banner", "-y", *args]
    if progress is not None and not capture_stdout:
        cmd = [FFMPEG, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats", *args]
    logger.info("ffmpeg: %s", " ".join(cmd[:24]) + (" …" if len(cmd) > 24 else ""))

    async def _read_progress(stream) -> bytes:
        async for line in stream:
            key, _, value = line.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                progress(int(value) / 1e6)
        return b""

    async def _run() -> tuple[bytes, str]:
        files = contextlib.ExitStack()
        try:
            argv, fds = await asyncio.to_thread(files.enter_context, bound_files(cmd))
        except BaseException:
            await asyncio.to_thread(files.close)
            raise
        try:
            result = await _spawn(argv, fds)
        except BaseException as exc:
            # The output's temp goes and nothing is written; a cancel cannot
            # cut the step that closes the descriptors.
            await asyncio.shield(asyncio.to_thread(files.__exit__, type(exc), exc, exc.__traceback__))
            raise
        if landing is not None:
            landing()
        await asyncio.shield(asyncio.to_thread(files.close))
        return result

    async def _spawn(argv: list[str], fds: list[int]) -> tuple[bytes, str]:
        piped = capture_stdout or progress is not None
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=fds,
        )
        try:
            if progress is not None and not capture_stdout:
                async def _both():
                    _, err = await asyncio.gather(_read_progress(proc.stdout), proc.stderr.read())
                    await proc.wait()
                    return b"", err
                stdout, stderr = await asyncio.wait_for(_both(), timeout=timeout)
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise FFmpegTimeout(f"ffmpeg timed out after {int(timeout)}s")
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode == -signal.SIGKILL:
            # Neither the timeout nor a cancel gets here: the kernel ended it,
            # which in the tool's container is its memory limit.
            raise FFmpegError(OUT_OF_MEMORY, _stderr_tail(stderr))
        if proc.returncode != 0:
            tail = _stderr_tail(stderr)
            raise FFmpegError(
                f"ffmpeg failed (exit {proc.returncode}): {tail[-400:]}", tail
            )
        return stdout or b"", _stderr_tail(stderr, 4000)

    if heavy:
        async with heavy_run(claim):
            return await _run()
    return await _run()


# Inputs that are lists of other inputs: ffmpeg opens each name they hold by
# itself, past every check this tool made on the input's own name. The image's
# ffprobe picks concat and hls by their content. Refused before ffprobe opens
# anything (by the first bytes) and after it (by the format it names). An XML
# file is a list only when it is a DASH manifest: an SVG picture is XML too.
LIST_FORMATS = frozenset({"concat", "hls", "dash", "sdp", "rtp", "rtsp"})
_LIST_HEADS = (b"#EXTM3U", b"ffconcat", b"v=0")
LIST_REFUSED = "this file lists other files, which the tool does not open"
UNREAD_KIND = "The tool does not read this kind of file: {name}."


def _unread_kind(stderr: str) -> bool:
    """ffprobe found no format the tool reads the file with: a format it
    knows but the tool does not take ("not on whitelist"), or none at all
    (its one line says the data is invalid)."""
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    if any("not on whitelist" in line for line in lines):
        return True
    return len(lines) == 1 and lines[0].endswith("Invalid data found when processing input")


def _lists_other_files(path: str) -> bool:
    try:
        with safe_open_read(path) as fh:
            head = fh.read(512).lstrip(b"\xef\xbb\xbf \t\r\n")
    except OSError:
        return False
    return head.startswith(_LIST_HEADS) or b"<MPD" in head


async def probe(path: str) -> dict:
    """ffprobe → parsed JSON with format + streams. A list of other files is
    refused (``LIST_FORMATS``) by its first bytes, by the format it names,
    and by the demuxers a person's file may be read with (``READ_GUARD``)."""
    if await asyncio.to_thread(_lists_other_files, path):
        raise FFmpegError(f"{path}: {LIST_REFUSED}")
    argv = [FFPROBE, "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams", path]
    files = contextlib.ExitStack()
    try:
        argv, fds = await asyncio.to_thread(files.enter_context, bound_files(argv, last_is_input=True))
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, pass_fds=fds,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise FFmpegError("ffprobe timed out (30s)")
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
    finally:
        await asyncio.shield(asyncio.to_thread(files.close))
    if proc.returncode != 0:
        if _unread_kind(stderr.decode(errors="replace")):
            raise FFmpegError(UNREAD_KIND.format(name=Path(path).name))
        raise FFmpegError(f"ffprobe failed: {_stderr_tail(stderr, 400)}")
    try:
        info = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned unparseable output: {exc}")
    names = str((info.get("format") or {}).get("format_name") or "").split(",")
    if LIST_FORMATS.intersection(names):
        raise FFmpegError(f"{path}: {LIST_REFUSED}")
    return info


def run_sync(argv: list[str], *, last_is_input: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` of a child through the door (``bound_files``), for
    code that already runs in a thread."""
    with bound_files(argv, last_is_input=last_is_input) as (bound, fds):
        return subprocess.run(bound, pass_fds=fds, **kwargs)


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


def stream_rotation(stream: dict) -> float:
    """The display-matrix rotation a video stream carries (degrees, ffprobe
    sign: −90 = the file wants a 90° clockwise turn), 0 when untagged.
    Both the modern side data and the legacy `rotate` tag are read."""
    for sd in stream.get("side_data_list", []) or []:
        rot = sd.get("rotation")
        if rot is not None:
            try:
                return float(rot)
            except (TypeError, ValueError):
                continue
    try:
        return float((stream.get("tags") or {}).get("rotate", 0) or 0)
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

    with safe_open_read(path) as fh:
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
# name (no user-controlled characters).


def ff_color(hex_or_name: str) -> str:
    """A colour in ffmpeg syntax, or ValueError for anything that is not one.

    '#RRGGBB' → '0xRRGGBB' ('#' starts a comment inside a
    -filter_complex_script file, so it must never reach the graph). Named
    colors pass through. The graph carries the value as written, so only a
    colour may reach it (``color.graph_colour``).
    """
    import color as color_mod
    return color_mod.graph_colour((hex_or_name or "").strip() or "black")


def _f(v: float) -> str:
    """A number for ffmpeg's arguments: fixed decimals to the microsecond,
    trailing zeros cut. ``%g`` keeps six digits in all, so a cut at
    1234.5678 s became 1234.57, a third of a frame off."""
    text = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


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


# The bytes a decoded frame holds per pixel by its pixel format: the planes
# with their chroma subsampling, two bytes a sample past 8 bits and four
# past 16, alpha as one more full plane. 8-bit 4:2:0 is 1.5, 10-bit 4:2:0 is
# 3, 10-bit 4:2:2 is 4, 8-bit 4:4:4 is 3. No format is counted short: one
# the names below miss counts 8 bytes.
_PACKED_BYTES = {
    "rgb24": 3, "bgr24": 3, "rgb48": 6, "bgr48": 6, "rgba64": 8, "bgra64": 8,
    "rgba": 4, "bgra": 4, "argb": 4, "abgr": 4, "rgb0": 4, "bgr0": 4, "0rgb": 4, "0bgr": 4,
    "x2rgb10": 4, "x2bgr10": 4, "yuyv422": 2, "uyvy422": 2, "yvyu422": 2, "y210": 4,
    "y212": 4, "pal8": 1, "ya8": 2, "ya16": 4, "monow": 1, "monob": 1,
    "rgbf16": 6, "rgbaf16": 8, "rgbf32": 12, "rgbaf32": 16, "xyz12": 6, "ayuv64": 8,
    "xv30": 4, "xv36": 8, "vuya": 4, "vuyx": 4, "uyyvyy411": 1.5,
}
_CHROMA_SAMPLES = {"420": 0.5, "422": 1.0, "444": 2.0, "440": 1.0, "411": 0.5, "410": 0.25}
_NV_BYTES = {"12": 1.5, "21": 1.5, "16": 2.0, "61": 2.0, "24": 3.0, "42": 3.0, "20": 4.0}
_PLANAR_RE = re.compile(r"(yuvj?|yuva|gbrp|gbrap|gray)(\d{3})?p?f?(\d*)")


def frame_bytes_per_pixel(pix_fmt: str | None) -> float:
    """The bytes per pixel of a frame decoded in ``pix_fmt`` (the probe's
    name). A source the probe names no format for counts as 8-bit 4:2:0."""
    name = str(pix_fmt or "").lower()
    if not name:
        return 1.5
    if name.endswith(("le", "be")):
        name = name[:-2]
    if name in _PACKED_BYTES:
        return float(_PACKED_BYTES[name])
    if re.fullmatch(r"p[024]\d\d", name):      # p010, p210, p416: 16-bit samples
        return (1.0 + _CHROMA_SAMPLES[{"0": "420", "2": "422", "4": "444"}[name[1]]]) * 2
    if name.startswith("nv") and name[2:] in _NV_BYTES:
        return _NV_BYTES[name[2:]]
    m = _PLANAR_RE.fullmatch(name)
    if not m:
        return 8.0
    kind, sub, depth = m.groups()
    if kind in ("gbrp", "gbrap"):
        samples = 3.0
    elif kind == "gray":
        samples = 1.0
    elif sub in _CHROMA_SAMPLES:
        samples = 1.0 + _CHROMA_SAMPLES[sub]
    else:
        return 8.0
    if kind in ("yuva", "gbrap"):
        samples += 1.0
    bits = int(depth or 8)
    return samples * (1 if bits <= 8 else 2 if bits <= 16 else 4)
