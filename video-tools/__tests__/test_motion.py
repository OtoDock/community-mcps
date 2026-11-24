"""render_motion_clip execution tests — need ffmpeg AND a Playwright
chromium; skipped when either is missing."""

import asyncio

import pytest

from conftest import HAVE_FFMPEG, same_path


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
    monkeypatch.setattr(motion, "_resolve_path", same_path)
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
    monkeypatch.setattr(motion, "_resolve_path", same_path)
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
    monkeypatch.setattr(motion, "_resolve_path", same_path)
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
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    both = _run(motion.handle_render_motion_clip({
        "html": "<html/>", "html_path": "x.html",
        "duration": 1, "output_path": str(tmp_path / "x.mp4")}))
    assert both.startswith("Error")
    too_long = _run(motion.handle_render_motion_clip({
        "html": "<html/>", "duration": 999,
        "output_path": str(tmp_path / "x.mp4")}))
    assert "duration" in too_long


def test_motion_vt_t0_anchor(tmp_path, monkeypatch):
    """The capture-anchor contract: window.VT_T0 is injected before page
    scripts run, equals the settle offset (1000 ms), Date.now() at capture
    start equals it, and the vt:capture-start event fires with the same t0.
    The page turns red only when every condition holds."""
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    out = str(tmp_path / "anchor.mp4")
    html = """<!doctype html><html><head><style>
      body { margin: 0; background: #000; }
    </style></head><body><script>
      const early = window.VT_T0;   // must exist before any page script
      window.addEventListener('vt:capture-start', (e) => {
        if (early === 1000 && e.detail.t0 === 1000
            && Math.abs(Date.now() - window.VT_T0) < 1)
          document.body.style.background = '#ff0000';
      });
    </script></body></html>"""
    text = _run(motion.handle_render_motion_clip({
        "html": html, "width": 64, "height": 64,
        "fps": 10, "duration": 0.4, "output_path": out,
    }))
    assert not text.startswith("Error"), text

    import subprocess
    from fftools import FFMPEG
    png = tmp_path / "anchor.png"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-i", out, "-frames:v", "1", str(png)],
        check=True, timeout=120)
    from PIL import Image
    r, g, b = Image.open(png).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert r > 180 and g < 60 and b < 60, (r, g, b)


def test_motion_reports_failed_subresources(tmp_path, monkeypatch):
    """A file:// ref that RESOLVES but points at a missing file renders,
    but the result must carry the sub-resource warning (silent breakage
    was the original bug's cost)."""
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    out = str(tmp_path / "warn.mp4")
    missing = tmp_path / "missing-asset.png"
    html = (f'<!doctype html><html><body style="background:#101014">'
            f'<img src="{missing.as_uri()}"></body></html>')
    text = _run(motion.handle_render_motion_clip({
        "html": html, "width": 64, "height": 64,
        "fps": 10, "duration": 0.3, "output_path": out,
    }))
    assert not text.startswith("Error"), text
    assert "WARNING" in text and "missing-asset.png" in text


class _Clock:
    """``time.monotonic`` of motion: one second passes at every reading."""

    def __init__(self):
        self.now = -1.0

    def monotonic(self):
        self.now += 1.0
        return self.now


def test_a_clip_the_call_has_no_time_for_keeps_what_it_captured(tmp_path, monkeypatch):
    """Capture stops when the call's time is spent (less the share kept for
    the encode); the frames it has are encoded, and the answer says what
    the clip covers."""
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    clock = _Clock()
    monkeypatch.setattr(motion, "time", clock)
    # The call starts at 0 s; a 10 s call with no answer margin keeps half
    # for the encode, so capture stops at 5 s. The turn is taken at 1 s,
    # frames 0-3 are read at 2-5 s, frame 4 at 6 s is past it, and the encode
    # starts at 7 s with 3 s left.
    monkeypatch.setattr(motion, "call_budget_s", lambda: 10.0)
    monkeypatch.setattr(motion, "_ANSWER_MARGIN_S", 0.0)
    monkeypatch.setattr(motion, "_ENCODE_SHARE", 0.5)
    out = str(tmp_path / "cut.mp4")
    text = _run(motion.handle_render_motion_clip({
        "html": _HTML, "width": 64, "height": 64,
        "fps": 10, "duration": 1.0, "output_path": out,
    }))
    assert not text.startswith("Error"), text
    assert " · 4 frames · " in text
    assert ("The clip covers 0 to 0.40 s of 1.00 s: the call's time ran out. "
            "Render the rest as a second clip, or a shorter or slower one.") in text
    assert abs(media_duration(_run(probe(out))) - 0.4) < 0.15


def test_a_clip_reports_its_frames_as_progress_when_the_call_asks(tmp_path, monkeypatch):
    """A request that carries a progress token hears how many frames are
    captured of the total, on its own request."""
    from mcp import types
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    monkeypatch.setattr(motion, "_REPORT_EVERY", 3)
    notes: list = []

    class _Session:
        async def send_progress_notification(self, token, progress, total=None, message=None,
                                             related_request_id=None):
            notes.append((token, progress, total, message, related_request_id))

    async def call():
        request_ctx.set(RequestContext(request_id=7, meta=types.RequestParams.Meta(progressToken="tok"),
                                       session=_Session(), lifespan_context=None))
        return await motion.handle_render_motion_clip({
            "html": _HTML, "width": 64, "height": 64,
            "fps": 10, "duration": 1.0, "output_path": str(tmp_path / "p.mp4"),
        })
    text = _run(call())
    assert not text.startswith("Error"), text
    assert notes == [("tok", n, 10, f"captured {n} of 10 frames", 7) for n in (0, 3, 6, 9)]
