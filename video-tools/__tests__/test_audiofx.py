"""Unit tests for audiofx.py filter builders (pure strings — the audible
A/B lives in test_render_smoke.py)."""

from pathlib import Path

import pytest

import audiofx


def test_rnnoise_model_is_bundled():
    assert Path(audiofx.RNNOISE_MODEL).exists()
    assert Path(audiofx.RNNOISE_MODEL).stat().st_size > 100_000


def test_clip_chain_fixed_order_and_stage_skip():
    chain = audiofx.clip_chain({"deess": True, "compress": True,
                                "eq": {"preset": "voice"}, "denoise": True})
    joined = ",".join(chain)
    # denoise → eq → compress → deess regardless of dict order.
    assert joined.index("afftdn") < joined.index("highpass")
    assert joined.index("equalizer") < joined.index("acompressor")
    assert joined.index("acompressor") < joined.index("deesser")

    assert audiofx.clip_chain(None) == []
    assert audiofx.clip_chain({"denoise": False, "eq": None}) == []


def test_denoise_variants():
    assert audiofx.clip_chain({"denoise": True}) == ["afftdn=nr=12:nf=-30"]
    assert audiofx.clip_chain(
        {"denoise": {"strength": 24, "floor_db": -40}}) == ["afftdn=nr=24:nf=-40"]
    voice = audiofx.clip_chain({"denoise": "voice"})
    assert voice == [f"arnndn=m={audiofx.RNNOISE_MODEL}"]


def test_eq_bands_and_presets():
    bands = audiofx.clip_chain({"eq": {"bands": [
        {"f": 3000, "gain_db": 2.5, "q": 1.2},
        {"f": 200, "gain_db": -3, "width_hz": 150},
    ]}})
    assert bands == ["equalizer=f=3000:t=q:w=1.2:g=2.5",
                     "equalizer=f=200:t=h:width=150:g=-3"]
    tel = audiofx.clip_chain({"eq": {"preset": "telephone"}})
    assert tel == ["highpass=f=300", "lowpass=f=3400"]


def test_compress_converts_db_to_linear():
    [flt] = audiofx.clip_chain({"compress": {"threshold_db": -20, "ratio": 4,
                                             "makeup_db": 6}})
    assert "threshold=0.1" in flt          # 10^(-20/20) = 0.1
    assert "ratio=4" in flt
    assert "makeup=1.99526" in flt         # 10^(6/20)


def test_master_chain_limiter():
    chain = audiofx.master_chain({"limiter": True, "gain_db": -3})
    assert chain == ["alimiter=limit=0.891251:level=disabled"]  # -1 dBTP
    chain = audiofx.master_chain({"limiter": {"ceiling_db": -3},
                                  "eq": {"preset": "warm"}, "compress": True})
    joined = ",".join(chain)
    assert joined.index("bass") < joined.index("acompressor")
    assert chain[-1] == "alimiter=limit=0.707946:level=disabled"
    assert audiofx.master_chain({"gain_db": -3}) == []


def test_enhance_chain_presets_and_overrides():
    voice = audiofx.enhance_chain("voice")
    assert voice[0].startswith("arnndn=")
    assert voice[-1].startswith("alimiter=")
    music = audiofx.enhance_chain("music")
    assert not any(f.startswith(("afftdn", "arnndn", "deesser")) for f in music)

    no_dn = audiofx.enhance_chain("voice", {"denoise": False})
    assert not any(f.startswith(("afftdn", "arnndn")) for f in no_dn)

    with pytest.raises(ValueError):
        audiofx.enhance_chain("podcast")
