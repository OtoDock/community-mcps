"""render_motion_clip: agent-authored HTML/CSS → video via headless Chromium.

Deterministic frame stepping: Playwright's Clock API fakes Date/timers/rAF
(drives GSAP-style JS animation), and every CSS/WAAPI animation is seeked
explicitly per frame via document.getAnimations() — so frame n is always
exactly t = n/fps regardless of render speed. Screenshots (optionally with
transparent background) are assembled by ffmpeg into MP4, alpha WebM
(usable directly as a composition overlay), GIF, or animated WebP.

Security posture: fresh throwaway context per render; all http(s) requests
are ABORTED (agent-authored HTML must not reach the network — pool safety,
no SSRF); only file:// and data: resources load. Frame/dimension caps bound
runaway renders.
"""

import shutil
import tempfile
from pathlib import Path

from fftools import FFmpegError, ff_color, media_duration, probe, run_ffmpeg
from shared import _notify_file_written, _resolve_path, _to_agents_relative, logger

MAX_DURATION = 120.0
MAX_FPS = 60
MAX_DIM = 4096

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
                          transparent: bool) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed in this image")

    total = int(round(duration * fps))
    frame_ms = 1000.0 / fps
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
            # Fake clock BEFORE any page script runs, so Date/timers/rAF are
            # deterministic from the first line.
            clock_ok = True
            try:
                await page.clock.install(time=0)
            except Exception as exc:
                clock_ok = False
                logger.warning(f"clock install failed ({exc}) — JS-driven "
                               "animation will not be stepped; CSS/WAAPI still is")
            await page.goto(Path(html_file).as_uri(), wait_until="load")
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            if clock_ok:
                try:
                    await page.clock.pause_at(1)
                except Exception:
                    clock_ok = False

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
    return total


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
        if html_path:
            html_file = _resolve_path(html_path)
            if not Path(html_file).exists():
                return f"Error: html file not found: {html_path}"
        else:
            html_file = str(tmp / "still.html")
            Path(html_file).write_text(html, encoding="utf-8")
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
        if html_path:
            html_file = _resolve_path(html_path)
            if not Path(html_file).exists():
                return f"Error: html file not found: {html_path}"
        else:
            html_file = str(tmp / "motion.html")
            Path(html_file).write_text(html, encoding="utf-8")

        frames_dir = tmp / "frames"
        frames_dir.mkdir()
        total = await _capture_frames(html_file, frames_dir, width, height,
                                      fps, duration, transparent)
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
    return "\n".join(lines)
