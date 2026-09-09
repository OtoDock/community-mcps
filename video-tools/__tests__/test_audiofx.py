"""Unit tests for audiofx.py filter builders (pure strings — the audible
A/B lives in test_render_smoke.py)."""

from pathlib import Path

import pytest

import audiofx

_COMP = ["aresample=48000", "apad=pad_len={d}"]
_TAIL = ["atrim=start_sample={d}", "asetpts=PTS-STARTPTS"]


def _wrapped(delay: int, stages: list[str]) -> list[str]:
    return ([a.format(d=delay) for a in _COMP] + stages
            + [a.format(d=delay) for a in _TAIL])


def test_rnnoise_model_is_bundled():
    assert Path(audiofx.RNNOISE_MODEL).exists()
    assert Path(audiofx.RNNOISE_MODEL).stat().st_size > 100_000


def test_clip_chain_fixed_order_and_stage_skip():
    chain = audiofx.clip_chain({"deess": True, "compress": True, "level": True,
                                "eq": {"preset": "voice"}, "denoise": True})
    joined = ",".join(chain)
    # denoise → eq → compress → deess → level regardless of dict order.
    assert joined.index("afftdn") < joined.index("highpass")
    assert joined.index("equalizer") < joined.index("acompressor")
    assert joined.index("acompressor") < joined.index("deesser")
    assert joined.index("deesser") < joined.index("dynaudnorm")

    assert audiofx.clip_chain(None) == []
    assert audiofx.clip_chain({"denoise": False, "eq": None, "level": 0}) == []


def test_denoise_variants_are_latency_compensated():
    # afftdn delays 25 ms (1200 samples at 48 kHz), rnnoise 10 ms (480):
    # pad before, trim after, same length out, content on time.
    assert audiofx.clip_chain({"denoise": True}) == _wrapped(1200, ["afftdn=nr=12:nf=-30"])
    assert audiofx.clip_chain(
        {"denoise": {"strength": 24, "floor_db": -40}}) == _wrapped(1200, ["afftdn=nr=24:nf=-40"])
    voice = audiofx.clip_chain({"denoise": "voice"})
    assert voice == _wrapped(480, [f"arnndn=m={audiofx.RNNOISE_MODEL}"])
    assert audiofx.stage_latency_ms({"denoise": "voice"}) == 10.0
    assert audiofx.stage_latency_ms({"denoise": True, "compress": True}) == 25.0
    assert audiofx.stage_latency_ms({"eq": {"preset": "voice"}}) == 0.0


def test_denoise_mix_is_the_voice_model_wet_share():
    partial = audiofx.clip_chain({"denoise": 0.4})
    assert partial == _wrapped(480, [f"arnndn=m={audiofx.RNNOISE_MODEL}:mix=0.4"])
    assert audiofx.clip_chain({"denoise": 1}) == audiofx.clip_chain({"denoise": "voice"})
    assert audiofx.clip_chain({"denoise": 0}) == []
    explicit = audiofx.clip_chain({"denoise": {"mode": "voice", "mix": 0.5}})
    assert explicit[2].endswith(":mix=0.5")
    with pytest.raises(ValueError):
        audiofx.clip_chain({"denoise": 1.5})


def test_stages_without_delay_get_no_compensation():
    chain = audiofx.clip_chain({"eq": {"preset": "music"}, "compress": True,
                                "deess": True, "level": True})
    assert not any(f.startswith(("apad", "atrim", "aresample")) for f in chain)


def test_level_defaults_and_params():
    [flt] = audiofx.clip_chain({"level": True})
    # 4.2 s window → 21 frames of 200 ms; 28 dB → ×25.1; −6 dBFS → 0.501.
    assert flt == "dynaudnorm=f=200:g=21:p=0.9:m=25.1189:r=0.501187"
    [flt] = audiofx.clip_chain({"level": {"window": 1.5, "max_gain_db": 12,
                                          "target_db": -12}})
    assert flt.startswith("dynaudnorm=f=200:g=7:")     # 7.5 frames → odd 7
    assert ":m=3.98107:" in flt and flt.endswith(":r=0.251189")


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


def test_master_chain_limiter_cancels_its_lookahead():
    chain = audiofx.master_chain({"limiter": True, "gain_db": -3})
    assert chain == ["alimiter=limit=0.891251:level=disabled:latency=1"]  # -1 dBTP
    chain = audiofx.master_chain({"limiter": {"ceiling_db": -3},
                                  "eq": {"preset": "warm"}, "compress": True})
    joined = ",".join(chain)
    assert joined.index("bass") < joined.index("acompressor")
    assert chain[-1] == "alimiter=limit=0.707946:level=disabled:latency=1"
    assert audiofx.master_chain({"gain_db": -3}) == []


def test_enhance_chain_presets_and_overrides():
    voice = audiofx.enhance_chain("voice")
    assert voice[:2] == ["aresample=48000", "apad=pad_len=480"]
    assert voice[2].startswith("arnndn=")
    assert voice[-3:-1] == ["atrim=start_sample=480", "asetpts=PTS-STARTPTS"]
    assert voice[-1] == "alimiter=limit=0.977237:level=disabled:latency=1"
    music = audiofx.enhance_chain("music")
    assert not any(f.startswith(("afftdn", "arnndn", "deesser", "apad")) for f in music)

    no_dn = audiofx.enhance_chain("voice", {"denoise": False})
    assert not any(f.startswith(("afftdn", "arnndn", "apad", "atrim")) for f in no_dn)
    partial = audiofx.enhance_chain("voice", {"denoise": 0.3, "level": True})
    assert any(f.endswith(":mix=0.3") for f in partial)
    assert any(f.startswith("dynaudnorm") for f in partial)
    assert ",".join(partial).index("dynaudnorm") < ",".join(partial).index("alimiter")

    with pytest.raises(ValueError):
        audiofx.enhance_chain("podcast")


# ---------------------------------------------------------------------------
# Loudness normalization modes
# ---------------------------------------------------------------------------

_MEAS_DYNAMIC = {"input_i": "-14.77", "input_tp": "-1.45", "input_lra": "6.30",
                 "input_thresh": "-25.00", "target_offset": "0.60"}
# +6 dB of gain lands the −9 dBTP peak at −3: inside the −1.5 ceiling.
_MEAS_LINEAR = {"input_i": "-20.00", "input_tp": "-9.00", "input_lra": "6.20",
                "input_thresh": "-30.50", "target_offset": "0.40"}


def test_loudnorm_config_defaults_and_mode():
    assert audiofx.loudnorm_config(True) == {"i": -14.0, "tp": -1.5, "lra": 11.0,
                                             "mode": "auto"}
    cfg = audiofx.loudnorm_config({"target_lufs": -16, "mode": "linear"})
    assert cfg["i"] == -16.0 and cfg["mode"] == "linear"
    with pytest.raises(ValueError):
        audiofx.loudnorm_config({"mode": "loud"})


def test_loudnorm_predict_replicates_ffmpeg_rule():
    cfg = audiofx.loudnorm_config(True)
    # The real Day-2 pass 1: +0.77 dB of gain would put −1.45 dBTP at −0.68.
    pred = audiofx.loudnorm_predict(cfg, _MEAS_DYNAMIC)
    assert pred["normalization_type"] == "dynamic"
    assert "true peak" in pred["reason"]
    assert pred["gain_db"] == 0.77
    pred = audiofx.loudnorm_predict(cfg, _MEAS_LINEAR)
    assert pred["normalization_type"] == "linear" and pred["reason"] is None
    wide = dict(_MEAS_LINEAR, input_lra="18.0")
    assert "LRA" in audiofx.loudnorm_predict(cfg, wide)["reason"]


def test_loudnorm_pass2_modes():
    cfg = audiofx.loudnorm_config(True)
    auto, rep = audiofx.loudnorm_pass2(cfg, _MEAS_DYNAMIC)
    assert auto.startswith("loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-14.77")
    assert ":linear=true,aresample=48000" in auto
    assert rep["normalization_type"] == "dynamic" and rep["mode"] == "auto"
    assert audiofx.loudnorm_warning(rep).startswith("loudness normalization ran in DYNAMIC")

    lin, rep = audiofx.loudnorm_pass2(dict(cfg, mode="linear"), _MEAS_DYNAMIC)
    assert lin == ("volume=0.77dB,aresample=192000,alimiter=limit=0.841395"
                   ":attack=5:release=50:level=disabled:latency=1,aresample=48000")
    assert rep["normalization_type"] == "linear"
    assert rep["limiter_db"] == 0.82           # −0.68 dBTP over the −1.5 ceiling
    assert audiofx.loudnorm_warning(rep) is None

    dyn, rep = audiofx.loudnorm_pass2(dict(cfg, mode="dynamic"), _MEAS_LINEAR)
    assert ":linear=false,aresample=48000" in dyn
    assert rep["normalization_type"] == "dynamic"
    assert audiofx.loudnorm_warning(rep) is None   # asked for explicitly

    ok, rep = audiofx.loudnorm_pass2(cfg, _MEAS_LINEAR)
    assert rep["normalization_type"] == "linear"
    assert "→ linear" in audiofx.loudnorm_summary(rep)


def test_parse_loudnorm_json_takes_the_last_block():
    stderr = ('progress {"frame": 1}\n{\n"input_i" : "-30.0",\n"input_tp" : "-9.0"\n}\n'
              '{\n"input_i" : "-23.10",\n"input_tp" : "-4.50",\n"input_lra" : "6.20",\n'
              '"input_thresh" : "-33.50",\n"target_offset" : "0.40"\n}\n')
    meas = audiofx.parse_loudnorm_json(stderr)
    assert meas["input_i"] == "-23.10"
    assert audiofx.parse_loudnorm_json("no json") is None
    assert audiofx.measured_is_silent({"input_i": "-inf"})
    assert audiofx.measured_is_silent({"input_i": "-84.3"})
    assert not audiofx.measured_is_silent(meas)
