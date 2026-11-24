"""Shared utilities for the video-tools MCP server.

Config, per-request session binding, path mapping, proxy-hook helpers.
Mirrors the platform's file-tools MCP: this is a SHARED container serving
every session of the install, so all session state is request-scoped.
"""

import asyncio
import base64
import contextlib
import contextvars
import errno
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import BinaryIO

import httpx

import safe_fs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("PROXY_URL", "")
# `/agents` is the canonical mount point inside the container (declared in
# docker-compose.yml). Hardcoded — not a config knob.
MOUNT_AGENTS_DIR = "/agents"
# A remote machine's file the proxy pulled for one session, and the slugs an
# answer may name.
HOST_CACHE_SEGMENT = ".remote-host-cache"
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
MCP_PORT = int(os.environ.get("MCP_PORT", "8933"))
# The resolve hook pulls a remote machine's file before it answers: over a WAN
# link a few MB takes tens of seconds (the proxy's own pull budget is 180 s).
HOOK_TIMEOUT = httpx.Timeout(connect=5.0, read=150.0, write=30.0, pool=5.0)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("video-tools")

# ---------------------------------------------------------------------------
# Per-request session binding (session_id + auth) via contextvars
# ---------------------------------------------------------------------------
#
# The proxy injects, per session, a `?session_id=` URL param AND an
# `Authorization: Bearer <session-JWT>` header. Both are bound per request at
# the transport boundary; the streamable-HTTP transport runs stateless, so the
# values propagate into the tool handler's task group and never bleed across
# concurrent sessions. Empty defaults fail CLOSED ("not session-bound"), never
# another session's identity.

_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_session_id", default=""
)
_auth_header_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_auth", default=""
)
# The session's agent, as the proxy named it in this request's first answer:
# a write in the form the results print is mapped to its sandbox form.
_agent_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "video_tools_agent", default=""
)


# What each file beneath the mount was when the call first opened it, by its
# path: a later open in the same call (the encode after the probe, a
# background job hours later) of another file at that name is refused. A
# write of the tool's own through ``safe_open_write`` drops its name.
_checked_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "video_tools_checked", default=None)


def begin_call() -> None:
    """A fresh record of what this tool call opens (``_checked_var``)."""
    _checked_var.set({})


def _hold_identity(path: str | os.PathLike[str], st: os.stat_result) -> None:
    """Refuse ``path`` when the call opened another file at that name
    before: one with another device, inode, size or change time."""
    book = _checked_var.get()
    if book is None or not beneath_mount(path):
        return
    seen = book.setdefault(os.fspath(path), (st.st_dev, st.st_ino, st.st_size, st.st_ctime_ns))
    if seen != (st.st_dev, st.st_ino, st.st_size, st.st_ctime_ns):
        raise PermissionError(f"Cannot read '{_to_agents_relative(os.fspath(path))}': {CHANGED}")


def set_request_context(session_id: str, auth_header: str) -> None:
    """Bind the in-flight request's session_id + Authorization header.

    Called at the transport boundary (server.py handle_sse / mcp_asgi_app)
    BEFORE the MCP SDK dispatches the tool handler.
    """
    _session_id_var.set(session_id or "")
    _auth_header_var.set(auth_header or "")


def _current_session() -> tuple[str, str]:
    """``(session_id, authorization_header)`` for the in-flight request."""
    return _session_id_var.get(), _auth_header_var.get()


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------
#
# Inbound (LLM → video-tools): paths arrive as sandbox-virtual or
# agents-relative strings. We always ask the proxy's /v1/hooks/resolve-path to
# translate them to a canonical agents-relative path, then prepend `/agents/`
# for the mounted container view.
#
# Outbound (video-tools → proxy hooks): we post agents-relative paths.


def _to_agents_relative(container_path: str) -> str:
    """Strip the `/agents/` mount prefix to produce an agents-relative path
    suitable for posting to the proxy hooks."""
    if container_path.startswith(MOUNT_AGENTS_DIR + "/"):
        return container_path[len(MOUNT_AGENTS_DIR) + 1:]
    if container_path == MOUNT_AGENTS_DIR:
        return ""
    return container_path  # already agents-relative or out-of-tree


async def _resolve_via_proxy(path: str, writing: bool = False) -> tuple[str | None, str, str]:
    """Ask the proxy to translate a path to an agents-relative path.

    Returns ``(agents_relative, agent, "")`` on success, ``agent`` being the
    slug the proxy names for the session. On failure returns ``(None, "",
    reason)`` where ``reason`` carries the proxy's real verdict (403 policy
    reject / 404 not reachable) so the caller can surface it. ``writing``
    marks a write target: the proxy judges it by the session's write rules.
    Async: this container serves every session, and a remote pull can take
    minutes on the shared loop otherwise.
    """
    session_id, auth = _current_session()
    if not session_id or not PROXY_URL or not auth:
        return None, "", "video-tools is not session-bound (missing session_id/PROXY_URL/auth)"
    try:
        async with httpx.AsyncClient(timeout=HOOK_TIMEOUT) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/resolve-path",
                json={"session_id": session_id, "path": path, "writing": writing},
                headers={"Authorization": auth},
            )
        if resp.status_code == 200:
            data = resp.json()
            agents_rel = data.get("agents_relative", "")
            if agents_rel:
                return agents_rel, str(data.get("agent") or ""), ""
            return None, "", (
                "proxy resolved the path but returned no agents-relative "
                "mapping (it is outside the synced agent tree)"
            )
        detail = ""
        try:
            detail = str(resp.json().get("detail", "")).strip()
        except (ValueError, AttributeError):
            detail = (resp.text or "")[:200].strip()
        return None, "", f"proxy resolve-path {resp.status_code}: {detail or '(no detail)'}"
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        logger.debug(f"resolve-path failed for '{path}': {e}")
        return None, "", f"resolve-path request failed: {e}"


def _session_subtree(slug: str) -> str | None:
    """The one tree this request may touch, as a rel under the mount: the
    session's own agent folder, or, for a remote machine's file, the
    session's own host-cache folder. None for anything else."""
    if slug == HOST_CACHE_SEGMENT:
        session_id = _session_id_var.get()
        if not _SAFE_SLUG_RE.match(session_id or ""):
            return None
        return f"{HOST_CACHE_SEGMENT}/{session_id}"
    if _SAFE_SLUG_RE.match(slug or ""):
        return slug
    return None


def _answer_in_tree(agents_rel: str, agent: str) -> str | None:
    """The container path of the server's answer when its TEXT stays in the
    session's own tree, else None. Nothing on disk is consulted: every part
    must be a plain name (a ``..``, a ``.`` or an empty part is refused,
    never folded), and the open that follows refuses any link met on the
    way."""
    subtree = _session_subtree(agent)
    if subtree is None:
        return None
    try:
        rel = safe_fs.rel_under(MOUNT_AGENTS_DIR + "/" + agents_rel.lstrip("/"), MOUNT_AGENTS_DIR)
    except OSError:
        return None
    # The separator matters: a bare prefix test admits a sibling such as
    # proj-a-evil/x.
    if rel == subtree or rel.startswith(subtree + "/"):
        return MOUNT_AGENTS_DIR + "/" + rel
    return None


async def _write_form(path: str) -> str:
    """A write named in the form the tool's own results print
    (``<agent>/users/...``), which the proxy admits for a read only: its
    sandbox form. The agent comes from an earlier answer in this request,
    else from a read answer for ``/workspace`` (asked only when the first
    part could be an agent's name)."""
    if path.startswith("/") or "/" not in path:
        return path
    head, rest = path.split("/", 1)
    agent = _agent_var.get()
    if not agent and _SAFE_SLUG_RE.match(head):
        agent = (await _resolve_via_proxy("/workspace"))[1]
        if agent and agent != HOST_CACHE_SEGMENT:
            _agent_var.set(agent)
    return "/" + rest if agent and head == agent else path


def _unicode_match_on_disk(path: str) -> str:
    """``path``, or the name in its folder that differs from it only by its
    Unicode normal form (a file Drive or macOS wrote in NFD, named back in
    NFC by a transport). Looked at beneath the write root with no link
    followed; blocking, so a thread runs it. Falls through with the path
    when something sits there verbatim, when it is not beneath the root or
    its folder cannot be opened without a link (the strict open answers),
    and when nothing matches."""
    root = _write_root()
    try:
        rel = safe_fs.rel_under(path, root)
    except OSError:
        return path
    parent_rel, _, basename = rel.rpartition("/")
    if not basename or safe_fs.kind_beneath(root, rel).kind != "missing":
        return path
    target_nfc = unicodedata.normalize("NFC", basename)
    try:
        fd = safe_fs.open_dir_beneath(root, parent_rel)
    except OSError:
        return path
    try:
        entries = os.listdir(fd)
    except OSError:
        return path
    finally:
        os.close(fd)
    for entry in entries:
        if unicodedata.normalize("NFC", entry) == target_nfc:
            return path.rstrip("/").rpartition("/")[0] + "/" + entry
    return path


async def _resolve_path(path: str, writing: bool = False) -> str:
    """Resolve a tool-input path to a container-local path, judged by the
    proxy for the calling session.

    Accepts sandbox-virtual (`/users/alice/workspace/foo`), agents-relative
    (`agent/users/alice/...`), or container-absolute (`/agents/...`) strings.
    Every form goes to the proxy: the mount holds every agent's tree, so a
    prefix says nothing about this session's rights on a file. ``writing``
    marks every path the tool creates or changes.

    The answer is confined to the session's own tree on its text (the agent
    the proxy names, or the session's own host-cache folder) and never
    resolved again: the server answered the place every link led to, and
    every reader and writer opens it beneath the mount with no link
    followed, so a link put on the way afterwards is refused, never
    followed.
    """
    if path == MOUNT_AGENTS_DIR:
        path = ""
    elif path.startswith(MOUNT_AGENTS_DIR + "/"):
        path = path[len(MOUNT_AGENTS_DIR) + 1:]
    if writing:
        path = await _write_form(path)

    agents_rel, agent, reason = await _resolve_via_proxy(path, writing=writing)
    if agents_rel and not agent:
        reason = "the proxy answered without the session's agent"
    elif agents_rel:
        container = _answer_in_tree(agents_rel, agent)
        if container is not None:
            if agent != HOST_CACHE_SEGMENT:
                _agent_var.set(agent)
            return await asyncio.to_thread(_unicode_match_on_disk, container)
        reason = "the path leaves this session's agent tree"

    raise ValueError(
        f"Cannot open '{path}': {reason}"
        if reason else
        f"Path could not be resolved: {path}"
    )


def _on_a_computer(container_path: str) -> bool:
    """A remote machine's file the proxy pulled for this session: its
    folder is a cache, never a place the session writes."""
    return container_path.startswith(f"{MOUNT_AGENTS_DIR}/{HOST_CACHE_SEGMENT}/")


async def _write_beside(container_path: str) -> str | None:
    """A file the tool writes on its own next to a source (an analysis
    sidecar, a stabilization cache, a default output): judged as a write for
    the session, or None when the session may not write there or the source
    sits on a computer (the tool then keeps the result in its answer or its
    own scratch folder)."""
    if _on_a_computer(container_path):
        return None
    try:
        return await _resolve_path(container_path, writing=True)
    except ValueError as exc:
        logger.info("not written beside the source: %s", exc)
        return None


async def _read_beside(container_path: str) -> str | None:
    """A file the tool reads on its own next to a source (a stabilization
    cache): judged as a read for the session like any file it names, or None
    when the session may not read it or the source sits on a computer (nothing
    is written beside it there)."""
    if _on_a_computer(container_path):
        return None
    try:
        return await _resolve_path(container_path)
    except ValueError as exc:
        logger.info("not read beside the source: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Opening a file: beneath the mount, no link followed
# ---------------------------------------------------------------------------
#
# A resolved path was judged by the proxy and confined above, but a folder on
# the way or the name itself can be swapped for a link before the open. Every
# read and write of a person's file goes through these helpers, the same
# names and meaning as file-tools' (safe_fs is the proxy's helper, copied):
# the kernel resolves the path from the mount with no link followed, a pipe or
# a device is refused, and a write is whole or not there. Outside the mount a
# file opens only in one of the tool's own folders (``own_place``), beneath
# that folder with no link followed, and any other path is refused. Every
# call blocks: run it in a thread.

_root_fixed = False


def fix_write_root() -> None:
    """Pin the mount for a running tool (the server's start): from then on
    ``_write_root`` is ``MOUNT_AGENTS_DIR`` whatever the environment says."""
    global _root_fixed
    _root_fixed = True


def _write_root() -> str:
    """The root every file opens beneath: the mount. The suite points it at
    a temporary tree with ``VIDEOTOOLS_WRITE_ROOT``, which a running tool
    never reads (``fix_write_root``)."""
    if _root_fixed:
        return MOUNT_AGENTS_DIR
    return os.environ.get("VIDEOTOOLS_WRITE_ROOT") or MOUNT_AGENTS_DIR


def beneath_mount(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` names a place beneath the mount, on its text."""
    try:
        safe_fs.rel_under(path, _write_root())
    except OSError:
        return False
    return True


# The tool's own folders outside the mount: its temp folders (a ``vt-``
# folder in the temp folder, one per call or cache) and the folders a module
# names with ``own_folder`` (the image's looks and models, the slow-motion
# cache). A file outside the mount opens only in one of them.
_OWN_TEMP_PREFIX = "vt-"
_own_folders: list[str] = []
OUTSIDE_REFUSED = "it is outside the agents tree and the tool's own folders"
CHANGED = "it changed after this call checked it. Run the call again"


def own_folder(path: str | os.PathLike[str]) -> None:
    """Name a folder of the tool's own outside the mount, where its files
    may open: called by the module that keeps the folder."""
    text = os.path.normpath(os.fspath(path))
    if os.path.isabs(text) and text not in _own_folders:
        _own_folders.append(text)


def own_place(path: str | os.PathLike[str]) -> tuple[str, str] | None:
    """``(folder, rel)`` for a path in one of the tool's own folders, judged
    on its text (a dot part or an empty part is no place); None for any
    other path."""
    text = os.fspath(path)
    if not isinstance(text, str):
        return None
    temp = tempfile.gettempdir()
    try:
        rel = safe_fs.rel_under(text, temp)
    except OSError:
        rel = ""
    top, _, rest = rel.partition("/")
    if top.startswith(_OWN_TEMP_PREFIX):
        return os.path.join(temp, top), rest
    try:
        return safe_fs.split_under(text, _own_folders)
    except OSError:
        return None


def _rel_beneath_root(container_path: str) -> tuple[str, str]:
    root = _write_root()
    try:
        return root, safe_fs.rel_under(container_path, root)
    except OSError:
        raise PermissionError(
            f"Cannot write '{_to_agents_relative(container_path)}': outside the writable tree"
        ) from None


@contextlib.contextmanager
def safe_open_write(container_path: str):
    """A binary file to write the output at ``container_path`` into; on a
    clean exit the bytes replace the name atomically beneath the mount with
    no component followed (a failure leaves no temp behind). The temp is
    ``.<name>.<hex>.partial`` beside the name, which the file sync skips and
    retention reaps."""
    root, rel = _rel_beneath_root(container_path)
    try:
        with safe_fs.atomic_writer(root, rel, mkdirs=True) as fh:
            yield fh
    except safe_fs.SafeFsError as exc:
        raise PermissionError(
            f"Cannot write '{_to_agents_relative(container_path)}': {exc.strerror}"
        ) from None
    book = _checked_var.get()
    if book is not None:
        book.pop(os.fspath(container_path), None)


def _read_place(path: str | os.PathLike[str]) -> tuple[str, str]:
    """``(root, rel)`` for a path beneath the write root, or in one of the
    tool's own folders (``own_place``). Anything else raises
    ``SafeFsError``: a dot part, a relative path, or any other place
    (``OUTSIDE_REFUSED``)."""
    root = _write_root()
    try:
        return root, safe_fs.rel_under(path, root)
    except safe_fs.EscapeRefused:
        place = own_place(path)
        if place is None:
            raise safe_fs.EscapeRefused(errno.EXDEV, OUTSIDE_REFUSED, os.fspath(path)) from None
        return place


def _read_refused(path: str | os.PathLike[str], exc: safe_fs.SafeFsError) -> PermissionError:
    return PermissionError(f"Cannot read '{_to_agents_relative(os.fspath(path))}': {exc.strerror}")


def safe_open_read(path: str | os.PathLike[str]) -> BinaryIO:
    """``path`` opened for reading as a binary file, beneath the mount or
    one of the tool's own folders with no component followed through a
    link; refused with "Cannot read '<path>': <why>". A missing file stays
    ``FileNotFoundError``."""
    try:
        return safe_fs.open_file_for_read(*_read_place(path))
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None


def safe_read_fd(path: str | os.PathLike[str]) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` of ``path`` opened as ``safe_open_read`` does, for a
    program or a library that needs a name: hand it ``safe_fs.fd_path(fd)``
    (a child process gets the descriptor through ``pass_fds``). The caller
    closes ``fd``. A file the call opened before must be the same file
    (``_hold_identity``)."""
    try:
        fd, st = safe_fs.open_regular_for_read(*_read_place(path))
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None
    try:
        _hold_identity(path, st)
    except PermissionError:
        os.close(fd)
        raise
    return fd, st


def read_identity(path: str | os.PathLike[str]) -> os.stat_result:
    """The status of ``path`` as ``safe_read_fd`` opens it, for a key that
    names the file: the call's later opens of that name are held to it."""
    fd, st = safe_read_fd(path)
    os.close(fd)
    return st


def safe_read_bytes(path: str | os.PathLike[str], max_size: int) -> bytes:
    """The whole of ``path`` read as ``safe_open_read`` does, refused past
    ``max_size`` bytes (``safe_fs.FileTooLarge``)."""
    with safe_open_read(path) as fh:
        if os.fstat(fh.fileno()).st_size > max_size:
            raise safe_fs.FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(path))
        data = fh.read(max_size + 1)
    if len(data) > max_size:
        raise safe_fs.FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(path))
    return data


@contextlib.contextmanager
def opened_name(path: str | os.PathLike[str]):
    """A name a library reads ``path`` by (OpenCV, scenedetect): the
    ``/proc/self/fd/N`` of a descriptor ``safe_read_fd`` opened, closed when
    the block ends. The thread that reads holds the block itself, so a
    cancelled caller never closes the descriptor under it."""
    fd, _ = safe_read_fd(path)
    try:
        yield safe_fs.fd_path(fd)
    finally:
        os.close(fd)


def open_capture(path: str | os.PathLike[str]):
    """An OpenCV capture of ``path``, opened through ``opened_name`` by
    OpenCV's FFmpeg reader alone (``fftools`` sets its lists and switches
    the other readers off): the capture holds a descriptor of its own once
    open, so this one closes at once. Blocking: a thread runs it."""
    import cv2

    import fftools  # noqa: F401  (OpenCV's reader settings, before its first capture)

    with opened_name(path) as name:
        return cv2.VideoCapture(name, cv2.CAP_FFMPEG)


def safe_write_bytes(container_path: str, data: bytes) -> None:
    """``data`` written at ``container_path`` whole or not at all
    (``safe_open_write``)."""
    with safe_open_write(container_path) as fh:
        fh.write(data)


LANDED_GONE = "{name} was written, but it was gone before this answer was made. Run the call again."


def landed_size_mb(path: str | os.PathLike[str]) -> float:
    """The size in MB of a file the call just wrote. Gone, or no file, by
    then: ``FileNotFoundError`` carrying ``LANDED_GONE``. Blocking: a thread
    runs it."""
    kind = safe_kind(path)
    if kind.kind != "file" or kind.st is None:
        raise FileNotFoundError(LANDED_GONE.format(name=os.path.basename(os.fspath(path))))
    return kind.st.st_size / 1e6


def safe_kind(path: str | os.PathLike[str]) -> safe_fs.NameKind:
    """What sits at ``path`` with no link followed (``kind_beneath``,
    beneath the mount or one of the tool's own folders): the status check
    in front of a read. Any other path is refused as a read is."""
    try:
        place = _read_place(path)
    except safe_fs.SafeFsError as exc:
        raise _read_refused(path, exc) from None
    return safe_fs.kind_beneath(*place)


def safe_is_file(path: str | os.PathLike[str]) -> bool:
    """Whether a regular file sits at ``path`` (no link followed)."""
    try:
        return safe_kind(path).kind == "file"
    except PermissionError:
        return False


def private_copy(path: str | os.PathLike[str], folder: str, max_size: int) -> str:
    """A copy of ``path`` read through ``safe_open_read`` at
    ``folder/<its name>`` (a folder of the call's own), refused past
    ``max_size`` bytes: what a program the tool starts reads instead of the
    person's file when it cannot be handed a descriptor."""
    target = os.path.join(folder, os.path.basename(os.fspath(path)))
    with safe_open_read(path) as src:
        size = os.fstat(src.fileno()).st_size
        if size > max_size:
            raise safe_fs.FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(path))
        try:
            with open(target, "xb") as dst:
                safe_fs.copy_fd(src, dst, max_size=max_size)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
    return target


def place_copy(temp: str, container_path: str) -> None:
    """A copy of the tool's own file at ``temp`` written to ``container_path``
    beneath the mount as one atomic replace. A target anywhere else is
    refused (``safe_open_write``)."""
    with open(temp, "rb") as src, safe_open_write(container_path) as dst:
        safe_fs.copy_fd(src, dst)


def place_file(temp: str, container_path: str) -> None:
    """``place_copy``, then ``temp`` removed: a finished file of the tool's
    own lands on the output."""
    place_copy(temp, container_path)
    with contextlib.suppress(OSError):
        os.unlink(temp)


# A temp no write has touched for this long belongs to no live writer: the
# platform's retention reaps it at the same age (``PARTIAL_ORPHAN_AGE_S``).
PARTIAL_ORPHAN_AGE_S = 3600


def _process_start() -> float:
    """When this process started, on the wall clock files are stamped by:
    from the kernel's process table, else the time of this import."""
    try:
        with open("/proc/self/stat", encoding="ascii") as fh:
            ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="ascii") as fh:
            boot = next(float(line.split()[1]) for line in fh if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return time.time()


PROCESS_START = _process_start()


def _no_writer_holds(st: os.stat_result, now: float) -> bool:
    """A temp last written before this process started (a write the
    container's end stopped), or not written for an hour. A live writer, of
    this process or any other, moves its temp's time with every write."""
    return st.st_mtime < PROCESS_START or now - st.st_mtime >= PARTIAL_ORPHAN_AGE_S


def cleanup_partials(container_path: str) -> None:
    """Remove the temps beside ``container_path`` (``.<name>.<hex>.partial``)
    that no writer can hold (``_no_writer_holds``). Another writer's temp of
    that name stays. Best-effort."""
    root = _write_root()
    try:
        parent_rel, _, name = safe_fs.rel_under(container_path, root).rpartition("/")
        fd = safe_fs.open_dir_beneath(root, parent_rel)
    except OSError:
        return
    now = time.time()
    try:
        for entry in os.listdir(fd):
            if not safe_fs.is_partial_of(entry, name):
                continue
            with contextlib.suppress(OSError):
                if _no_writer_holds(os.stat(entry, dir_fd=fd, follow_symlinks=False), now):
                    os.unlink(entry, dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _op_type(op: dict) -> str:
    """Extract operation type — LLMs may use 'type', 'op', 'operation', or 'action'."""
    return op.get("type") or op.get("op") or op.get("operation") or op.get("action") or ""


def _normalize_operations(ops) -> list[dict]:
    """Normalize an operations argument to a list of dicts.

    Some LLMs double-encode array parameters as JSON strings. Accept any of:
    list of dicts, list of JSON strings, JSON string of a list, JSON string of
    a single op, or a single dict. Malformed items are dropped so the rest
    still run.
    """
    if ops is None:
        return []
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        return []
    normalized: list[dict] = []
    for op in ops:
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except (json.JSONDecodeError, ValueError):
                continue
        if isinstance(op, dict):
            normalized.append(op)
    return normalized


# ---------------------------------------------------------------------------
# Proxy-hook helpers
# ---------------------------------------------------------------------------


async def _notify_file_written(file_path: str) -> bool:
    """Tell the proxy that we just finished writing a file.

    For remote agent sessions the proxy pushes the file back to the satellite
    so the agent CLI and downstream MCPs see the updated content. No-op for
    local sessions. Fire-and-forget — tool success is independent of sync.
    """
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return False
    agents_rel = _to_agents_relative(file_path)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PROXY_URL}/v1/hooks/file-written",
                json={"session_id": session_id, "path": agents_rel},
                headers={"Authorization": auth},
            )
            if resp.status_code == 200:
                return bool(resp.json().get("ok"))
    except Exception as exc:
        logger.warning(f"file-written notify failed (non-fatal): {exc}")
    return False


async def _push_image_preview(image_bytes: bytes, mime: str, caption: str = ""):
    """Push an inline image preview to the dashboard (user-visible)."""
    session_id, auth = _current_session()
    if not PROXY_URL or not session_id or not auth:
        return
    b64 = base64.b64encode(image_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{PROXY_URL}/v1/hooks/images",
                json={
                    "session_id": session_id,
                    "images": [{
                        "image_data": b64,
                        "mime_type": mime,
                        "caption": caption,
                    }],
                },
                headers={"Authorization": auth},
            )
    except Exception as exc:
        logger.warning(f"Image preview push failed (non-fatal): {exc}")
