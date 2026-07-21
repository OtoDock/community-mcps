"""Unit tests for stab.py: presets, filter strings, sidecar cache keying,
and the detect-pass runner (ffmpeg mocked — the real two-pass is exercised
in test_render_smoke.py)."""

import asyncio

import pytest

import stab


def test_spec_params_presets_and_overrides():
    assert stab.spec_params(True) == {"shakiness": 6, "smoothing": 15, "zoom": 0.0}
    assert stab.spec_params({"strength": "high"})["shakiness"] == 9
    assert stab.spec_params({"strength": "low"})["smoothing"] == 8
    p = stab.spec_params({"strength": "high", "smoothing": 40, "zoom": 3})
    assert p == {"shakiness": 9, "smoothing": 40, "zoom": 3.0}
    with pytest.raises(ValueError):
        stab.spec_params({"strength": "maximum"})


def test_transform_filters_shape():
    flts = stab.transform_filters("/tmp/x/stab0.trf", {"smoothing": 15, "zoom": 0})
    assert flts == [
        "vidstabtransform=input=/tmp/x/stab0.trf:smoothing=15"
        ":optzoom=1:interpol=bicubic",
        "unsharp=5:5:0.8:3:3:0.4",
    ]
    with_zoom = stab.transform_filters("/tmp/x/s.trf", {"smoothing": 8, "zoom": 2.5})
    assert ":zoom=2.5" in with_zoom[0]


def test_sidecar_key_varies_with_span_params_and_identity(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 100)
    a = stab.sidecar_path(str(src), (0.0, 5.0), 6)
    assert a.parent == tmp_path and a.name.startswith("clip.stab-")
    assert a.suffix == ".trf"
    b = stab.sidecar_path(str(src), (0.0, 6.0), 6)   # different span
    c = stab.sidecar_path(str(src), (0.0, 5.0), 9)   # different shakiness
    d = stab.sidecar_path(str(src), None, 6)          # full file
    assert len({a.name, b.name, c.name, d.name}) == 4
    src.write_bytes(b"y" * 120)                       # replaced file
    assert stab.sidecar_path(str(src), (0.0, 5.0), 6).name != a.name


def test_ensure_trf_runs_detect_then_caches(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 100)
    calls = []

    async def fake_run(args, timeout=0, heavy=True):
        calls.append(args)
        # vidstabdetect writes the result file — emulate it.
        vf = args[args.index("-vf") + 1]
        result = vf.split("result=")[1]
        with open(result, "w") as fh:
            fh.write("VID.STAB 1\n")
        return b"", ""

    monkeypatch.setattr(stab, "run_ffmpeg", fake_run)

    staged1 = tmp_path / "work1" / "stab0.trf"
    staged1.parent.mkdir()
    hit, sidecar = asyncio.run(
        stab.ensure_trf(str(src), (0.5, 3.5), 6, str(staged1)))
    assert hit is False and sidecar is not None
    assert staged1.exists() and staged1.read_text().startswith("VID.STAB")
    assert "trim=start=0.5:end=3.5,setpts=PTS-STARTPTS," in calls[0][calls[0].index("-vf") + 1]

    # Second render: cache hit, no ffmpeg call, staged copy identical.
    staged2 = tmp_path / "work2" / "stab0.trf"
    staged2.parent.mkdir()
    hit2, sidecar2 = asyncio.run(
        stab.ensure_trf(str(src), (0.5, 3.5), 6, str(staged2)))
    assert hit2 is True and sidecar2 == sidecar
    assert len(calls) == 1
    assert staged2.read_text() == staged1.read_text()


def test_ensure_trf_full_file_has_no_trim(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x")
    captured = {}

    async def fake_run(args, timeout=0, heavy=True):
        vf = args[args.index("-vf") + 1]
        captured["vf"] = vf
        with open(vf.split("result=")[1], "w") as fh:
            fh.write("VID.STAB 1\n")
        return b"", ""

    monkeypatch.setattr(stab, "run_ffmpeg", fake_run)
    staged = tmp_path / "s.trf"
    asyncio.run(stab.ensure_trf(str(src), None, 9, str(staged)))
    assert captured["vf"].startswith("setpts=PTS-STARTPTS,vidstabdetect=shakiness=9")
