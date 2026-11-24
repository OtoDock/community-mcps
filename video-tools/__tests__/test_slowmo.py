"""Unit tests for slowmo.py: native-first logic, mezzanine cache keying,
the runner (ffmpeg mocked), and LRU pruning. Real flow renders are
exercised in test_render_smoke.py."""

import asyncio
import time
from pathlib import Path

import slowmo


def test_native_sufficient():
    assert slowmo.native_sufficient(60.0, 0.5, 30.0)          # 60×0.5 = 30
    assert slowmo.native_sufficient(120.0, 0.25, 30.0)
    assert not slowmo.native_sufficient(30.0, 0.5, 30.0)      # 15 < 30
    assert not slowmo.native_sufficient(0.0, 0.5, 30.0)       # unknown fps


def test_cache_key_varies(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 64)
    base = slowmo.cache_key(str(src), (0.0, 3.0), 0.25, 30.0, None)
    assert slowmo.cache_key(str(src), (0.0, 4.0), 0.25, 30.0, None) != base
    assert slowmo.cache_key(str(src), (0.0, 3.0), 0.5, 30.0, None) != base
    assert slowmo.cache_key(str(src), (0.0, 3.0), 0.25, 60.0, None) != base
    stab = {"shakiness": 6, "smoothing": 15, "zoom": 0.0}
    assert slowmo.cache_key(str(src), (0.0, 3.0), 0.25, 30.0, stab) != base
    # Same-strength stab with a different trf tmp path must NOT change the
    # key (the path varies per render; the transform identity does not).
    stab2 = dict(stab, trf="/tmp/other/stab0.trf")
    assert (slowmo.cache_key(str(src), (0.0, 3.0), 0.25, 30.0, stab2)
            == slowmo.cache_key(str(src), (0.0, 3.0), 0.25, 30.0, stab))


def test_ensure_mezzanine_builds_then_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(slowmo, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 64)
    calls = []

    async def fake_run(args, timeout=0, heavy=True):
        calls.append(args)
        with open(args[-1], "wb") as fh:
            fh.write(b"mezz")
        return b"", ""

    monkeypatch.setattr(slowmo, "run_ffmpeg", fake_run)

    path, hit = asyncio.run(slowmo.ensure_mezzanine(
        str(src), (0.5, 3.5), 0.25, 30.0))
    assert hit is False and path.endswith(".mp4")
    vf = calls[0][calls[0].index("-vf") + 1]
    assert vf.startswith("trim=start=0.5:end=3.5,setpts=(PTS-STARTPTS)/0.25,")
    assert "mi_mode=mci" in vf and "fps=30" in vf

    path2, hit2 = asyncio.run(slowmo.ensure_mezzanine(
        str(src), (0.5, 3.5), 0.25, 30.0))
    assert hit2 is True and path2 == path and len(calls) == 1


def test_ensure_mezzanine_bakes_stab_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(slowmo, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 64)
    captured = {}

    async def fake_run(args, timeout=0, heavy=True):
        captured["vf"] = args[args.index("-vf") + 1]
        with open(args[-1], "wb") as fh:
            fh.write(b"mezz")
        return b"", ""

    monkeypatch.setattr(slowmo, "run_ffmpeg", fake_run)
    stab_params = {"shakiness": 9, "smoothing": 25, "zoom": 0.0}
    asyncio.run(slowmo.ensure_mezzanine(
        str(src), (0.0, 2.0), 0.2, 30.0,
        stab_filters=["vidstabtransform=input=/tmp/r/stab0.trf:smoothing=25"
                      ":optzoom=1:interpol=bicubic", "unsharp=5:5:0.8:3:3:0.4"],
        stab_params=stab_params))
    # Stabilize BEFORE interpolating — synthesized frames from shaky input
    # would warp.
    vf = captured["vf"]
    assert vf.index("vidstabtransform") < vf.index("mi_mode=mci")


def test_prune_keeps_newest_within_budget(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(slowmo, "CACHE_DIR", cache)
    monkeypatch.setattr(slowmo, "CACHE_MAX_BYTES", 250)
    now = time.time()
    for i in range(4):
        f = cache / f"mezz-{i}.mp4"
        f.write_bytes(b"y" * 100)
        # Strictly increasing mtimes: 0 oldest, 3 newest.
        import os
        os.utime(f, (now - 100 + i, now - 100 + i))
    slowmo._prune_cache()
    left = sorted(p.name for p in cache.glob("mezz-*.mp4"))
    assert left == ["mezz-2.mp4", "mezz-3.mp4"]


def test_two_misses_at_once_write_temps_of_their_own(tmp_path, monkeypatch):
    """Two renders that miss on one key at once each encode to a temp of its
    own, and the entry is one whole file. A failed build leaves no temp, and
    the prune counts none."""
    import pytest
    cache = tmp_path / "cache"
    monkeypatch.setattr(slowmo, "CACHE_DIR", cache)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 64)
    temps: list[str] = []

    async def slow_run(args, timeout=0, heavy=True):
        temps.append(args[-1])
        with open(args[-1], "wb") as fh:
            fh.write(b"first half ")
            await asyncio.sleep(0.2)
            fh.write(b"second half")
        return b"", ""
    monkeypatch.setattr(slowmo, "run_ffmpeg", slow_run)

    async def both():
        return await asyncio.gather(*(slowmo.ensure_mezzanine(str(src), (0.0, 1.0), 0.5, 30.0)
                                      for _ in range(2)))
    (one, _), (two, _) = asyncio.run(both())
    assert one == two and len(set(temps)) == 2
    assert open(one, "rb").read() == b"first half second half"
    assert [p.name for p in cache.iterdir()] == [Path(one).name]

    async def failing(args, timeout=0, heavy=True):
        with open(args[-1], "wb") as fh:
            fh.write(b"part")
        raise RuntimeError("encode failed")
    monkeypatch.setattr(slowmo, "run_ffmpeg", failing)
    with pytest.raises(RuntimeError):
        asyncio.run(slowmo.ensure_mezzanine(str(src), (1.0, 2.0), 0.5, 30.0))
    assert sorted(p.name for p in cache.iterdir()) == [Path(one).name]

    monkeypatch.setattr(slowmo, "CACHE_MAX_BYTES", 1)
    (cache / ".mezz-live.0123456789abcdef.mp4").write_bytes(b"z" * 100)
    (cache / "mezz-newer.mp4").write_bytes(b"y" * 100)
    slowmo._prune_cache()
    assert (cache / ".mezz-live.0123456789abcdef.mp4").exists()


def test_the_prune_removes_a_temp_of_a_process_that_ended(tmp_path, monkeypatch):
    """A build a process never finished leaves its temp, last written before
    this process started: the prune removes it, and keeps a temp written
    since, which a build of this process may still be writing."""
    import os
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(slowmo, "CACHE_DIR", cache)
    dead = cache / ".mezz-dead.0123456789abcdef.mp4"
    dead.write_bytes(b"z" * 100)
    os.utime(dead, (slowmo.PROCESS_START - 60, slowmo.PROCESS_START - 60))
    live = cache / ".mezz-live.fedcba9876543210.mp4"
    live.write_bytes(b"z" * 100)
    slowmo._prune_cache()
    assert not dead.exists() and live.exists()
