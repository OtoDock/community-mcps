"""render_motion_clip execution tests — need ffmpeg AND a Playwright
chromium; skipped when either is missing."""

import asyncio

import pytest

from conftest import HAVE_FFMPEG


def _have_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return bool(p.chromium.executable_path)
    except Exception:
        return False


HAVE_CHROMIUM = _have_chromium()
pytestmark = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_CHROMIUM),
    reason="ffmpeg or playwright chromium not available")

import motion  # noqa: E402
from fftools import media_duration, probe, video_stream  # noqa: E402

_HTML = """<!doctype html><html><head><style>
  body { margin: 0; }
  .box { position: absolute; top: 90px; left: 0; width: 60px; height: 60px;
         background: #14a096; border-radius: 12px;
         animation: slide 1s linear forwards; }
  @keyframes slide { from { left: 0; } to { left: 260px; } }
  .title { position: absolute; top: 20px; left: 20px; color: white;
           font: bold 28px sans-serif; }
</style></head><body>
  <div class="title">OtoDock</div>
  <div class="box"></div>
</body></html>"""


def _run(coro):
    return asyncio.run(coro)


def test_motion_mp4_render(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", lambda p: p)
    out = str(tmp_path / "clip.mp4")
    html = _HTML.replace("<body>", "<body style=\"background:#101014\">")
    text = _run(motion.handle_render_motion_clip({
        "html": html, "width": 320, "height": 240,
        "fps": 10, "duration": 1.0, "output_path": out,
    }))
    assert not text.startswith("Error"), text
    info = _run(probe(out))
    vs = video_stream(info)
    assert vs["codec_name"] == "h264"
    assert (vs["width"], vs["height"]) == (320, 240)
    assert abs(media_duration(info) - 1.0) < 0.15


def test_motion_transparent_webm(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", lambda p: p)
    out = str(tmp_path / "sting.webm")
    text = _run(motion.handle_render_motion_clip({
        "html": _HTML, "width": 320, "height": 240,
        "fps": 10, "duration": 0.6, "transparent": True,
        "output_path": out,
    }))
    assert not text.startswith("Error"), text
    assert "alpha" in text
    info = _run(probe(out))
    assert video_stream(info)["codec_name"] == "vp9"


def test_motion_determinism(tmp_path, monkeypatch):
    """Two renders of the same animation are byte-comparable at the frame
    level: compare a mid-animation frame across renders."""
    monkeypatch.setattr(motion, "_resolve_path", lambda p: p)
    outs = []
    for i in range(2):
        out = str(tmp_path / f"det{i}.mp4")
        html = _HTML.replace("<body>", "<body style=\"background:#101014\">")
        _run(motion.handle_render_motion_clip({
            "html": html, "width": 320, "height": 240,
            "fps": 10, "duration": 1.0, "output_path": out,
        }))
        outs.append(out)
    import subprocess
    from fftools import FFMPEG
    digests = []
    for out in outs:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", out,
             "-map", "0:v", "-f", "framemd5", "-"],
            capture_output=True, text=True, timeout=120)
        digests.append(proc.stdout)
    assert digests[0] == digests[1]


def test_motion_rejects_bad_args(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", lambda p: p)
    both = _run(motion.handle_render_motion_clip({
        "html": "<html/>", "html_path": "x.html",
        "duration": 1, "output_path": str(tmp_path / "x.mp4")}))
    assert both.startswith("Error")
    too_long = _run(motion.handle_render_motion_clip({
        "html": "<html/>", "duration": 999,
        "output_path": str(tmp_path / "x.mp4")}))
    assert "duration" in too_long
