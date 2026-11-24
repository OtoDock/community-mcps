"""render_motion_clip: agent-authored HTML/CSS → video via headless Chromium.

Deterministic frame stepping: Playwright's Clock API fakes Date/timers/rAF
(drives GSAP-style JS animation), and every CSS/WAAPI animation is seeked
explicitly per frame via document.getAnimations() — so frame n is always
exactly t = n/fps regardless of render speed. Screenshots (optionally with
transparent background) are assembled by ffmpeg into MP4, alpha WebM
(usable directly as a composition overlay), GIF, or animated WebP.

Security posture: a fresh context per render, and the page reaches no
network. Every request the page makes passes its route, which serves the
page, data, blob and about URLs and the files the session may read, and
refuses everything else. Service workers are blocked and every WebSocket is
refused. A request the route never sees finds no network: the browser's
proxy is a loopback port where nothing listens, it resolves no name, and
WebRTC sends nothing outside that proxy. That rests on Chromium's own
settings, with its sandbox off in the container. file:// references are
judged for the session before load, and an unresolvable one is an error
before capture. Frame and size caps bound a runaway render, and one capture
is served at most 64 names and 256 MB.
"""

import asyncio
import contextlib
import json
import mimetypes
import posixpath
import re
import shutil
import tempfile
import time
import urllib.parse
import weakref
from dataclasses import dataclass, field
from pathlib import Path

import safe_fs
from fftools import FFmpegError, FFmpegTimeout, ff_color, media_duration, probe, run_ffmpeg
from shared import (
    LANDED_GONE,
    _notify_file_written,
    _resolve_path,
    _to_agents_relative,
    landed_size_mb,
    logger,
    safe_kind,
    safe_read_bytes,
    safe_write_bytes,
)

MAX_DURATION = 120.0
MAX_FPS = 60
MAX_DIM = 4096

# Virtual-clock settle before frame 0: pause_at() fast-forwards 0→settle
# (timers and rAF fire), so at frame n Date.now() == SETTLE_SECONDS*1000 +
# n·frame_ms. The anchor is exported to the page as window.VT_T0 — scene
# code writes `const t = (Date.now() - (window.VT_T0 ?? 0)) / 1000` and
# never needs to probe rAF timestamps. (Stills run 0-anchored: no settle.)
SETTLE_SECONDS = 1.0

_FILE_REF_RE = re.compile(r"file://[^\s\"'()<>]+")


# A page loads from this origin: every request it makes is answered by its
# route from a file read beneath the mount, or refused, so the browser never
# opens a name of the agents tree itself (a link put on the way after the
# check is never followed). The document is the origin's page; a reference
# relative to it is relative to the html_path's folder, a root-absolute one is
# a path in the session's own form, and both are judged by the proxy.
ORIGIN = "https://motion.invalid"
_PAGE_PREFIX = "/__page__/"
PAGE_URL = ORIGIN + _PAGE_PREFIX + "index.html"
# A page or an asset past this is refused.
_ASSET_MAX_BYTES = 64 * 1024 * 1024
# What one capture is served, so one page cannot take the shared process:
# the guide's pages hold 2 to 4 elements, a still's collage a dozen photos.
# A name past the first 64 is not asked for, and no file is served once the
# capture was served 256 MB. Two captures run at once, each a whole browser.
_PAGE_MAX_NAMES = 64
_PAGE_MAX_BYTES = 256 * 1024 * 1024
_CAPTURES_AT_ONCE = 2
NAMES_REFUSED = f"The page asked for more than {_PAGE_MAX_NAMES} files, so the rest were not served."
BYTES_REFUSED = (f"The page asked for more than {_PAGE_MAX_BYTES // (1024 * 1024)} MB of files, "
                 "so the rest were not served.")
NO_TURN = (f"the tool renders {_CAPTURES_AT_ONCE} pages at a time, and other pages took all the time "
           "this call could wait, so nothing was rendered. Run the call again.")


@dataclass
class PageDoc:
    """The document a page loads, and the caller's own folder of its
    html_path (None for inline html, whose relative references name
    nothing). ``served`` counts the bytes its capture was served, and
    ``refused`` holds the sentences of the limits it reached."""
    html: bytes
    folder: str | None
    served: int = 0
    refused: list[str] = field(default_factory=list)


_capture_slots: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _capture_slot() -> asyncio.Semaphore:
    """The running loop's share of ``_CAPTURES_AT_ONCE`` browsers."""
    loop = asyncio.get_running_loop()
    slot = _capture_slots.get(loop)
    if slot is None:
        slot = _capture_slots[loop] = asyncio.Semaphore(_CAPTURES_AT_ONCE)
    return slot


@contextlib.asynccontextmanager
async def _capture_turn(deadline: float):
    """A turn among ``_CAPTURES_AT_ONCE`` captures, waited for until
    ``deadline`` (``time.monotonic()``) at most, as the encode's gate is
    waited for within its call's time. A call that gets no turn by then says
    so (``NO_TURN``). A free turn is taken whatever the time."""
    slot = _capture_slot()
    try:
        async with asyncio.timeout(deadline - time.monotonic()):
            await slot.acquire()
    except TimeoutError:
        raise RuntimeError(NO_TURN) from None
    try:
        yield
    finally:
        slot.release()


def _origin_url(raw_path: str) -> str:
    return ORIGIN + urllib.parse.quote(raw_path if raw_path.startswith("/") else "/" + raw_path)


async def _localize_file_urls(doc: str) -> str:
    """Every file:// reference judged now and pointed at the page's origin,
    where the route serves it. Chromium fails a missing sub-resource
    SILENTLY (fonts fall back, images break, the capture "succeeds"), so an
    unresolvable reference is a loud error naming every offender."""
    bad: list[str] = []
    for url in sorted(set(_FILE_REF_RE.findall(doc))):
        try:
            await _resolve_path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        except ValueError:
            bad.append(url)
    if bad:
        shown = ", ".join(bad[:8])
        more = f" (+{len(bad) - 8} more)" if len(bad) > 8 else ""
        raise ValueError(
            f"unresolvable file:// reference(s) in the document: {shown}"
            f"{more}. This tool renders platform-side — reference assets by "
            "workspace path, or inline them as data: URIs.")
    return _FILE_REF_RE.sub(
        lambda m: _origin_url(urllib.parse.unquote(urllib.parse.urlparse(m.group(0)).path)), doc)


async def _prepare_page_doc(html: str | None, html_path: str | None) -> PageDoc:
    """The document a page loads, its file:// references localized: an
    html_path read beneath the mount with no link followed."""
    if html_path:
        src_file = await _resolve_path(html_path)
        if (await asyncio.to_thread(safe_kind, src_file)).kind != "file":
            raise ValueError(f"html file not found: {html_path}")
        raw = await asyncio.to_thread(safe_read_bytes, src_file, _ASSET_MAX_BYTES)
        doc = await _localize_file_urls(raw.decode("utf-8", errors="replace"))
        return PageDoc(doc.encode("utf-8"), posixpath.dirname(html_path))
    return PageDoc((await _localize_file_urls(html or "")).encode("utf-8"), None)


def _page_route(doc: PageDoc):
    """The route every request of a page takes: data, blob and about load;
    the page is the document; a name under the origin is resolved for the
    session (once per name per call) and served from the bytes read beneath
    the mount; everything else is refused. A page's relative and
    root-absolute references, its CSS, fonts, frames, fetches and the URLs
    its scripts build all come through here. What a route cannot see finds
    no network (``_CHROMIUM_ARGS``).

    Playwright runs the route once for each request, each in a task of its
    own. One request of a capture at a time finds its place, reads its file
    and hands it over (``turn``), so the page's two limits are counted before
    the next request reads them, and the tool holds one body at a time."""
    places: dict[str, str | None] = {}
    turn = asyncio.Lock()

    async def place_of(path: str) -> str | None:
        if path not in places:
            if len(places) >= _PAGE_MAX_NAMES:
                if NAMES_REFUSED not in doc.refused:
                    doc.refused.append(NAMES_REFUSED)
                return None
            if path.startswith(_PAGE_PREFIX):
                rel = path[len(_PAGE_PREFIX):]
                target = (posixpath.normpath(posixpath.join(doc.folder or ".", rel))
                          if doc.folder is not None and rel else None)
            else:
                target = path
            container = None
            if target:
                try:
                    container = await _resolve_path(target)
                except ValueError:
                    container = None
            places[path] = container
        return places[path]

    async def route(route):
        url = route.request.url
        if url.startswith(("data:", "about:", "blob:")):
            await route.continue_()
            return
        parts = urllib.parse.urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != ORIGIN:
            await route.abort()
            return
        path = urllib.parse.unquote(parts.path)
        if path == _PAGE_PREFIX + "index.html":
            await route.fulfill(status=200, body=doc.html, content_type="text/html; charset=utf-8")
            return
        async with turn:
            container = await place_of(path)
            if container is None:
                await route.abort()
                return
            room = _PAGE_MAX_BYTES - doc.served
            try:
                body = await asyncio.to_thread(safe_read_bytes, container, min(_ASSET_MAX_BYTES, room))
            except safe_fs.FileTooLarge:
                if room < _ASSET_MAX_BYTES and BYTES_REFUSED not in doc.refused:
                    doc.refused.append(BYTES_REFUSED)
                await route.abort()
                return
            except OSError:
                await route.abort()
                return
            doc.served += len(body)
            await route.fulfill(status=200, body=body,
                                content_type=mimetypes.guess_type(container)[0] or "application/octet-stream")
    return route


_STEP_ANIMATIONS_JS = """
(tMs) => {
  for (const a of document.getAnimations()) {
    try { a.pause(); a.currentTime = tMs; } catch (e) {}
  }
}
"""

# The browser has no network of its own: every connection goes to a proxy on
# a loopback port where nothing listens, loopback addresses included, it
# resolves no name, and WebRTC sends nothing outside that proxy. A request a
# page's route never sees (a worker's socket, a service worker's fetch) fails.
_DEAD_PROXY = "127.0.0.1:9"
_CHROMIUM_ARGS = [
    "--no-sandbox",              # container runs unprivileged; no userns inside
    "--disable-gpu",
    "--disable-dev-shm-usage",   # tiny /dev/shm in containers
    "--force-color-profile=srgb",
    "--hide-scrollbars",
    f"--proxy-server=http://{_DEAD_PROXY}",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]
# The close code and reason a page's refused WebSocket gets.
SOCKET_REFUSED = (1008, "The page has no network.")


async def _refuse_socket(ws) -> None:
    await ws.close(code=SOCKET_REFUSED[0], reason=SOCKET_REFUSED[1])


async def _launch(p):
    """Chromium with no network of its own (``_CHROMIUM_ARGS``)."""
    return await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)


async def _page_context(browser, doc: PageDoc, **options):
    """A context whose pages reach nothing but their route: service workers
    blocked, every request through ``_page_route``, every WebSocket refused."""
    context = await browser.new_context(service_workers="block", **options)
    await context.route("**/*", _page_route(doc))
    await context.route_web_socket(lambda _url: True, _refuse_socket)
    return context


async def _capture_frames(doc: PageDoc, out_dir: Path, width: int,
                          height: int, fps: float, duration: float,
                          transparent: bool, *, deadline: float,
                          report=None) -> tuple[int, int, list[str]]:
    """``(captured, total, failed)``: the frames written to ``out_dir``, how
    many the clip has, and the sub-resources that failed. The turn is waited
    for until ``deadline`` (``time.monotonic()``), and capture stops there
    with the frames it has; ``report(n, total)`` is awaited every few
    frames."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed in this image")

    total = int(round(duration * fps))
    frame_ms = 1000.0 / fps
    failed: list[str] = []
    async with _capture_turn(deadline), async_playwright() as p:
        try:
            browser = await _launch(p)
        except Exception as exc:
            raise RuntimeError(
                f"Chromium is not available for motion rendering: {exc}")
        try:
            context = await _page_context(
                browser, doc,
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page = await context.new_page()
            page.on("requestfailed",
                    lambda r: failed.append(r.url) if len(failed) < 32 else None)
            # Fake clock BEFORE any page script runs, so Date/timers/rAF are
            # deterministic from the first line.
            clock_ok = True
            try:
                await page.clock.install(time=0)
            except Exception as exc:
                clock_ok = False
                logger.warning(f"clock install failed ({exc}) — JS-driven "
                               "animation will not be stepped; CSS/WAAPI still is")
            # Export the capture anchor before any page script runs, so
            # load-time scene code can already read it (see SETTLE_SECONDS).
            await page.add_init_script(
                f"window.VT_T0 = {SETTLE_SECONDS * 1000:.0f};")
            await page.goto(PAGE_URL, wait_until="load")
            try:
                await asyncio.wait_for(page.evaluate(_FONTS_READY_JS), _FONTS_WAIT_S)
            except Exception:
                pass
            if clock_ok:
                try:
                    await page.clock.pause_at(SETTLE_SECONDS)
                except Exception:
                    clock_ok = False
            if not clock_ok:
                # Real clock: Date.now() is epoch-scale, so the constant
                # anchor would be garbage — re-anchor at capture start.
                try:
                    await page.evaluate("window.VT_T0 = Date.now()")
                except Exception:
                    pass
            try:
                await page.evaluate(
                    "() => { document.documentElement.classList.add('vt-capture');"
                    " window.dispatchEvent(new CustomEvent('vt:capture-start',"
                    " {detail: {t0: window.VT_T0}})); }")
            except Exception:
                pass

            # run_for takes integer milliseconds — accumulate against the
            # exact frame time so rounding never drifts more than 1ms. Exact
            # sub-ms timing comes from the getAnimations() seek anyway.
            ticks_done = 0
            captured = 0
            for n in range(total):
                if time.monotonic() > deadline:
                    break
                if report is not None and n % _REPORT_EVERY == 0:
                    await report(n, total)
                t_ms = n * frame_ms
                if clock_ok and n > 0:
                    target = round(t_ms)
                    if target > ticks_done:
                        await page.clock.run_for(int(target - ticks_done))
                        ticks_done = target
                await page.evaluate(_STEP_ANIMATIONS_JS, t_ms)
                await page.screenshot(
                    path=str(out_dir / f"f{n:05d}.png"),
                    omit_background=transparent,
                )
                captured = n + 1
        finally:
            await browser.close()
    return captured, total, failed


async def _encode(frames_dir: Path, fps: float, fmt: str, transparent: bool,
                  background: str, width: int, height: int, out: str,
                  timeout: float, landing=None) -> None:
    pattern = str(frames_dir / "f%05d.png")
    base = ["-framerate", f"{fps:.6g}", "-i", pattern]
    if fmt == "mp4":
        if transparent:
            # Flatten alpha over the background color.
            graph = (f"color=c={ff_color(background)}:s={width}x{height}"
                     f":r={fps:.6g}[bg];[bg][0:v]overlay=shortest=1,"
                     f"format=yuv420p[v]")
            args = base + ["-filter_complex", graph, "-map", "[v]"]
        else:
            args = base + ["-vf", "format=yuv420p"]
        args += ["-c:v", "libx264", "-preset", "slow", "-crf", "18",
                 "-movflags", "+faststart", out]
    elif fmt == "webm":
        pix = "yuva420p" if transparent else "yuv420p"
        args = base + ["-c:v", "libvpx-vp9", "-pix_fmt", pix,
                       "-b:v", "0", "-crf", "24", "-row-mt", "1", out]
    elif fmt == "gif":
        args = base + [
            "-filter_complex",
            "[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[v]",
            "-map", "[v]", out]
    elif fmt == "webp":
        args = base + ["-c:v", "libwebp", "-q:v", "85", "-loop", "0", out]
    else:
        raise ValueError(f"unknown format '{fmt}'")
    await run_ffmpeg(args, timeout=timeout, landing=landing)


async def _capture_still(doc: PageDoc, width: int, height: int, at: float,
                         transparent: bool, scale_factor: float, *, deadline: float) -> bytes:
    """The page pictured at ``at`` seconds. The turn and the picture end by
    ``deadline`` (``time.monotonic()``): ``NO_TURN`` when no turn came by
    then, ``STILL_TOO_LONG`` when the page was not pictured by then."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed in this image")

    async with _capture_turn(deadline), async_playwright() as p:
        try:
            browser = await _launch(p)
        except Exception as exc:
            raise RuntimeError(f"Chromium is not available: {exc}")
        try:
            async with asyncio.timeout(deadline - time.monotonic()):
                context = await _page_context(
                    browser, doc,
                    viewport={"width": width, "height": height},
                    device_scale_factor=scale_factor,
                )
                page = await context.new_page()
                try:
                    await page.clock.install(time=0)
                except Exception:
                    pass
                await page.goto(PAGE_URL, wait_until="load")
                try:
                    await asyncio.wait_for(page.evaluate(_FONTS_READY_JS), _FONTS_WAIT_S)
                except Exception:
                    pass
                # Freeze any animation at the requested moment.
                await page.evaluate(_STEP_ANIMATIONS_JS, at * 1000.0)
                return await page.screenshot(omit_background=transparent, type="png")
        except TimeoutError:
            raise RuntimeError(STILL_TOO_LONG) from None
        finally:
            await browser.close()


# The share of a call's time a motion clip keeps for its encode: capture
# stops when the rest is spent, and the frames it has are encoded.
_ENCODE_SHARE = 0.2
# What a motion clip keeps of its call after the encode, for its answer: the
# probe of the file (30 s at most), the platform's notice of it (30 s) and
# room to spare. The encode ends before this margin starts.
_ANSWER_MARGIN_S = 90.0
OUT_OF_TIME = ("Error: the call's time ran out while the clip was encoded, so nothing was "
               "written. Render a shorter or smaller clip, or one with fewer frames a second.")
# A still's turn and picture end within this, well inside its call's ceiling.
_STILL_MAX_S = 300.0
STILL_TOO_LONG = (f"the page was not pictured within {int(_STILL_MAX_S // 60)} minutes, so nothing was "
                  "written. Make the page lighter, or run the call again.")
# The page's own promise of its fonts is waited for this long at most.
_FONTS_READY_JS = "document.fonts && document.fonts.ready"
_FONTS_WAIT_S = 10.0
# A progress note every this many frames, when the call asks for progress.
_REPORT_EVERY = 30
_DEFAULT_CALL_S = 3600.0


def call_budget_s() -> float:
    """The call's ceiling: the manifest's ``server.tool_timeout_sec``, which
    the platform gives the engine (the image carries the manifest)."""
    try:
        manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
        return float(manifest["server"]["tool_timeout_sec"])
    except (OSError, ValueError, KeyError, TypeError):
        return _DEFAULT_CALL_S


def _progress():
    """A progress reporter for this call, or None when it asked for none:
    MCP progress notifications on the request's own stream."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
    except LookupError:
        return None
    token = getattr(ctx.meta, "progressToken", None) if ctx.meta is not None else None
    if token is None:
        return None

    async def report(done: int, total: int) -> None:
        try:
            await ctx.session.send_progress_notification(
                token, done, total, message=f"captured {done} of {total} frames",
                related_request_id=ctx.request_id)
        except Exception:  # noqa: BLE001  # progress is a courtesy; the capture goes on
            logger.debug("progress note not sent", exc_info=True)
    return report


async def handle_render_still(args: dict):
    """Single-frame HTML/CSS → PNG/JPEG: the thumbnail / social-card /
    collage engine (same deterministic renderer as motion clips)."""
    html = args.get("html")
    html_path = args.get("html_path")
    if bool(html) == bool(html_path):
        return "Error: pass exactly one of html (inline) or html_path"
    out_arg = args.get("output_path")
    if not out_arg:
        return "Error: output_path is required"

    width = int(args.get("width", 1920))
    height = int(args.get("height", 1080))
    at = float(args.get("at", 0.0))
    transparent = bool(args.get("transparent", False))
    fmt = str(args.get("format", "png")).lower()
    quality = int(args.get("quality", 92))
    scale_factor = float(args.get("scale", 1.0))
    if fmt not in ("png", "jpeg", "jpg"):
        return "Error: format must be png or jpeg"
    fmt = "jpeg" if fmt in ("jpeg", "jpg") else "png"
    if not (16 <= width <= MAX_DIM and 16 <= height <= MAX_DIM):
        return f"Error: width/height must be 16–{MAX_DIM}"
    if not 1.0 <= scale_factor <= 3.0:
        return "Error: scale must be 1–3 (device pixel ratio for crisp output)"
    if not 0.0 <= at <= 300.0:
        return "Error: 'at' must be 0–300 seconds"
    if transparent and fmt == "jpeg":
        return "Error: jpeg cannot carry transparency — use png"

    started = time.monotonic()
    out = await _resolve_path(out_arg, writing=True)
    ext = ".png" if fmt == "png" else ".jpg"
    if Path(out).suffix.lower() not in (ext, ".jpeg" if fmt == "jpeg" else ext):
        # Another name is another place: judged again.
        out = await _resolve_path(str(Path(out).with_suffix(ext)), writing=True)

    try:
        doc = await _prepare_page_doc(html, html_path)
        png = await _capture_still(doc, width, height, at, transparent, scale_factor,
                                   deadline=started + _STILL_MAX_S)
    except (RuntimeError, ValueError) as exc:
        return f"Error: {exc}"

    if fmt == "jpeg":
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.open(io.BytesIO(png)).convert("RGB").save(buf, format="JPEG", quality=quality, subsampling=0)
        push_bytes, push_mime = buf.getvalue(), "image/jpeg"
    else:
        push_bytes, push_mime = png, "image/png"
    await asyncio.to_thread(safe_write_bytes, out, push_bytes)

    await _notify_file_written(out)
    from shared import _push_image_preview
    await _push_image_preview(push_bytes, push_mime,
                              f"Rendered: {Path(out).name}")

    px_w, px_h = int(width * scale_factor), int(height * scale_factor)
    size = len(push_bytes) / 1e6
    return "\n".join([f"Rendered still: {_to_agents_relative(out)}",
                      f"{px_w}x{px_h}px ({width}x{height} @ {scale_factor:g}x) · "
                      f"{fmt} · {size:.2f} MB — shown inline to the user already.", *doc.refused])


async def handle_render_motion_clip(args: dict):
    html = args.get("html")
    html_path = args.get("html_path")
    if bool(html) == bool(html_path):
        return "Error: pass exactly one of html (inline) or html_path"

    width = int(args.get("width", 1920))
    height = int(args.get("height", 1080))
    fps = float(args.get("fps", 30))
    duration = float(args.get("duration", 0))
    transparent = bool(args.get("transparent", False))
    background = args.get("background", "#000000")
    try:
        ff_color(background)
    except ValueError:
        return "Error: background must be a colour, such as #000000 or black"
    out_arg = args.get("output_path")
    if not out_arg:
        return "Error: output_path is required"
    fmt = args.get("format") or ("webm" if transparent else "mp4")
    if fmt not in ("mp4", "webm", "gif", "webp"):
        return "Error: format must be mp4, webm, gif, or webp"

    if not 0.2 <= duration <= MAX_DURATION:
        return f"Error: duration must be 0.2–{MAX_DURATION:.0f}s"
    if not (16 <= width <= MAX_DIM and 16 <= height <= MAX_DIM):
        return f"Error: width/height must be 16–{MAX_DIM}"
    if not 1 <= fps <= MAX_FPS:
        return f"Error: fps must be 1–{MAX_FPS}"
    if width % 2 or height % 2:
        return "Error: width/height must be even"

    started = time.monotonic()
    # The time the call may use: its encode ends with this, and the answer
    # follows within the margin.
    usable = call_budget_s() - _ANSWER_MARGIN_S
    out = await _resolve_path(out_arg, writing=True)
    ext = {"mp4": ".mp4", "webm": ".webm", "gif": ".gif", "webp": ".webp"}[fmt]
    if Path(out).suffix.lower() != ext:
        # Another name is another place: judged again.
        out = await _resolve_path(str(Path(out).with_suffix(ext)), writing=True)

    tmp = Path(tempfile.mkdtemp(prefix="vt-motion-"))
    try:
        doc = await _prepare_page_doc(html, html_path)
        frames_dir = tmp / "frames"
        frames_dir.mkdir()
        captured, total, failed = await _capture_frames(
            doc, frames_dir, width, height, fps, duration, transparent,
            deadline=started + usable * (1.0 - _ENCODE_SHARE), report=_progress())
        if captured == 0:
            return "Error: the call's time ran out before the first frame was captured"
        left = started + usable - time.monotonic()
        if left <= 0:
            return OUT_OF_TIME
        # One limit over the wait at the gate and the run: a cancelled
        # encode ends its child, and its output is not written. The file
        # lands in the step that lifts the limit, so a call whose file
        # landed goes on to its answer.
        async with asyncio.timeout(left) as limit:
            await _encode(frames_dir, fps, fmt, transparent, background, width, height, out,
                          timeout=left, landing=lambda: limit.reschedule(None))
    except (FFmpegTimeout, asyncio.TimeoutError):
        return OUT_OF_TIME
    except (RuntimeError, ValueError, FFmpegError) as exc:
        return f"Error: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    await _notify_file_written(out)
    try:
        size = await asyncio.to_thread(landed_size_mb, out)
        info = await probe(out)
        dur_note = f"{media_duration(info):.2f}s"
    except FileNotFoundError:
        return "Error: " + LANDED_GONE.format(name=Path(out).name)
    except FFmpegError:
        dur_note = f"~{duration:.2f}s"
    lines = [
        f"Rendered motion clip: {_to_agents_relative(out)}",
        f"{width}x{height} @ {fps:.3g} fps · {dur_note} · {captured} frames · "
        f"{size:.2f} MB · {fmt}"
        + (" (alpha)" if transparent and fmt == "webm" else ""),
    ]
    if captured < total:
        lines.append(f"The clip covers 0 to {captured / fps:.2f} s of {duration:.2f} s: the call's time "
                     "ran out. Render the rest as a second clip, or a shorter or slower one.")
    if transparent and fmt == "webm":
        lines.append("Use it as a composition overlay clip (src) — alpha is "
                     "preserved when compositing.")
    elif fmt == "mp4":
        lines.append("Usable as a base/overlay clip or standalone — show the "
                     "user with display_video.")
    if failed:
        shown = ", ".join(failed[:6])
        more = f" (+{len(failed) - 6} more)" if len(failed) > 6 else ""
        lines.append(
            f"WARNING: {len(failed)} sub-resource(s) failed to load: {shown}"
            f"{more}. The page reaches no network, so inline anything from the web. "
            "A file the page names loads only when the session may read it.")
    lines += doc.refused
    return "\n".join(lines)
