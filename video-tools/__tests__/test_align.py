"""align_audio: the lag bounds and the sanity checks, on synthetic
recordings whose true offset is known (the real failure this reproduces:
a 13 s camera clip against a 50 s recorder file whose shared speech sat
32.5 s in — the old symmetric clamp never searched past 13 s)."""

import asyncio

import pytest

from conftest import HAVE_FFMPEG

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")

import analysis  # noqa: E402

SR = 8000

@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """ref: 6 s with the shared event at 1 s. target: 40 s of unrelated
    noise with the same event at 31 s → true offset +30.0 s."""
    import numpy as np
    import soundfile as sf
    root = tmp_path_factory.mktemp("align")
    rng = np.random.default_rng(3)
    event = rng.normal(0, 0.3, SR * 4)
    event *= 0.5 + 0.5 * np.sin(2 * np.pi * 3 * np.arange(len(event)) / SR) ** 2
    ref = rng.normal(0, 0.02, SR * 6)
    ref[SR:SR + len(event)] += event
    tgt = rng.normal(0, 0.02, SR * 40)
    tgt[SR * 31:SR * 31 + len(event)] += 0.7 * event + rng.normal(0, 0.05, len(event))
    paths = {}
    for name, y in (("ref", ref), ("tgt", tgt)):
        p = root / f"{name}.wav"
        sf.write(str(p), y, SR, subtype="PCM_16")
        paths[name] = str(p)
    return paths

def test_offset_beyond_the_shorter_file_is_found(pair):
    res = analysis._align_waveforms(pair["ref"], pair["tgt"], 60.0)
    assert abs(res["offset"] - 30.0) < 0.002, res["offset"]
    assert res["searched"] == (-(6 - 1 / SR), 40 - 1 / SR)
    assert res["candidates"][0][0] == res["offset"]
    assert res["envelope_agreement"] > 0.5, res["envelope_agreement"]
    assert not res["at_edge"]
    grade, problems = analysis.align_sanity(res, analysis._align_grade(res["ratio"]))
    assert problems == [] and grade in ("strong", "fair"), (grade, res["ratio"])

def test_offset_outside_the_window_is_flagged(pair):
    res = analysis._align_waveforms(pair["ref"], pair["tgt"], 10.0)
    assert res["searched"] == (-(6 - 1 / SR), 10.0)
    assert abs(res["offset"] - 30.0) > 1.0          # cannot be right
    grade, problems = analysis.align_sanity(res, analysis._align_grade(res["ratio"]))
    assert grade == "weak"
    assert problems, res
    assert any("envelopes disagree" in p or "edge of the searched range" in p
               for p in problems), problems

def test_handler_reports_placement_candidates_and_verdict(pair, monkeypatch):
    monkeypatch.setattr(analysis, "_resolve_path", lambda p: p)
    text = asyncio.run(analysis.handle_align_audio(
        {"ref": pair["ref"], "target": pair["tgt"], "max_offset": 60}))
    assert "offset: +30.0" in text, text
    assert "searched -6.00…+40.00 s" in text, text
    assert "placement: target starts at ref -30.0" in text
    assert "candidates:" in text
    assert "IMPLAUSIBLE" not in text

    text = asyncio.run(analysis.handle_align_audio(
        {"ref": pair["ref"], "target": pair["tgt"], "max_offset": 10}))
    assert "confidence: weak" in text and "IMPLAUSIBLE placement" in text, text
    assert "raise max_offset" in text

def test_overlap_too_short_is_rejected():
    res = {"overlap": 0.4, "at_edge": False, "envelope_agreement": 0.9}
    grade, problems = analysis.align_sanity(res, "strong")
    assert grade == "weak" and "overlapping" in problems[0]
    res = {"overlap": 5.0, "at_edge": False, "envelope_agreement": 0.1}
    assert analysis.align_sanity(res, "strong") == ("strong", [])   # PHAT wins
    assert analysis.align_sanity(res, "fair")[0] == "weak"
