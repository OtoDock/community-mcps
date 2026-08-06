"""render_motion_clip: agent-authored HTML/CSS → video via headless Chromium.

Deterministic frame stepping: Playwright's Clock API fakes Date/timers/rAF
(drives GSAP-style JS animation), and every CSS/WAAPI animation is seeked
explicitly per frame via document.getAnimations() — so frame n is always
exactly t = n/fps regardless of render speed. Screenshots (optionally with
transparent background) are assembled by ffmpeg into MP4, alpha WebM
(usable directly as a composition overlay), GIF, or animated WebP.

Security posture: fresh throwaway context per render; all http(s) requests
are ABORTED (agent-authored HTML must not reach the network — pool safety,
no SSRF); only file:// and data: resources load. file:// references are
localized through _resolve_path before load (the tool runs PLATFORM-side —
satellite/host paths don't exist here) and unresolvable ones are a hard
preflight error, never a silently broken frame. Frame/dimension caps bound
runaway renders.
"""

import re
import shutil
import tempfile
import urllib.parse
from pathlib import Path

from fftools import FFmpegError, ff_color, media_duration, probe, run_ffmpeg
from shared import _notify_file_written, _resolve_path, _to_agents_relative, logger

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


def _localize_file_urls(doc: str) -> tuple[str, list[str]]:
    """Rewrite every file:// reference in a document to its container-local
    path via _resolve_path. Chromium fails an unknown file:// sub-resource
    SILENTLY (fonts fall back, images break, the capture "succeeds"), so
    the unresolvable list must be treated as a hard error by the caller."""
    bad: list[str] = []

    def _sub(m: re.Match) -> str:
        url = m.group(0)
        raw = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        try:
            resolved = _resolve_path(raw)
        except ValueError:
            bad.append(url)
            return url
        return "file://" + urllib.parse.quote(resolved)

    return _FILE_REF_RE.sub(_sub, doc), bad


def _prepare_page_doc(html: str | None, html_path: str | None,
                      tmp: Path, name: str) -> str:
    """Materialize the document Chromium loads, with file:// refs localized.

    An html_path doc with NO file:// refs loads in place, so its relative
    refs keep resolving against its own directory (they work today — the
    html file itself is inside the mount). When rewriting forces a tmp
    copy, an injected <base href> to the original directory preserves those
    relative refs. Unresolvable file:// refs raise ValueError naming every
    offender — the loud preflight this tool historically lacked."""

    def _fail(bad: list[str]):
        shown = ", ".join(bad[:8])
        more = f" (+{len(bad) - 8} more)" if len(bad) > 8 else ""
        raise ValueError(
            f"unresolvable file:// reference(s) in the document: {shown}"
            f"{more}. This tool renders platform-side — reference assets by "
            "workspace path, or inline them as data: URIs.")

    if html_path:
        src_file = _resolve_path(html_path)
        if not Path(src_file).exists():
            raise ValueError(f"html file not found: {html_path}")
        doc = Path(src_file).read_text(encoding="utf-8")
        rewritten, bad = _localize_file_urls(doc)
        if bad:
            _fail(bad)
        if rewritten == doc:
            return src_file
        base_tag = f'<base href="{Path(src_file).parent.as_uri()}/">'
        low = rewritten.lower()
        i = low.find("<head")
        if i != -1:
            j = rewritten.find(">", i)
            rewritten = rewritten[:j + 1] + base_tag + rewritten[j + 1:]
        else:
            rewritten = base_tag + rewritten
        out = tmp / name
        out.write_text(rewritten, encoding="utf-8")
        return str(out)

    rewritten, bad = _localize_file_urls(html or "")
    if bad:
        _fail(bad)
    out = tmp / name
    out.write_text(rewritten, encoding="utf-8")
    return str(out)

_STEP_ANIMATIONS_JS = """
(tMs) => {
  for (const a of document.getAnimations()) {
    try { a.pause(); a.currentTime = tMs; } catch (e) {}
  }
}
"""

_CHROMIUM_ARGS = [
    "--no-sandbox",              # container runs unprivileged; no userns inside
    "--disable-gpu",
    "--disable-dev-shm-usage",   # tiny /dev/shm in containers
    "--force-color-profile=srgb",
    "--hide-scrollbars",
]


async def _route_block_network(route):
    url = route.request.url
    if url.startswith(("file://", "data:", "about:", "blob:")):
        await route.continue_()
    else:
        await route.abort()


async def _capture_frames(html_file: str, out_dir: Path, width: int,
                          height: int, fps: float, duration: float,
                          transparent: bool) -> tuple[int, list[str]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed in this image")

    total = int(round(duration * fps))
    frame_ms = 1000.0 / fps
    failed: list[str] = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        except Exception as exc:
            raise RuntimeError(
                f"Chromium is not available for motion rendering: {exc}")
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            await context.route("**/*", _route_block_network)
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
            await page.goto(Path(html_file).as_uri(), wait_until="load")
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
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
            for n in range(total):
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
        finally:
            await browser.close()
    return total, failed


async def _encode(frames_dir: Path, fps: float, fmt: str, transparent: bool,
                  background: str, width: int, height: int, out: str) -> None:
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
    await run_ffmpeg(args, timeout=1200)


async def _capture_still(html_file: str, width: int, height: int, at: float,
                         transparent: bool, scale_factor: float) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed in this image")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        except Exception as exc:
            raise RuntimeError(f"Chromium is not available: {exc}")
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale_factor,
            )
            await context.route("**/*", _route_block_network)
            page = await context.new_page()
            try:
                await page.clock.install(time=0)
            except Exception:
                pass
            await page.goto(Path(html_file).as_uri(), wait_until="load")
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            # Freeze any animation at the requested moment.
            await page.evaluate(_STEP_ANIMATIONS_JS, at * 1000.0)
            return await page.screenshot(omit_background=transparent, type="png")
        finally:
            await browser.close()


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

    out = _resolve_path(out_arg)
    ext = ".png" if fmt == "png" else ".jpg"
    if Path(out).suffix.lower() not in (ext, ".jpeg" if fmt == "jpeg" else ext):
        out = str(Path(out).with_suffix(ext))

    tmp = Path(tempfile.mkdtemp(prefix="vt-still-"))
    try:
        html_file = _prepare_page_doc(html, html_path, tmp, "still.html")
        png = await _capture_still(html_file, width, height, at,
                                   transparent, scale_factor)
    except (RuntimeError, ValueError) as exc:
        return f"Error: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jpeg":
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        img.save(out, format="JPEG", quality=quality, subsampling=0)
        push_bytes, push_mime = Path(out).read_bytes(), "image/jpeg"
    else:
        Path(out).write_bytes(png)
        push_bytes, push_mime = png, "image/png"

    await _notify_file_written(out)
    from shared import _push_image_preview
    await _push_image_preview(push_bytes, push_mime,
                              f"Rendered: {Path(out).name}")

    px_w, px_h = int(width * scale_factor), int(height * scale_factor)
    size = Path(out).stat().st_size / 1e6
    return (f"Rendered still: {_to_agents_relative(out)}\n"
            f"{px_w}x{px_h}px ({width}x{height} @ {scale_factor:g}x) · "
            f"{fmt} · {size:.2f} MB — shown inline to the user already.")


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

    out = _resolve_path(out_arg)
    ext = {"mp4": ".mp4", "webm": ".webm", "gif": ".gif", "webp": ".webp"}[fmt]
    if Path(out).suffix.lower() != ext:
        out = str(Path(out).with_suffix(ext))

    tmp = Path(tempfile.mkdtemp(prefix="vt-motion-"))
    try:
        html_file = _prepare_page_doc(html, html_path, tmp, "motion.html")

        frames_dir = tmp / "frames"
        frames_dir.mkdir()
        total, failed = await _capture_frames(html_file, frames_dir, width,
                                              height, fps, duration,
                                              transparent)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        await _encode(frames_dir, fps, fmt, transparent, background,
                      width, height, out)
    except (RuntimeError, ValueError, FFmpegError) as exc:
        return f"Error: {exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    await _notify_file_written(out)
    try:
        info = await probe(out)
        dur_note = f"{media_duration(info):.2f}s"
    except FFmpegError:
        dur_note = f"~{duration:.2f}s"
    size = Path(out).stat().st_size / 1e6
    lines = [
        f"Rendered motion clip: {_to_agents_relative(out)}",
        f"{width}x{height} @ {fps:.3g} fps · {dur_note} · {total} frames · "
        f"{size:.2f} MB · {fmt}"
        + (" (alpha)" if transparent and fmt == "webm" else ""),
    ]
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
            f"{more}. http(s) is blocked by design — inline remote assets; "
            "file:// failures mean a missing file at the resolved path.")
    return "\n".join(lines)
