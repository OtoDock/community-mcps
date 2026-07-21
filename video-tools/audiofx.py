"""Audio sweetening: per-clip denoise/eq/compress/deess chains + master bus.

Pure filter-string builders (compiler-importable, no I/O beyond locating the
bundled rnnoise model at import). Chain order is fixed and classic:
denoise → eq → compress → deess. The clip field:

  audio: {
    denoise: true | "voice" | {strength: dB},   # afftdn / arnndn (voice model)
    eq: {preset: voice|music|bright|warm|telephone} | {bands: [{f, gain_db,
        width_hz?|q?}]},                         # biquad equalizer chain
    compress: true | {threshold_db, ratio, attack, release, makeup_db},
    deess: true | {intensity: 0–1},
  }

Master bus (audio_master): eq / compress (same shapes) + limiter:
true | {ceiling_db} → alimiter as a true-peak safety, useful when loudnorm
is off. EQ presets use the plain `equalizer`/`bass`/`treble`/`highpass`/
`lowpass` biquads — anequalizer would need every band duplicated per
channel for stereo.
"""

from pathlib import Path

# Bundled voice model (models/README.md carries provenance/licensing).
RNNOISE_MODEL = str(Path(__file__).resolve().parent / "models" / "rnnoise-voice.rnnn")

AUDIO_KEYS = ("denoise", "eq", "compress", "deess")

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


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def _lin(db: float) -> str:
    return _f(10 ** (float(db) / 20.0))


def _denoise_filters(spec) -> list[str]:
    if spec == "voice":
        return [f"arnndn=m={RNNOISE_MODEL}"]
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


_BUILDERS = {
    "denoise": _denoise_filters,
    "eq": _eq_filters,
    "compress": _compress_filters,
    "deess": _deess_filters,
}


def clip_chain(spec: dict | None) -> list[str]:
    """The per-clip sweetening chain in fixed order. Falsy stage values
    (false/None) are skipped so a stage can be explicitly disabled."""
    if not spec:
        return []
    out: list[str] = []
    for key in AUDIO_KEYS:
        val = spec.get(key)
        if val:
            out += _BUILDERS[key](val)
    return out


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
        out.append(f"alimiter=limit={_lin(ceiling)}:level=disabled")
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


def enhance_chain(preset: str, overrides: dict | None = None) -> list[str]:
    if preset not in ENHANCE_PRESETS:
        raise ValueError(f"preset must be one of {sorted(ENHANCE_PRESETS)}")
    spec = dict(ENHANCE_PRESETS[preset])
    for key in AUDIO_KEYS:
        if overrides and key in overrides:
            spec[key] = overrides[key]
    return clip_chain(spec) + ["alimiter=limit=0.977:level=disabled"]
