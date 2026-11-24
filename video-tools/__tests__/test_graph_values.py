"""Text values that ffmpeg's filter graph carries as written: a colour is a
colour, and despill's channel is one of two words. Anything else is refused by
the validator, and by the graph's own writers if it ever reaches them."""

import asyncio
import subprocess

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


def test_a_number_is_written_with_fixed_decimals_to_the_microsecond():
    """A cut at 1234.5678 s stays there (six significant digits made it
    1234.57, a third of a frame off), and no number is ever written in
    exponent form."""
    from fftools import _f
    assert _f(1234.5678) == "1234.5678"
    assert _f(0.1 + 0.2) == "0.3"
    assert _f(1e-7) == "0"
    assert _f(-0.0) == "0"
    assert _f(3) == "3"
    assert _f(86399.999999) == "86399.999999"
    assert "e" not in _f(1e-5)


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
    async def resolve(path, writing=False):
        return str(tmp_path / "m.mp4")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
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


def test_a_playlist_named_like_a_clip_never_opens_the_clip_it_lists(monkeypatch, tmp_path):
    """A file under the mount named ``clip.mp4`` whose bytes are an HLS
    playlist naming another clip under the mount by its absolute path: the
    probe refuses it by its first bytes, and past them by the demuxers a
    person's file is read with (``READ_GUARD``); an encode refuses it the
    same way. The clip it names is never read: its frame never lands."""
    import fftools
    from conftest import HAVE_FFMPEG
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available")
    listed = tmp_path / "second.ts"
    subprocess.run([fftools.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(listed)], check=True, timeout=120)
    playlist = tmp_path / "clip.mp4"
    playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                        f"#EXTINF:1.0,\n{listed}\n#EXT-X-ENDLIST\n")
    ran: list[list[str]] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*cmd, **kw):
        ran.append(list(cmd))
        return await real_spawn(*cmd, **kw)
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(fftools.FFmpegError, match="lists other files"):
        asyncio.run(fftools.probe(str(playlist)))
    assert ran == []                                  # refused before ffprobe ran

    # Past the first bytes: ffprobe reads the descriptor under the guard and
    # finds no format it may read the file with.
    monkeypatch.setattr(fftools, "_lists_other_files", lambda path: False)
    with pytest.raises(fftools.FFmpegError):
        asyncio.run(fftools.probe(str(playlist)))
    assert "-format_whitelist" in ran[-1] and str(playlist) not in ran[-1]

    frame = tmp_path / "frame.png"
    with pytest.raises(fftools.FFmpegError):
        asyncio.run(fftools.run_ffmpeg(["-i", str(playlist), "-frames:v", "1", str(frame)],
                                       heavy=False, timeout=60))
    assert "-format_whitelist" in ran[-1] and "hls" not in fftools.INPUT_FORMATS.split(",")
    assert not frame.exists() and not list(tmp_path.glob(".frame.png.*.partial"))


def test_a_kind_of_file_the_tool_does_not_read_is_named_in_one_sentence(tmp_path):
    """A kind ffmpeg knows but the tool does not take (SVG, Y4M), and one no
    format the tool reads with takes at all (TGA): each answers one sentence
    naming the file, never ffprobe's own text. A cut file of a kind the
    tool reads keeps ffprobe's reason."""
    import fftools
    from conftest import HAVE_FFMPEG
    from PIL import Image
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available")
    Image.new("RGB", (16, 16), (255, 0, 0)).save(tmp_path / "a.tga")
    (tmp_path / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"/>')
    good = tmp_path / "good.mp4"
    for args in (["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5:duration=1", "-f", "yuv4mpegpipe",
                  str(tmp_path / "a.y4m")],
                 ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5:duration=1", "-c:v", "libx264",
                  str(good)]):
        subprocess.run([fftools.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       check=True, timeout=120)
    for name in ("a.tga", "a.svg", "a.y4m"):
        with pytest.raises(fftools.FFmpegError) as err:
            asyncio.run(fftools.probe(str(tmp_path / name)))
        assert str(err.value) == f"The tool does not read this kind of file: {name}."
    (tmp_path / "cut.mp4").write_bytes(good.read_bytes()[:1500])
    with pytest.raises(fftools.FFmpegError, match="moov atom not found"):
        asyncio.run(fftools.probe(str(tmp_path / "cut.mp4")))
