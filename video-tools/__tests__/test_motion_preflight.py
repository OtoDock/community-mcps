"""render_motion_clip / render_still file:// preflight — pure logic, no
Chromium needed. The tool runs platform-side: file:// refs must be judged
through _resolve_path (and point at the page's origin, whose route serves
them), and unresolvable ones must fail LOUDLY before capture (the
historical behavior was a silent broken frame)."""

import asyncio
import contextlib

import pytest

import motion


def _mapped_resolver(monkeypatch, mapping):
    asked: list[str] = []

    async def resolve(p, writing=False):
        asked.append(p)
        if p in mapping:
            return mapping[p]
        raise ValueError(f"Cannot open '{p}'")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    return asked


def test_localize_rewrites_resolvable_refs(monkeypatch):
    asked = _mapped_resolver(monkeypatch,
                             {"/workspace/a.png": "/agents/x/users/u/workspace/a.png"})
    doc = '<img src="file:///workspace/a.png">'
    out = asyncio.run(motion._localize_file_urls(doc))
    assert asked == ["/workspace/a.png"]
    # Served by the page's route from the origin, never as a file:// name.
    assert f'src="{motion.ORIGIN}/workspace/a.png"' in out
    assert "file://" not in out


def test_localize_percent_decodes_before_resolving(monkeypatch):
    asked = _mapped_resolver(monkeypatch,
                             {"/workspace/my img.png": "/agents/x/my img.png"})
    out = asyncio.run(motion._localize_file_urls(
        'url("file:///workspace/my%20img.png")'))
    assert asked == ["/workspace/my img.png"]
    assert f"{motion.ORIGIN}/workspace/my%20img.png" in out


def test_localize_fails_loud_on_unresolvable(monkeypatch):
    _mapped_resolver(monkeypatch, {"/workspace/a.png": "/agents/x/a.png"})
    doc = ('<img src="file:///home/user/gone.png">'
           '<img src="file:///workspace/a.png">')
    with pytest.raises(ValueError) as e:
        asyncio.run(motion._localize_file_urls(doc))
    msg = str(e.value)
    assert "file:///home/user/gone.png" in msg
    assert "file:///workspace/a.png" not in msg


def test_prepare_inline_fails_loud_on_bad_ref(monkeypatch):
    _mapped_resolver(monkeypatch, {})
    with pytest.raises(ValueError) as e:
        asyncio.run(motion._prepare_page_doc('<img src="file:///nope/x.png">', None))
    msg = str(e.value)
    assert "file:///nope/x.png" in msg
    assert "platform-side" in msg


def test_prepare_htmlpath_without_refs_keeps_its_folder(monkeypatch, tmp_path):
    src = tmp_path / "doc.html"
    src.write_text("<html><body>hi</body></html>", encoding="utf-8")
    _mapped_resolver(monkeypatch, {"/workspace/site/doc.html": str(src)})
    got = asyncio.run(motion._prepare_page_doc(None, "/workspace/site/doc.html"))
    assert got.html == src.read_bytes()
    # Relative refs keep their original base: the html_path's own folder,
    # in the form the caller named it (the route judges what they name).
    assert got.folder == "/workspace/site"


def test_prepare_htmlpath_rewrites_file_refs_and_keeps_relative_ones(monkeypatch, tmp_path):
    src = tmp_path / "doc.html"
    src.write_text('<html><head><title>t</title></head>'
                   '<body><img src="file:///workspace/a.png">'
                   '<img src="rel.png"></body></html>', encoding="utf-8")
    _mapped_resolver(monkeypatch, {
        "/workspace/doc.html": str(src),
        "/workspace/a.png": "/agents/x/a.png",
    })
    got = asyncio.run(motion._prepare_page_doc(None, "/workspace/doc.html"))
    text = got.html.decode("utf-8")
    assert f'src="{motion.ORIGIN}/workspace/a.png"' in text
    assert 'src="rel.png"' in text and "file://" not in text
    assert got.folder == "/workspace"



def test_a_motion_clips_encode_ends_while_the_call_still_has_time(monkeypatch, tmp_path):
    """The encode may use the time the call has left, less the margin its
    answer needs. An encode past that is stopped and nothing is written,
    and with no time left no encode starts: either way the answer comes
    back in time and says so."""
    import fftools

    async def resolve(p, writing=False):
        return str(tmp_path / "late.mp4")
    monkeypatch.setattr(motion, "_resolve_path", resolve)

    async def captured(doc, out_dir, *a, **kw):
        return 3, 3, []
    monkeypatch.setattr(motion, "_capture_frames", captured)
    limits: list[float] = []

    async def encode(args, timeout=600.0, **kw):
        limits.append(timeout)
        raise fftools.FFmpegTimeout(f"ffmpeg timed out after {int(timeout)}s")
    monkeypatch.setattr(motion, "run_ffmpeg", encode)
    call = {"html": "<p>x</p>", "width": 64, "height": 64, "fps": 10, "duration": 0.3,
            "output_path": "/workspace/late.mp4"}

    monkeypatch.setattr(motion, "call_budget_s", lambda: motion._ANSWER_MARGIN_S + 40.0)
    assert asyncio.run(motion.handle_render_motion_clip(call)) == motion.OUT_OF_TIME
    assert len(limits) == 1 and 30.0 < limits[0] <= 40.0

    monkeypatch.setattr(motion, "call_budget_s", lambda: motion._ANSWER_MARGIN_S)
    assert asyncio.run(motion.handle_render_motion_clip(call)) == motion.OUT_OF_TIME
    assert len(limits) == 1                       # no encode started
    assert not (tmp_path / "late.mp4").exists()


def test_a_motion_clips_limit_counts_its_wait_at_the_gate(monkeypatch, tmp_path):
    """While other renders hold the gate, the encode's wait is part of its
    limit: the call answers that its time ran out, leaves the line and
    writes nothing."""
    import time

    import fftools

    async def resolve(p, writing=False):
        return str(tmp_path / "late.mp4")
    monkeypatch.setattr(motion, "_resolve_path", resolve)

    async def captured(doc, out_dir, *a, **kw):
        return 3, 3, []
    monkeypatch.setattr(motion, "_capture_frames", captured)
    monkeypatch.setattr(motion, "call_budget_s", lambda: motion._ANSWER_MARGIN_S + 1.0)
    call = {"html": "<p>x</p>", "width": 64, "height": 64, "fps": 10, "duration": 0.3,
            "output_path": "/workspace/late.mp4"}

    async def main():
        holders = contextlib.AsyncExitStack()
        for _ in range(fftools.MAX_RENDERS):
            await holders.enter_async_context(fftools.heavy_run())
        try:
            began = time.monotonic()
            answer = await motion.handle_render_motion_clip(call)
            return answer, time.monotonic() - began, len(fftools._gate.line)
        finally:
            await holders.aclose()
    answer, took, waiting = asyncio.run(main())
    assert answer == motion.OUT_OF_TIME and took < 5.0 and waiting == 0
    assert not (tmp_path / "late.mp4").exists()


def test_a_clip_that_landed_as_the_time_ran_out_is_answered(monkeypatch, tmp_path):
    """The limit ends when the file lands: a landing that runs past the
    call's time goes on to the answer, which names the clip, and never
    says that nothing was written."""
    import fftools

    out = tmp_path / "landed.mp4"

    async def resolve(p, writing=False):
        return str(out)
    monkeypatch.setattr(motion, "_resolve_path", resolve)

    async def captured(doc, out_dir, *a, **kw):
        return 3, 3, []
    monkeypatch.setattr(motion, "_capture_frames", captured)

    async def encode(args, timeout=600.0, landing=None, **kw):
        landing()
        await asyncio.sleep(1.5)            # a slow sync and rename, past the limit
        out.write_bytes(b"clip")
        return b"", ""
    monkeypatch.setattr(motion, "run_ffmpeg", encode)

    async def unread(path):
        raise fftools.FFmpegError("not probed in this test")
    monkeypatch.setattr(motion, "probe", unread)

    async def notify(path):
        return None
    monkeypatch.setattr(motion, "_notify_file_written", notify)
    monkeypatch.setattr(motion, "call_budget_s", lambda: motion._ANSWER_MARGIN_S + 1.0)
    call = {"html": "<p>x</p>", "width": 64, "height": 64, "fps": 10, "duration": 0.3,
            "output_path": "/workspace/landed.mp4"}
    answer = asyncio.run(motion.handle_render_motion_clip(call))
    assert answer.startswith("Rendered motion clip: "), answer
    assert out.read_bytes() == b"clip"


def test_a_clip_gone_right_after_it_landed_is_one_sentence(monkeypatch, tmp_path):
    """A clip removed between its landing and the answer: the call says so
    in one sentence, with no raw error."""
    async def resolve(p, writing=False):
        return str(tmp_path / "gone.mp4")
    monkeypatch.setattr(motion, "_resolve_path", resolve)

    async def captured(doc, out_dir, *a, **kw):
        return 3, 3, []
    monkeypatch.setattr(motion, "_capture_frames", captured)

    async def encode(*a, **kw):
        return None                     # landed, then gone before the answer
    monkeypatch.setattr(motion, "_encode", encode)

    async def notify(path):
        return None
    monkeypatch.setattr(motion, "_notify_file_written", notify)
    call = {"html": "<p>x</p>", "width": 64, "height": 64, "fps": 10, "duration": 0.3,
            "output_path": "/workspace/gone.mp4"}
    answer = asyncio.run(motion.handle_render_motion_clip(call))
    assert answer == "Error: " + motion.LANDED_GONE.format(name="gone.mp4")


def test_a_capture_that_gets_no_turn_in_its_time_says_so(monkeypatch, tmp_path):
    """While two captures hold their turns, a still waits until its own
    ceiling and a clip until its capture time is spent. Each then answers in
    one sentence, starts no browser and writes nothing."""
    import time
    from pathlib import Path

    async def resolve(p, writing=False):
        return str(tmp_path / Path(p).name)
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    launched: list = []

    async def launch(p):
        launched.append(p)
        raise RuntimeError("no browser in this test")
    monkeypatch.setattr(motion, "_launch", launch)
    monkeypatch.setattr(motion, "_STILL_MAX_S", 0.3)
    monkeypatch.setattr(motion, "call_budget_s", lambda: motion._ANSWER_MARGIN_S + 0.5)
    still = {"html": "<p>x</p>", "width": 64, "height": 64, "output_path": "/workspace/still.png"}
    clip = {"html": "<p>x</p>", "width": 64, "height": 64, "fps": 10, "duration": 0.3,
            "output_path": "/workspace/clip.mp4"}

    async def main():
        slot = motion._capture_slot()
        for _ in range(motion._CAPTURES_AT_ONCE):
            await slot.acquire()
        began = time.monotonic()
        answers = [await motion.handle_render_still(still), await motion.handle_render_motion_clip(clip)]
        return answers, time.monotonic() - began
    answers, took = asyncio.run(main())
    assert answers == ["Error: " + motion.NO_TURN] * 2 and took < 5.0
    assert not launched and not list(tmp_path.iterdir())
