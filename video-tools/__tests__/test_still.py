"""render_still: single-frame HTML rendering (thumbnails/cards/collages)."""

import asyncio

import pytest

from conftest import HAVE_FFMPEG, same_path
from test_motion import HAVE_CHROMIUM

pytestmark = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_CHROMIUM),
    reason="ffmpeg or playwright chromium not available")

import motion  # noqa: E402

_CARD = """<!doctype html><html><head><style>
  body { margin: 0; width: 320px; height: 180px;
         background: linear-gradient(135deg, #101014 0%, #14343f 100%);
         font-family: sans-serif; position: relative; overflow: hidden; }
  h1 { position: absolute; left: 18px; bottom: 34px; margin: 0;
       color: #fff; font-size: 34px; letter-spacing: -1px; }
  .tag { position: absolute; left: 18px; bottom: 12px; color: #ffd400;
         font-size: 12px; font-weight: 700; }
  .chip { position: absolute; right: -30px; top: -30px; width: 120px;
          height: 120px; border-radius: 50%;
          background: rgba(20,160,150,.5); }
</style></head><body>
  <div class="chip"></div>
  <h1>OtoDock</h1>
  <div class="tag">SELF-HOSTED AGENTS</div>
</body></html>"""


def _run(coro):
    return asyncio.run(coro)


def test_still_png_dimensions_and_scale(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    from PIL import Image

    out1 = str(tmp_path / "card.png")
    text = _run(motion.handle_render_still({
        "html": _CARD, "width": 320, "height": 180, "output_path": out1}))
    assert not text.startswith("Error"), text
    assert Image.open(out1).size == (320, 180)

    out2 = str(tmp_path / "card2x.png")
    _run(motion.handle_render_still({
        "html": _CARD, "width": 320, "height": 180, "scale": 2,
        "output_path": out2}))
    assert Image.open(out2).size == (640, 360)


def test_still_transparent_has_alpha(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    import numpy as np
    from PIL import Image

    html = """<!doctype html><html><body style="margin:0">
      <div style="width:100px;height:100px;background:#14a096;
                  border-radius:20px;margin:40px"></div>
    </body></html>"""
    out = str(tmp_path / "badge.png")
    text = _run(motion.handle_render_still({
        "html": html, "width": 200, "height": 200, "transparent": True,
        "output_path": out}))
    assert not text.startswith("Error"), text
    arr = np.asarray(Image.open(out).convert("RGBA"))
    assert (arr[..., 3] == 0).any()      # transparent background
    assert (arr[..., 3] == 255).any()    # opaque subject


def test_still_jpeg_and_bad_args(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "_resolve_path", same_path)
    from PIL import Image

    out = str(tmp_path / "card.jpg")
    text = _run(motion.handle_render_still({
        "html": _CARD, "width": 320, "height": 180,
        "format": "jpeg", "output_path": out}))
    assert not text.startswith("Error"), text
    assert Image.open(out).format == "JPEG"

    bad = _run(motion.handle_render_still({
        "html": _CARD, "transparent": True, "format": "jpeg",
        "output_path": str(tmp_path / "x.jpg")}))
    assert bad.startswith("Error")


def test_a_page_whose_fonts_never_settle_is_pictured_after_the_limit(tmp_path, monkeypatch):
    """The page's own promise of its fonts is waited for within its limit:
    a page that never settles it is pictured once the limit is spent, and a
    still that is not done within its ceiling says so and writes nothing."""
    import time

    monkeypatch.setattr(motion, "_resolve_path", same_path)
    monkeypatch.setattr(motion, "_FONTS_WAIT_S", 0.5)
    html = ("<!doctype html><html><body style='margin:0;background:#123456'><script>"
            "Object.defineProperty(document, 'fonts', {value: {ready: new Promise(() => {})}});"
            "</script></body></html>")
    out = tmp_path / "never.png"
    began = time.monotonic()
    text = _run(motion.handle_render_still({"html": html, "width": 64, "height": 64,
                                            "output_path": str(out)}))
    assert not text.startswith("Error"), text
    assert out.exists() and time.monotonic() - began < 30.0

    monkeypatch.setattr(motion, "_FONTS_WAIT_S", 60.0)
    monkeypatch.setattr(motion, "_STILL_MAX_S", 3.0)
    late = tmp_path / "late.png"
    text = _run(motion.handle_render_still({"html": html, "width": 64, "height": 64,
                                            "output_path": str(late)}))
    assert text == "Error: " + motion.STILL_TOO_LONG and not late.exists()
