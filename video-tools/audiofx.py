"""Audio sweetening: per-clip denoise/eq/compress/deess/level chains, the
master bus, and the loudness-normalization modes.

Pure filter-string builders (compiler-importable, no I/O beyond locating the
bundled rnnoise model at import). Chain order is fixed and classic:
denoise → eq → compress → deess → level. The clip field:

  audio: {
    denoise: true | "voice" | 0–1 | {strength: dB, floor_db?}
             | {mode: "voice", mix: 0–1},   # afftdn / arnndn (voice model)
    eq: {preset: voice|music|bright|warm|telephone} | {bands: [{f, gain_db,
        width_hz?|q?}]},                     # biquad equalizer chain
    compress: true | {threshold_db, ratio, attack, release, makeup_db},
    deess: true | {intensity: 0–1},
    level: true | {window, max_gain_db, target_db},   # dynaudnorm leveller
  }

Master bus (audio_master): eq / compress (same shapes) + limiter:
true | {ceiling_db} → alimiter as a true-peak safety, useful when loudnorm
is off. EQ presets use the plain `equalizer`/`bass`/`treble`/`highpass`/
`lowpass` biquads — anequalizer would need every band duplicated per
channel for stereo.

Latency. Two stages delay the signal and do NOT flush that delay at EOF
(output length unchanged, content late, tail lost — measured on ffmpeg
7.0.2 and 7.1.5): rnnoise by 480 samples (10 ms, it forces 48 kHz) and
afftdn by 25 ms at any rate. alimiter's 5 ms lookahead is cancelled by its
own `latency=1`. Every chain that contains a delaying stage is built
latency-neutral: pinned to 48 kHz, padded by the delay BEFORE the stages
and trimmed by the same amount after them, so the output lines up
sample-exact with the input and keeps its length (the whole voice enhance
chain ran 15 ms late until 0.4.3 — a sync bug for every unaligned VO).
"""

import json
import math
import re
from pathlib import Path

# Bundled voice model (models/README.md carries provenance/licensing).
RNNOISE_MODEL = str(Path(__file__).resolve().parent / "models" / "rnnoise-voice.rnnn")

AUDIO_KEYS = ("denoise", "eq", "compress", "deess", "level")

# The rate every compensated chain pins, and the measured per-stage delays
# at that rate.
CHAIN_RATE = 48000
_STAGE_LATENCY = {"arnndn": 480, "afftdn": 1200}

EQ_PRESETS = {
    # Spoken word: rumble cut, mud dip, presence, air.
    "voice": ["highpass=f=80", "equalizer=f=300:t=q:w=1.5:g=-2",
              "equalizer=f=3000:t=q:w=1.2:g=2.5", "treble=g=1.5:f=8000"],
    # Gentle smile curve for music beds.
    "music": ["bass=g=1.5:f=100", "treble=g=1.5:f=8000"],
    "bright": ["treble=g=2.5:f=6000"],
    "warm": ["bass=g=2:f=200", "treble=g=-1.5:f=6000"],
    # Stylistic band-limit (radio/phone voice).
    "telephone": ["highpass=f=300", "lowpass=f=3400"],
}

_COMPRESS_DEFAULTS = {"threshold_db": -18.0, "ratio": 3.0, "attack": 20.0,
                      "release": 250.0, "makeup_db": 3.0}

# Leveller defaults: dynaudnorm in RMS-target mode — 200 ms frames over a
# 4.2 s Gaussian window (g=21), up to 28 dB of gain, −6 dBFS RMS target.
# Validated by ear on a real interview VO with a 12 dB phrase swing (a
# compressor barely narrowed it; speechnorm widened it).
_LEVEL_DEFAULTS = {"window": 4.2, "max_gain_db": 28.0, "target_db": -6.0}
_LEVEL_FRAME_MS = 200


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def _lin(db: float) -> str:
    return _f(10 ** (float(db) / 20.0))


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _denoise_filters(spec) -> list[str]:
    mix = None
    if spec == "voice":
        mix = 1.0
    elif _is_number(spec):
        mix = float(spec)
    elif isinstance(spec, dict) and (spec.get("mode") == "voice" or "mix" in spec):
        mix = float(spec.get("mix", 1.0))
    if mix is not None:
        if not 0.0 <= mix <= 1.0:
            raise ValueError("denoise mix must be 0–1")
        if mix == 0.0:
            return []
        # `mix` blends the filtered signal with the input INSIDE the filter,
        # on aligned samples (measured: mix 0 / 0.5 / 1 all carry the same
        # 10 ms delay) — no comb filtering, one atom.
        suffix = "" if mix == 1.0 else f":mix={_f(mix)}"
        return [f"arnndn=m={RNNOISE_MODEL}{suffix}"]
    strength, floor = 12.0, -30.0
    if isinstance(spec, dict):
        if spec.get("strength") is not None:
            strength = float(spec["strength"])
        if spec.get("floor_db") is not None:
            floor = float(spec["floor_db"])
    # nf seeds afftdn's noise-floor estimate. The default (-50) treats real
    # recording hiss (-30ish) as signal and reduces nothing; -30 measured
    # -11.8 dB of floor reduction with zero signal loss on quiet material.
    # (tn=1 noise tracking DEFEATS the reduction — measured, don't add it.)
    return [f"afftdn=nr={_f(strength)}:nf={_f(floor)}"]


def _eq_filters(spec) -> list[str]:
    if not isinstance(spec, dict):
        raise ValueError("eq must be {preset: name} or {bands: [...]}")
    if spec.get("preset"):
        return list(EQ_PRESETS[spec["preset"]])
    out = []
    for band in spec.get("bands", []):
        opts = [f"f={_f(band['f'])}"]
        if band.get("width_hz") is not None:
            opts.append(f"t=h:width={_f(band['width_hz'])}")
        else:
            opts.append(f"t=q:w={_f(band.get('q', 1.0))}")
        opts.append(f"g={_f(band['gain_db'])}")
        out.append("equalizer=" + ":".join(opts))
    return out


def _compress_filters(spec) -> list[str]:
    p = dict(_COMPRESS_DEFAULTS)
    if isinstance(spec, dict):
        p.update({k: float(spec[k]) for k in _COMPRESS_DEFAULTS if k in spec})
    return [(f"acompressor=threshold={_lin(p['threshold_db'])}"
             f":ratio={_f(p['ratio'])}:attack={_f(p['attack'])}"
             f":release={_f(p['release'])}:makeup={_lin(p['makeup_db'])}")]


def _deess_filters(spec) -> list[str]:
    intensity = 0.12
    if isinstance(spec, dict) and spec.get("intensity") is not None:
        intensity = float(spec["intensity"])
    return [f"deesser=i={_f(intensity)}"]


def _level_filters(spec) -> list[str]:
    p = dict(_LEVEL_DEFAULTS)
    if isinstance(spec, dict):
        p.update({k: float(spec[k]) for k in _LEVEL_DEFAULTS if spec.get(k) is not None})
    # Gaussian size is in frames and must be odd (the window rounds down to
    # the odd frame count it covers).
    g = max(3, int(p["window"] * 1000 / _LEVEL_FRAME_MS) | 1)
    return [(f"dynaudnorm=f={_LEVEL_FRAME_MS}:g={g}:p=0.9"
             f":m={_f(10 ** (p['max_gain_db'] / 20.0))}"
             f":r={_lin(p['target_db'])}")]


_BUILDERS = {
    "denoise": _denoise_filters,
    "eq": _eq_filters,
    "compress": _compress_filters,
    "deess": _deess_filters,
    "level": _level_filters,
}


def stage_latency_samples(filters: list[str]) -> int:
    """Samples (at CHAIN_RATE) the given stage filters delay the signal by."""
    return sum(_STAGE_LATENCY.get(f.split("=", 1)[0], 0) for f in filters)


def stage_latency_ms(spec: dict | None) -> float:
    """The delay the chain's stages would introduce uncompensated — what
    `clip_chain` cancels (0 when no delaying stage is present)."""
    return stage_latency_samples(_stages(spec)) * 1000.0 / CHAIN_RATE


def _stages(spec: dict | None) -> list[str]:
    if not spec:
        return []
    out: list[str] = []
    for key in AUDIO_KEYS:
        val = spec.get(key)
        if val:
            out += _BUILDERS[key](val)
    return out


def compensated(filters: list[str]) -> list[str]:
    """Wrap delaying stages so the chain is latency-neutral: pin the rate
    (the delays are known in 48 kHz samples), pad the input by the delay,
    trim the same amount off the head, restart the timestamps. Chains
    without a delaying stage pass through untouched."""
    delay = stage_latency_samples(filters)
    if not delay:
        return list(filters)
    return ([f"aresample={CHAIN_RATE}", f"apad=pad_len={delay}"] + list(filters)
            + [f"atrim=start_sample={delay}", "asetpts=PTS-STARTPTS"])


def clip_chain(spec: dict | None) -> list[str]:
    """The per-clip sweetening chain in fixed order, latency-neutral. Falsy
    stage values (false/None/0) are skipped so a stage can be explicitly
    disabled."""
    return compensated(_stages(spec))


def limiter_filter(ceiling_db: float) -> str:
    """Sample-peak safety at a ceiling; `latency=1` cancels the lookahead
    delay (5 ms uncompensated — measured)."""
    return f"alimiter=limit={_lin(ceiling_db)}:level=disabled:latency=1"


def master_chain(master: dict | None) -> list[str]:
    """Master-bus eq/compress/limiter (runs before loudness normalization —
    the limiter is the true-peak safety when loudnorm is off)."""
    if not master:
        return []
    out: list[str] = []
    if master.get("eq"):
        out += _eq_filters(master["eq"])
    if master.get("compress"):
        out += _compress_filters(master["compress"])
    limiter = master.get("limiter")
    if limiter:
        ceiling = -1.0
        if isinstance(limiter, dict) and limiter.get("ceiling_db") is not None:
            ceiling = float(limiter["ceiling_db"])
        out.append(limiter_filter(ceiling))
    return out


# enhance_audio presets (edit_video): a finished one-shot chain with a
# safety limiter — EQ boosts + makeup gain must not clip the file.
ENHANCE_PRESETS = {
    "voice": {"denoise": "voice", "eq": {"preset": "voice"},
              "compress": True, "deess": True},
    # No denoise for music: broadband reduction eats cymbals/air.
    "music": {"eq": {"preset": "music"},
              "compress": {"threshold_db": -14, "ratio": 2,
                           "attack": 25, "release": 300, "makeup_db": 1.5}},
}


def enhance_spec(preset: str, overrides: dict | None = None) -> dict:
    if preset not in ENHANCE_PRESETS:
        raise ValueError(f"preset must be one of {sorted(ENHANCE_PRESETS)}")
    spec = dict(ENHANCE_PRESETS[preset])
    for key in AUDIO_KEYS:
        if overrides and key in overrides:
            spec[key] = overrides[key]
    return spec


def enhance_chain(preset: str, overrides: dict | None = None) -> list[str]:
    return clip_chain(enhance_spec(preset, overrides)) + [limiter_filter(-0.2)]


# ---------------------------------------------------------------------------
# Loudness normalization — the three modes and ffmpeg's two-pass contract
# ---------------------------------------------------------------------------
#
# loudnorm with `linear=true` + measured_* only stays linear when the static
# gain lands the true peak under TP AND the measured LRA is within the
# target LRA (af_loudnorm.c, config_input); otherwise it silently runs its
# DYNAMIC leveller — a program compressor that rewrote a real mix by 11 dB
# and halved its LRA. `predict` replicates that rule from the pass-1
# numbers; `linear` mode never enters the leveller at all.

LOUDNORM_MODES = ("auto", "linear", "dynamic")
_LOUDNORM_DEFAULTS = {"i": -14.0, "tp": -1.5, "lra": 11.0}
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)
# Below this integrated level the mix is treated as silent: loudnorm
# rejects non-finite measured values outright and "normalizing" silence
# would only amplify the noise floor.
SILENCE_LUFS = -70.0


def loudnorm_config(spec) -> dict:
    """`audio_master.loudnorm` (true | {target_lufs, true_peak, lra, mode})
    → {i, tp, lra, mode}."""
    opts = spec if isinstance(spec, dict) else {}
    mode = str(opts.get("mode", "auto"))
    if mode not in LOUDNORM_MODES:
        raise ValueError(f"loudnorm mode must be one of {LOUDNORM_MODES}")
    return {
        "i": float(opts.get("target_lufs", _LOUDNORM_DEFAULTS["i"])),
        "tp": float(opts.get("true_peak", _LOUDNORM_DEFAULTS["tp"])),
        "lra": float(opts.get("lra", _LOUDNORM_DEFAULTS["lra"])),
        "mode": mode,
    }


def loudnorm_measure_filter(cfg: dict) -> str:
    return (f"loudnorm=I={_f(cfg['i'])}:TP={_f(cfg['tp'])}:LRA={_f(cfg['lra'])}"
            ":print_format=json")


def parse_loudnorm_json(stderr: str) -> dict | None:
    """The LAST loudnorm JSON block in an ffmpeg stderr (progress lines can
    contain braces), or None."""
    m = None
    for m in _LOUDNORM_JSON.finditer(stderr):
        pass
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def measured_is_silent(meas: dict) -> bool:
    try:
        i = float(meas["input_i"])
    except (KeyError, TypeError, ValueError):
        return True
    return not math.isfinite(i) or i < SILENCE_LUFS


def loudnorm_predict(cfg: dict, meas: dict) -> dict:
    """Which branch ffmpeg's two-pass loudnorm will take for these pass-1
    numbers, with the reason when it is the dynamic one."""
    mi, mtp, mlra = float(meas["input_i"]), float(meas["input_tp"]), float(meas["input_lra"])
    thresh = float(meas.get("input_thresh", -70.0))
    gain = cfg["i"] - mi
    tp_after = mtp + gain
    reasons = []
    if tp_after > cfg["tp"]:
        reasons.append(f"a static {gain:+.1f} dB would put the true peak at "
                       f"{tp_after:+.1f} dBTP (target {cfg['tp']:+.1f})")
    if mlra > cfg["lra"]:
        reasons.append(f"measured LRA {mlra:.1f} LU exceeds the {cfg['lra']:.0f} LU target")
    if mtp == 99 or thresh == -70 or mlra == 0 or mi == 0:
        # A constant-level source measures LRA 0.0 — ffmpeg reads that (and
        # a −70 threshold, a 99 peak, a 0 integrated) as "not measured" and
        # never takes the linear branch for it.
        reasons.append("pass-1 values ffmpeg treats as unmeasured "
                       f"(I {mi:g}, TP {mtp:g}, LRA {mlra:g}, thresh {thresh:g})")
    return {
        "normalization_type": "dynamic" if reasons else "linear",
        "gain_db": round(gain, 2),
        "tp_after_gain": round(tp_after, 2),
        "reason": "; ".join(reasons) if reasons else None,
    }


def _measured_dict(meas: dict) -> dict:
    return {"i": float(meas["input_i"]), "tp": float(meas["input_tp"]),
            "lra": float(meas["input_lra"]),
            "thresh": float(meas.get("input_thresh", -70.0))}


def loudnorm_pass2(cfg: dict, meas: dict) -> tuple[str, dict]:
    """The pass-2 filter chain for the configured mode + a report:
    {mode, normalization_type, measured, target, gain_db, reason,
    limiter_db}. Every chain ends at CHAIN_RATE (loudnorm emits 192 kHz)."""
    mode = cfg.get("mode", "auto")
    pred = loudnorm_predict(cfg, meas)
    report = {
        "mode": mode,
        "measured": _measured_dict(meas),
        "target": {"i": cfg["i"], "tp": cfg["tp"], "lra": cfg["lra"]},
        "gain_db": pred["gain_db"],
        "reason": None,
        "limiter_db": 0.0,
    }
    if mode == "linear":
        # Static gain to the target, then a true-peak ceiling: 4× oversampled
        # so the limiter sees inter-sample peaks (1× overshoots a −1.5 dBTP
        # ceiling to −0.7 — measured), back to the timeline rate.
        over = max(0.0, pred["tp_after_gain"] - cfg["tp"])
        chain = (f"volume={_f(pred['gain_db'])}dB,aresample=192000,"
                 f"alimiter=limit={_lin(cfg['tp'])}:attack=5:release=50"
                 f":level=disabled:latency=1,aresample={CHAIN_RATE}")
        report.update(normalization_type="linear", limiter_db=round(over, 2))
        return chain, report
    linear = "true" if mode == "auto" else "false"
    chain = (f"loudnorm=I={_f(cfg['i'])}:TP={_f(cfg['tp'])}:LRA={_f(cfg['lra'])}"
             f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
             f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
             f":offset={meas.get('target_offset', 0)}:linear={linear}"
             f",aresample={CHAIN_RATE}")
    if mode == "dynamic":
        report.update(normalization_type="dynamic")
    else:
        report.update(normalization_type=pred["normalization_type"],
                      reason=pred["reason"])
    return chain, report


def loudnorm_warning(report: dict) -> str | None:
    """The warning text for a report whose auto mode fell back to dynamic."""
    if report.get("mode") != "auto" or report.get("normalization_type") != "dynamic":
        return None
    return ("loudness normalization ran in DYNAMIC mode (a program compressor "
            "that reshapes the mix's dynamics): " + str(report.get("reason"))
            + ". For a static gain that keeps the authored dynamics set "
            "audio_master.loudnorm.mode to \"linear\" (true-peak limiter at "
            "the ceiling), or widen lra.")


def loudnorm_summary(report: dict) -> str:
    """One line for tool results."""
    m, t = report["measured"], report["target"]
    ntype = report["normalization_type"]
    line = (f"loudnorm {report['mode']} → {ntype}: measured {m['i']:.1f} LUFS / "
            f"{m['tp']:+.1f} dBTP / LRA {m['lra']:.1f} → target {t['i']:.0f} LUFS / "
            f"{t['tp']:+.1f} dBTP (gain {report['gain_db']:+.1f} dB")
    if report["mode"] == "linear" and report["limiter_db"] > 0:
        line += f", limiter trimmed up to {report['limiter_db']:.1f} dB of peaks"
    return line + ")"
