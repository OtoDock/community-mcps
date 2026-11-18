"""Text values that ffmpeg's filter graph carries as written: a colour is a
colour, and despill's channel is one of two words. Anything else is refused by
the validator, and by the graph's own writers if it ever reaches them."""

import asyncio

import pytest

import color as color_mod
import composition as comp_mod
import compiler
import motion
from fftools import ff_color

NOT_COLOURS = ["black:s=2x2", "red[x]", "#00ff00,drawtext", "0x00ff00;", "white@0.5:x", "", "#12345",
               "green blue", "1black"]


@pytest.mark.parametrize("value", ["#00ff00", "#00FF0080", "#0f0", "0x00ff00", "black", "White",
                                   "black@0.5", "#000000@.25"])
def test_a_colour_is_a_colour(value):
    assert color_mod.is_colour(value)
    out = ff_color(value)
    assert not any(ch in out for ch in ":;,[]='# ")


@pytest.mark.parametrize("value", NOT_COLOURS)
def test_anything_else_is_not_a_colour(value):
    assert not color_mod.is_colour(value)
    if value:
        with pytest.raises(ValueError, match="not a colour"):
            ff_color(value)


def test_a_short_hex_is_written_in_full():
    assert ff_color("#0f0") == "0x00ff00"
    assert ff_color("#00ff0080") == "0x00ff0080"


def _errors(comp) -> list[str]:
    return [i["message"] for i in comp_mod.validate(comp) if i["level"] == "error"]


def _comp() -> dict:
    return comp_mod.new_composition()


def test_a_fill_must_be_a_colour():
    comp = _comp()
    comp["tracks"][0]["clips"] = [{"fill": "black:s=2x2", "duration": 1}]
    assert any("fill" in m and "colour" in m for m in _errors(comp))
    comp["tracks"][0]["clips"] = [{"fill": "#0a0a12", "duration": 1}]
    assert not [m for m in _errors(comp) if "colour" in m]


def test_a_background_must_be_a_colour():
    comp = _comp()
    comp["project"]["background"] = "black,drawbox"
    assert any("background" in m and "colour" in m for m in _errors(comp))


def _overlay(effect) -> dict:
    comp = _comp()
    comp["tracks"][0]["clips"] = [{"fill": "#000000", "duration": 2}]
    comp["tracks"].append({"kind": "overlay", "clips": [
        {"fill": "#00ff00", "start": 0, "duration": 1, "effects": [effect]}]})
    return comp


@pytest.mark.parametrize("kind", ["chromakey", "colorkey"])
def test_a_key_colour_must_be_a_colour(kind):
    assert any("color" in m and "colour" in m
               for m in _errors(_overlay({"type": kind, "color": "#00ff00[x]"})))
    assert not [m for m in _errors(_overlay({"type": kind, "color": "#00ff00"})) if "colour" in m]


def test_despills_channel_is_green_or_blue():
    assert any("channel" in m for m in _errors(_overlay({"type": "despill", "channel": "green:x=1"})))
    assert not [m for m in _errors(_overlay({"type": "despill", "channel": "blue"})) if "channel" in m]
    with pytest.raises(ValueError, match="channel"):
        compiler._EFFECT_FILTERS["despill"]({"type": "despill", "channel": "red"})


def test_a_motion_clips_background_must_be_a_colour(monkeypatch, tmp_path):
    monkeypatch.setattr(motion, "_resolve_path", lambda path, writing=False: str(tmp_path / "m.mp4"))
    answer = asyncio.run(motion.handle_render_motion_clip(
        {"html": "<p>x</p>", "duration": 1, "background": "black,drawtext", "output_path": "/workspace/m.mp4"}))
    assert "background" in answer and "colour" in answer


# ── A list of other files is never an input ──────────────────────────────


def test_a_list_of_files_is_refused_before_ffprobe_opens_it(tmp_path, monkeypatch):
    import fftools
    opened: list = []

    async def never(*a, **kw):
        opened.append(a)
        raise AssertionError("ffprobe ran")
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", never)
    for name, body in (("list.mp4", "ffconcat version 1.0\nfile a.mp4\n"),
                       ("play.m3u8", "#EXTM3U\n#EXTINF:1.0,\nseg.ts\n"),
                       ("s.mp4", "v=0\no=- 0 0 IN IP4 127.0.0.1\n"),
                       ("m.mp4", '<?xml version="1.0"?><MPD/>')):
        f = tmp_path / name
        f.write_text(body)
        with pytest.raises(fftools.FFmpegError, match="lists other files"):
            asyncio.run(fftools.probe(str(f)))
    assert opened == []


def test_an_svg_picture_is_still_probed(monkeypatch, tmp_path):
    """An SVG is XML like a DASH manifest, and the image reads it as a
    picture: it goes to ffprobe."""
    import fftools
    f = tmp_path / "logo.svg"
    f.write_text('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>\n')

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"format": {"format_name": "svg_pipe"}, "streams": []}', b""

    async def fake(*a, **kw):
        return _Proc()
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", fake)
    assert asyncio.run(fftools.probe(str(f)))["format"]["format_name"] == "svg_pipe"


def test_a_list_named_by_the_probe_is_refused(monkeypatch, tmp_path):
    import fftools
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"format": {"format_name": "concat"}, "streams": []}', b""

    async def fake(*a, **kw):
        return _Proc()
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", fake)
    with pytest.raises(fftools.FFmpegError, match="lists other files"):
        asyncio.run(fftools.probe(str(f)))
