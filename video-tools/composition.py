"""Composition schema (.vproj.json): load/save, timeline math, validation,
and the edit-operation set.

The composition is the artifact the agent edits — a declarative JSON document
compiled to an ffmpeg filtergraph at render time. Schema shape:

{
  "version": 1,
  "project": {"width": 1080, "height": 1920, "fps": 30,
               "background": "#000000", "color": {...global grade}},
  "tracks": [
    {"kind": "video", "clips": [...]},      # exactly one: the base timeline
    {"kind": "overlay", "clips": [...]},    # 0+: positioned over the base
    {"kind": "audio", "clips": [...]}       # 0+: music/VO/SFX beds
  ],
  "captions": {"source": "...", "preset": "karaoke", ...} | null,
  "audio_master": {"gain_db": 0, "loudnorm": true | {...}}
}

Base-track clips are strictly SEQUENTIAL (no gaps, no explicit start) — the
timeline position of each clip is computed, with `transition_in` overlapping
it into the previous clip (xfade semantics). Overlay/audio clips carry an
explicit `start` on the final timeline.

Validation is two-level: structural (no filesystem access — unit-testable)
plus asset-aware when a `media_info` map (path → probe summary) is supplied.
"""

import copy
import json
from pathlib import Path

import captions as captions_mod
import color as color_mod
import speedramp as speedramp_mod

SCHEMA_VERSION = 1

TRACK_KINDS = ("video", "overlay", "audio")

# ffmpeg xfade transition names (7.x set) + "cut" as the explicit no-transition.
XFADE_TRANSITIONS = frozenset({
    "fade", "fadeblack", "fadewhite", "fadegrays", "distance", "dissolve",
    "pixelize", "radial", "hblur", "zoomin",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circlecrop", "rectcrop", "circleclose", "circleopen",
    "horzclose", "horzopen", "vertclose", "vertopen",
    "diagbl", "diagbr", "diagtl", "diagtr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "squeezev", "squeezeh",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
})

# Wow presets (transitions.py): edge styling around a HARD CUT — no
# overlap, so compute_timeline gives them zero transition duration.
PRESET_TRANSITIONS = ("whip_pan", "zoom_punch", "flash_cut", "glitch",
                      "spin", "shake", "zoom_out", "zoom_in")
# Overlapping wow presets: xfade core (normal overlap semantics) + edge fx.
XFADE_PRESET_TRANSITIONS = ("whip_left", "whip_right", "luma_wipe")

CAPTION_POSITIONS = ("lower_third", "center", "top")
FIT_MODES = ("cover", "contain")

_CLIP_KEYS = {
    "src", "image", "fill", "in", "out", "start", "duration", "speed",
    "speed_ramp", "fit", "transform", "color", "transition_in", "volume_db",
    "mute", "gain_db", "fade_in", "fade_out", "duck", "effects", "mask",
    "label", "stabilize", "interpolate", "audio", "vignette", "grain",
    "sharpen", "motion_blur",
}

# Filmic finishing knobs: key → the name of its options-object field.
FINISH_KEYS = {"vignette": "strength", "grain": "strength", "sharpen": "amount"}

INTERP_MODES = ("flow", "blend", "duplicate")

MATCH_KEYS = ("ref", "ramp_from", "ramp_to", "strength", "target_time")

# Mirror audiofx.py (kept local: this module stays import-light).
AUDIO_FX_KEYS = ("denoise", "eq", "compress", "deess")
EQ_PRESET_NAMES = ("voice", "music", "bright", "warm", "telephone")
_COMPRESS_BOUNDS = {"threshold_db": (-60, 0), "ratio": (1, 20),
                    "attack": (0.01, 2000), "release": (10, 9000),
                    "makeup_db": (0, 24)}

# Mirrors stab.PRESETS (kept local: this module stays import-light).
STAB_STRENGTHS = ("low", "medium", "high")
_TRANSFORM_KEYS = {"scale", "pos", "opacity", "rotate", "keyframes"}

# Overlay-clip effects (keying only makes sense where alpha survives).
EFFECT_TYPES = ("chromakey", "colorkey", "despill")


class CompositionError(ValueError):
    pass


# ---------------------------------------------------------------------------
# I/O + construction
# ---------------------------------------------------------------------------


def load_composition(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise CompositionError(f"Composition file not found: {path}")
    try:
        comp = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompositionError(f"Composition is not valid JSON: {exc}")
    if not isinstance(comp, dict):
        raise CompositionError("Composition root must be a JSON object")
    return comp


def save_composition(path: str, comp: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(comp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def new_composition(project: dict | None = None) -> dict:
    proj = {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "background": "#000000",
    }
    if project:
        proj.update({k: v for k, v in project.items() if v is not None})
    return {
        "version": SCHEMA_VERSION,
        "project": proj,
        "tracks": [{"kind": "video", "clips": []}],
        "captions": None,
        "audio_master": {"gain_db": 0, "loudnorm": True},
    }


# ---------------------------------------------------------------------------
# Timeline math
# ---------------------------------------------------------------------------


def clip_source_kind(clip: dict) -> str:
    """'media' | 'image' | 'fill' — exactly one source field must be set.

    ('fill' is a solid-color clip: {"fill": "#0a0a12", "duration": 2}.
    The 'color' key is the GRADE, valid on any clip kind.)
    """
    present = [k for k in ("src", "image", "fill") if clip.get(k)]
    if len(present) != 1:
        raise CompositionError(
            f"clip must have exactly one of src/image/fill (got {present or 'none'}) — "
            'e.g. {"src": "workspace/clip.mp4", "in": 2, "out": 7} for media, '
            '{"image": "workspace/photo.jpg", "duration": 3} for a still, '
            '{"fill": "#0a0a12", "duration": 2} for a solid'
        )
    return {"src": "media", "image": "image", "fill": "fill"}[present[0]]


def clip_duration(clip: dict, media_info: dict | None = None) -> float:
    """Duration this clip occupies on the timeline (post-speed)."""
    kind = clip_source_kind(clip)
    speed = float(clip.get("speed", 1.0))
    if kind in ("image", "fill"):
        dur = clip.get("duration")
        if dur is None:
            raise CompositionError(f"{kind} clip needs an explicit duration")
        return float(dur)
    cin = float(clip.get("in", 0.0))
    cout = clip.get("out")
    if cout is None:
        if media_info is None or clip["src"] not in media_info:
            raise CompositionError(
                f"clip '{clip['src']}' has no 'out' and its duration is unknown — "
                "set 'out' or let the server probe the file"
            )
        cout = float(media_info[clip["src"]].get("duration", 0.0))
    span = float(cout) - cin
    if span <= 0:
        raise CompositionError(
            f"clip '{clip.get('src')}' has non-positive span (in={cin}, out={cout})"
        )
    ramp = clip.get("speed_ramp")
    if ramp is not None:
        try:
            return speedramp_mod.output_duration(span, ramp)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            raise CompositionError(
                f"clip '{clip.get('src')}' has an invalid speed_ramp — "
                'needs {"from": 1.0, "to": 0.25, "curve": '
                '"linear|ease_in|ease_out"} with speeds 0.1–4.0')
    return span / speed


def base_track(comp: dict) -> dict:
    for t in comp.get("tracks", []):
        if t.get("kind") == "video":
            return t
    raise CompositionError("composition has no base 'video' track")


def compute_timeline(comp: dict, media_info: dict | None = None) -> dict:
    """Resolve timeline placement.

    Returns {"duration": float, "base": [{"index", "start", "end",
    "duration", "transition"}...]}. Base clips are sequential; a
    transition_in of duration D overlaps the clip D seconds into its
    predecessor (xfade), shortening the total.
    """
    clips = base_track(comp).get("clips", [])
    placed = []
    cursor = 0.0
    for i, clip in enumerate(clips):
        d = clip_duration(clip, media_info)
        trans = clip.get("transition_in") or None
        tdur = 0.0
        if (trans and i > 0
                and trans.get("type", "cut") not in ("cut",) + PRESET_TRANSITIONS):
            tdur = float(trans.get("duration", 0.5))
        start = cursor - tdur
        placed.append({
            "index": i,
            "start": round(start, 6),
            "end": round(start + d, 6),
            "duration": round(d, 6),
            "transition": (trans if tdur > 0 else None),
        })
        cursor = start + d
    return {"duration": round(cursor, 6) if placed else 0.0, "base": placed}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _issue(issues: list, level: str, where: str, message: str) -> None:
    issues.append({"level": level, "where": where, "message": message})


def _num(value, lo, hi) -> bool:
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False


def _validate_keyframes(kfs, where: str, issues: list, is_overlay: bool) -> None:
    if not isinstance(kfs, list) or len(kfs) < 2:
        _issue(issues, "error", where, "keyframes need a list of ≥2 entries")
        return
    animated: set[str] = set()
    for kf in kfs:
        if isinstance(kf, dict):
            animated |= set(kf) & {"scale", "pos"}
    if not animated:
        _issue(issues, "error", where,
               "keyframes must animate scale and/or pos: [{t, scale?, pos?}, …]")
        return
    if is_overlay and "scale" in animated:
        _issue(issues, "error", where,
               "overlay scale keyframes are not supported — animate pos, or "
               "bake the zoom into the overlay media")
        return
    last_t = -1.0
    for i, kf in enumerate(kfs):
        w = f"{where}[{i}]"
        if not isinstance(kf, dict) or not _num(kf.get("t"), 0, 86400):
            _issue(issues, "error", w, "each keyframe needs t ≥ 0 (clip-local seconds)")
            return
        if float(kf["t"]) <= last_t:
            _issue(issues, "error", w, "keyframe t values must be increasing")
            return
        last_t = float(kf["t"])
        missing = animated - set(kf)
        if missing:
            _issue(issues, "error", w,
                   f"animated properties must appear in EVERY keyframe "
                   f"(missing {sorted(missing)})")
            return
        if "scale" in kf:
            lo = 1.0 if not is_overlay else 0.05
            if not _num(kf["scale"], lo, 8.0):
                _issue(issues, "error", w,
                       f"keyframe scale must be {lo}–8.0"
                       + (" (zoompan cannot zoom below 1 on base clips)"
                          if not is_overlay else ""))
        if "pos" in kf:
            pos = kf["pos"]
            if (not isinstance(pos, (list, tuple)) or len(pos) != 2
                    or not all(_num(v, -8192, 8192) for v in pos)):
                _issue(issues, "error", w, "keyframe pos must be [x, y]")


def _validate_transform(tr: dict, where: str, issues: list, is_overlay: bool) -> None:
    if not isinstance(tr, dict):
        _issue(issues, "error", where, "transform must be an object")
        return
    unknown = set(tr) - _TRANSFORM_KEYS
    if unknown:
        _issue(issues, "warning", where,
               f"unknown transform keys ignored: {sorted(unknown)} "
               f"(accepted: {sorted(_TRANSFORM_KEYS)})")
    if "keyframes" in tr and tr["keyframes"] is not None:
        _validate_keyframes(tr["keyframes"], where + ".keyframes", issues, is_overlay)
        for static in ("scale", "pos"):
            if static in tr:
                _issue(issues, "error", where,
                       f"static '{static}' conflicts with keyframes — "
                       "put the starting value in the first keyframe instead")
    if "rotate" in tr:
        if not is_overlay:
            _issue(issues, "error", where,
                   "rotate is supported on overlay clips only")
        elif not _num(tr["rotate"], -360, 360):
            _issue(issues, "error", where, "rotate must be -360–360 degrees")
    if "scale" in tr and not _num(tr["scale"], 0.05, 8.0):
        _issue(issues, "error", where, "transform.scale must be 0.05–8.0")
    if not is_overlay and _num(tr.get("scale", 1.0), 0.05, 8.0) and float(tr.get("scale", 1.0)) < 1.0:
        _issue(issues, "warning", where,
               "base-clip scale < 1.0 leaves background bars — intended?")
    pos = tr.get("pos")
    if pos is not None:
        if (not isinstance(pos, (list, tuple)) or len(pos) != 2
                or not all(_num(v, -8192, 8192) for v in pos)):
            _issue(issues, "error", where,
                   "transform.pos must be [x, y] pixel offsets of the clip "
                   "CENTER from the canvas center")
    if "opacity" in tr:
        if not _num(tr["opacity"], 0.0, 1.0):
            _issue(issues, "error", where, "transform.opacity must be 0–1")
        elif not is_overlay:
            _issue(issues, "warning", where,
                   "opacity on a base clip blends against the background color")


def _validate_color_field(spec, where: str, issues: list,
                          exists=None) -> None:
    if spec is None:
        return
    if not isinstance(spec, dict):
        _issue(issues, "error", where, "color must be an object")
        return
    for problem in color_mod.validate_color_spec(spec):
        _issue(issues, "error", where, problem)
    lut = spec.get("lut")
    if lut and exists is not None and not color_mod.is_builtin_look(lut):
        if not exists(lut):
            _issue(issues, "error", where,
                   f"LUT file not found: {lut} (or use a built-in look: "
                   f"{', '.join(sorted(color_mod.BUILTIN_LOOKS))})")


def _validate_eq(eq, where: str, issues: list) -> None:
    if not isinstance(eq, dict):
        _issue(issues, "error", where,
               'eq must be an object — e.g. {"preset": "voice"} or '
               '{"bands": [{"f": 3000, "gain_db": 2.5}]}')
        return
    preset = eq.get("preset")
    if preset is not None:
        if preset not in EQ_PRESET_NAMES:
            _issue(issues, "error", where,
                   f"eq preset must be one of {EQ_PRESET_NAMES}")
        return
    bands = eq.get("bands")
    if not isinstance(bands, list) or not bands:
        _issue(issues, "error", where,
               'eq needs a preset or bands — e.g. {"preset": "voice"} or '
               '{"bands": [{"f": 3000, "gain_db": 2.5, "q": 1.2}]}')
        return
    for bi, band in enumerate(bands):
        bw = f"{where}.bands[{bi}]"
        if not isinstance(band, dict) or not _num(band.get("f"), 20, 20000):
            _issue(issues, "error", bw, "each band needs f (20–20000 Hz)")
            continue
        if not _num(band.get("gain_db"), -24, 24):
            _issue(issues, "error", bw, "gain_db must be -24–+24")
        if "width_hz" in band and not _num(band["width_hz"], 1, 10000):
            _issue(issues, "error", bw, "width_hz must be 1–10000")
        if "q" in band and not _num(band["q"], 0.1, 10):
            _issue(issues, "error", bw, "q must be 0.1–10")


def _validate_compress(spec, where: str, issues: list) -> None:
    if spec is True or spec is False:
        return
    if not isinstance(spec, dict):
        _issue(issues, "error", where,
               "compress must be true or {threshold_db, ratio, attack, "
               "release, makeup_db}")
        return
    for k, (lo, hi) in _COMPRESS_BOUNDS.items():
        if k in spec and not _num(spec[k], lo, hi):
            _issue(issues, "error", where, f"compress.{k} must be {lo}–{hi}")
    unknown = set(spec) - set(_COMPRESS_BOUNDS)
    if unknown:
        _issue(issues, "warning", where,
               f"unknown compress keys ignored: {sorted(unknown)}")


def _validate_audiofx(spec: dict, where: str, issues: list) -> None:
    unknown = set(spec) - set(AUDIO_FX_KEYS)
    if unknown:
        _issue(issues, "warning", where,
               f"unknown audio keys ignored: {sorted(unknown)} "
               f"(accepted: {list(AUDIO_FX_KEYS)})")
    dn = spec.get("denoise")
    if dn is not None and not isinstance(dn, bool) and dn != "voice":
        if isinstance(dn, dict):
            if "strength" in dn and not _num(dn["strength"], 1, 40):
                _issue(issues, "error", where,
                       "denoise.strength must be 1–40 (dB of reduction)")
            if "floor_db" in dn and not _num(dn["floor_db"], -80, -20):
                _issue(issues, "error", where,
                       "denoise.floor_db must be -80–-20 (assumed noise floor)")
        else:
            _issue(issues, "error", where,
                   "denoise must be true, \"voice\" (speech model), or "
                   "{strength: dB, floor_db?}")
    if spec.get("eq") is not None:
        _validate_eq(spec["eq"], where + ".eq", issues)
    if spec.get("compress") is not None:
        _validate_compress(spec["compress"], where + ".compress", issues)
    de = spec.get("deess")
    if de is not None and not isinstance(de, bool):
        if isinstance(de, dict):
            if "intensity" in de and not _num(de["intensity"], 0, 1):
                _issue(issues, "error", where, "deess.intensity must be 0–1")
        else:
            _issue(issues, "error", where,
                   "deess must be true or {intensity: 0–1}")


def _parse_match_ref(ref) -> tuple[str, float]:
    """'path@seconds' → (path, seconds). Raises ValueError on bad syntax.
    (Duplicated in colormatch.parse_ref — this module stays import-light.)"""
    if not isinstance(ref, str) or "@" not in ref:
        raise ValueError("must be 'path@seconds' (the reference frame to match)")
    path, _, t = ref.rpartition("@")
    try:
        time = float(t)
    except ValueError:
        raise ValueError(f"time after '@' must be a number (got {t!r})")
    if not path or time < 0:
        raise ValueError("must be 'path@seconds' with seconds ≥ 0")
    return path, time


def _validate_match(match, kind: str, skind: str, where: str, issues: list,
                    exists=None) -> None:
    if kind != "video":
        _issue(issues, "error", where,
               "color.match applies to base-track clips only")
        return
    if skind != "media":
        _issue(issues, "error", where, "color.match needs a media 'src' clip")
        return
    if not isinstance(match, dict):
        _issue(issues, "error", where,
               'match must be an object — e.g. {"ref": "workspace/a.mp4@11.8"} '
               'or bridge mode {"ramp_from": "workspace/a.mp4@11.9", '
               '"ramp_to": "workspace/b.mp4@0.1"}')
        return
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        _issue(issues, "warning", where,
               f"unknown match keys ignored: {sorted(unknown)}")
    has_single = match.get("ref") is not None
    has_ramp = (match.get("ramp_from") is not None
                or match.get("ramp_to") is not None)
    if has_single == has_ramp:
        _issue(issues, "error", where,
               'match needs EITHER ref: "path@seconds" (single reference) OR '
               "ramp_from + ramp_to (both — grade-ramp between two "
               "references, the AI-bridge join fix)")
        return
    if has_ramp and (match.get("ramp_from") is None
                     or match.get("ramp_to") is None):
        _issue(issues, "error", where,
               "ramp mode needs BOTH ramp_from and ramp_to")
        return
    keys = ("ref",) if has_single else ("ramp_from", "ramp_to")
    for key in keys:
        try:
            path, _ = _parse_match_ref(match[key])
        except ValueError as exc:
            _issue(issues, "error", f"{where}.{key}", str(exc))
            continue
        if exists is not None and not exists(path):
            _issue(issues, "error", f"{where}.{key}",
                   f"reference file not found: {path}")
    if "strength" in match and not _num(match["strength"], 0, 1):
        _issue(issues, "error", where, "strength must be 0–1")
    if "target_time" in match and not _num(match["target_time"], 0, 86400):
        _issue(issues, "error", where,
               "target_time must be ≥ 0 (source seconds of THIS clip to sample)")
    if has_ramp and "target_time" in match:
        _issue(issues, "warning", where,
               "target_time is ignored in ramp mode (endpoints are sampled)")


def _validate_finish(container: dict, where: str, issues: list,
                     kind: str | None = None) -> None:
    """vignette/grain/sharpen knobs on a clip (base only) or the project."""
    for key, field in FINISH_KEYS.items():
        val = container.get(key)
        if val is None or val is False:
            continue
        where_k = f"{where}.{key}"
        if kind is not None and kind != "video":
            _issue(issues, "error", where_k,
                   f"{key} applies to base-track clips (or project-wide)")
            continue
        if val is True:
            continue
        if isinstance(val, dict):
            if field in val and not _num(val[field], 0, 1):
                _issue(issues, "error", where_k, f"{field} must be 0–1")
            unknown = set(val) - {field}
            if unknown:
                _issue(issues, "warning", where_k,
                       f"unknown keys ignored: {sorted(unknown)}")
        elif not _num(val, 0, 1):
            _issue(issues, "error", where_k,
                   f"{key} must be true, 0–1, or {{{field}: 0–1}}")


def _validate_clip(clip: dict, kind: str, where: str, issues: list,
                   exists=None, media_info: dict | None = None,
                   project_fps: float = 30.0) -> None:
    if not isinstance(clip, dict):
        _issue(issues, "error", where, "clip must be an object")
        return
    unknown = set(clip) - _CLIP_KEYS
    if unknown:
        _issue(issues, "warning", where,
               f"unknown clip keys ignored: {sorted(unknown)}")
    try:
        skind = clip_source_kind(clip)
    except CompositionError as exc:
        _issue(issues, "error", where, str(exc))
        return

    if kind == "audio" and skind != "media":
        _issue(issues, "error", where, "audio-track clips need a media 'src'")
        return

    if skind == "media":
        if exists is not None and not exists(clip["src"]):
            _issue(issues, "error", where, f"file not found: {clip['src']}")
        elif media_info is not None and clip["src"] in media_info:
            mi = media_info[clip["src"]]
            if kind in ("video", "overlay") and not mi.get("has_video"):
                _issue(issues, "error", where,
                       f"'{clip['src']}' has no video stream")
            if kind == "audio" and not mi.get("has_audio"):
                _issue(issues, "error", where,
                       f"'{clip['src']}' has no audio stream")
            cout = clip.get("out")
            dur = mi.get("duration", 0.0)
            if cout is not None and dur and float(cout) > dur + 0.05:
                _issue(issues, "error", where,
                       f"out={cout} exceeds source duration {dur:.2f}s")
        cin, cout = clip.get("in", 0.0), clip.get("out")
        if not _num(cin, 0, 86400):
            _issue(issues, "error", where, "'in' must be ≥ 0 seconds")
        if cout is not None and float(cout) <= float(cin or 0):
            _issue(issues, "error", where, f"'out' ({cout}) must be > 'in' ({cin})")
    else:
        if not _num(clip.get("duration"), 0.05, 3600):
            _issue(issues, "error", where,
                   f"{skind} clip needs duration 0.05–3600s")
        if skind == "image" and exists is not None and not exists(clip["image"]):
            _issue(issues, "error", where, f"file not found: {clip['image']}")

    speed = clip.get("speed", 1.0)
    if not _num(speed, 0.1, 4.0):
        _issue(issues, "error", where, "speed must be 0.1–4.0")
    elif not 0.5 <= float(speed) <= 2.0 and float(speed) != 1.0:
        _issue(issues, "warning", where,
               f"speed {speed} is aggressive — audio pitch artifacts likely"
               + ("; mute or replace the audio for extreme slow motion"
                  if float(speed) < 0.5 else ""))

    # The slowest point the clip reaches — a ramp's slow endpoint drives the
    # interpolate/judder checks exactly like a constant slow speed would.
    eff_slow = float(speed) if _num(speed, 0.1, 4.0) else 1.0
    ramp = clip.get("speed_ramp")
    if ramp is not None:
        where_r = where + ".speed_ramp"
        if kind != "video":
            _issue(issues, "error", where_r,
                   "speed_ramp applies to base-track clips only")
        elif skind != "media":
            _issue(issues, "error", where_r,
                   "speed_ramp needs a media 'src' clip")
        elif not isinstance(ramp, dict) or not {"from", "to"} <= set(ramp):
            _issue(issues, "error", where_r,
                   'speed_ramp must be {"from": 1.0, "to": 0.25, "curve": '
                   '"linear|ease_in|ease_out"} — compiled as '
                   f"{speedramp_mod.SEGMENTS} constant-speed segments")
        else:
            ok = True
            for k in ("from", "to"):
                if not _num(ramp.get(k), 0.1, 4.0):
                    _issue(issues, "error", where_r, f"'{k}' must be 0.1–4.0")
                    ok = False
            if ramp.get("curve", "linear") not in speedramp_mod.RAMP_CURVES:
                _issue(issues, "error", where_r,
                       f"curve must be one of {speedramp_mod.RAMP_CURVES}")
            unknown = set(ramp) - {"from", "to", "curve"}
            if unknown:
                _issue(issues, "warning", where_r,
                       f"unknown speed_ramp keys ignored: {sorted(unknown)}")
            if "speed" in clip:
                _issue(issues, "error", where_r,
                       "speed_ramp replaces 'speed' — remove one of the two")
            tr_kfs = (clip.get("transform") or {})
            if isinstance(tr_kfs, dict) and tr_kfs.get("keyframes"):
                _issue(issues, "error", where_r,
                       "speed_ramp cannot combine with transform.keyframes — "
                       "keyframes are clip-local output time and the "
                       "segmentation would restart the motion in every "
                       "segment")
            col_m = clip.get("color")
            if isinstance(col_m, dict) and col_m.get("match") is not None:
                _issue(issues, "error", where_r,
                       "speed_ramp cannot combine with color.match — match "
                       "the shot first (edit_video op match_color), then "
                       "ramp the matched file")
            if ok:
                lo = min(float(ramp["from"]), float(ramp["to"]))
                eff_slow = min(eff_slow, lo)
                if float(ramp["from"]) == float(ramp["to"]):
                    _issue(issues, "warning", where_r,
                           "from == to is a constant speed — use 'speed'")
                if lo < 0.5 and not clip.get("mute"):
                    _issue(issues, "warning", where_r,
                           "ramping below 0.5x — the audio steps through "
                           "tempo changes at each segment; mute the clip and "
                           "lay music over the ramp")

    interp = clip.get("interpolate")
    if interp is not None:
        if interp not in INTERP_MODES:
            _issue(issues, "error", where,
                   f"interpolate must be one of {INTERP_MODES}")
        elif kind == "audio":
            _issue(issues, "error", where,
                   "interpolate applies to video/overlay clips only")
        elif eff_slow >= 1.0:
            _issue(issues, "warning", where,
                   "interpolate only affects slow motion (speed < 1) — ignored")
    # Slow motion needs project_fps/speed frames per source second: warn when
    # the source can't supply them and nothing will synthesize the rest.
    if (skind == "media" and kind in ("video", "overlay")
            and eff_slow < 1.0
            and interp in (None, "duplicate")
            and media_info is not None):
        src_fps = float((media_info.get(clip.get("src")) or {}).get("fps") or 0)
        needed = project_fps / eff_slow
        if src_fps and src_fps < needed - 0.01:
            _issue(issues, "warning", where,
                   f"speed {eff_slow:g} needs {needed:.0f} fps of source motion "
                   f"but '{clip.get('src')}' has {src_fps:.0f} — frames will "
                   "duplicate (judder). Set interpolate: 'flow' (best, slow "
                   "first render) or 'blend' (fast, motion-smear), or shoot "
                   "60/120 fps for planned slow motion")

    if clip.get("fit") is not None and clip["fit"] not in FIT_MODES:
        _issue(issues, "error", where, f"fit must be one of {FIT_MODES}")

    stab = clip.get("stabilize")
    if stab is not None and stab is not False:
        where_s = where + ".stabilize"
        if kind == "audio":
            _issue(issues, "error", where_s,
                   "stabilize applies to video/overlay clips only")
        elif skind != "media":
            _issue(issues, "error", where_s,
                   "stabilize needs a media 'src' clip — stills and fills "
                   "have no camera shake")
        elif isinstance(stab, dict):
            if stab.get("strength", "medium") not in STAB_STRENGTHS:
                _issue(issues, "error", where_s,
                       f"strength must be one of {STAB_STRENGTHS}")
            if "smoothing" in stab and not _num(stab["smoothing"], 1, 100):
                _issue(issues, "error", where_s,
                       "smoothing must be 1–100 (frames of camera-path averaging)")
            if "zoom" in stab and not _num(stab["zoom"], -5, 40):
                _issue(issues, "error", where_s,
                       "zoom must be -5–40 (extra zoom-in percent)")
            unknown = set(stab) - {"strength", "smoothing", "zoom"}
            if unknown:
                _issue(issues, "warning", where_s,
                       f"unknown stabilize keys ignored: {sorted(unknown)}")
        elif stab is not True:
            _issue(issues, "error", where_s,
                   'stabilize must be true or e.g. {"strength": "high"} '
                   "(low|medium|high, smoothing?, zoom?)")

    effects = clip.get("effects")
    if effects:
        if kind != "overlay":
            _issue(issues, "error", where,
                   "effects (keying) apply to overlay clips only — alpha "
                   "does not survive on the base track")
        elif not isinstance(effects, list):
            _issue(issues, "error", where, "effects must be a list")
        else:
            for ei, eff in enumerate(effects):
                ew = f"{where}.effects[{ei}]"
                if not isinstance(eff, dict) or eff.get("type") not in EFFECT_TYPES:
                    _issue(issues, "error", ew,
                           f"effect type must be one of {EFFECT_TYPES}")
                    continue
                if eff["type"] in ("chromakey", "colorkey"):
                    if "similarity" in eff and not _num(eff["similarity"], 0.01, 1.0):
                        _issue(issues, "error", ew, "similarity must be 0.01–1")
                    if "blend" in eff and not _num(eff["blend"], 0.0, 1.0):
                        _issue(issues, "error", ew, "blend must be 0–1")

    mask = clip.get("mask")
    if mask is not None:
        if kind != "overlay":
            _issue(issues, "error", where, "mask applies to overlay clips only")
        elif not isinstance(mask, dict) or not mask.get("image"):
            _issue(issues, "error", where,
                   "mask must be {\"image\": path} — a grayscale image "
                   "(white = visible, black = hidden)")
        elif exists is not None and not exists(mask["image"]):
            _issue(issues, "error", where, f"mask image not found: {mask['image']}")

    if "transform" in clip and clip["transform"] is not None:
        _validate_transform(clip["transform"], where + ".transform", issues,
                            is_overlay=(kind == "overlay"))
    if kind != "audio":
        _validate_color_field(clip.get("color"), where + ".color", issues, exists)
        col = clip.get("color")
        if isinstance(col, dict) and col.get("match") is not None:
            _validate_match(col["match"], kind, skind, where + ".color.match",
                            issues, exists)

    for f in ("volume_db", "gain_db"):
        if f in clip and not _num(clip[f], -60, 12):
            _issue(issues, "error", where, f"{f} must be -60–+12 dB")
    for f in ("fade_in", "fade_out"):
        if f in clip and not _num(clip[f], 0, 10):
            _issue(issues, "error", where, f"{f} must be 0–10 s")

    if kind in ("overlay", "audio"):
        if kind == "audio":
            example = '{"src": "workspace/music.mp3", "start": 0, "gain_db": -10}'
        else:
            example = ('{"image": "workspace/logo.png", "start": 2, '
                       '"duration": 3, "fade_in": 0.3}')
        if not _num(clip.get("start", None), 0, 86400):
            _issue(issues, "error", where,
                   f"{kind} clips need 'start' ≥ 0 on the timeline — e.g. {example}")
    elif "start" in clip:
        _issue(issues, "warning", where,
               "base-track clips are sequential — 'start' is ignored")

    _validate_finish(clip, where, issues, kind=kind)

    mb = clip.get("motion_blur")
    if mb is not None and mb is not False:
        where_m = where + ".motion_blur"
        if kind != "video":
            _issue(issues, "error", where_m,
                   "motion_blur applies to base-track clips only")
        elif skind != "media":
            _issue(issues, "error", where_m,
                   "motion_blur needs a media 'src' clip")
        elif isinstance(mb, dict):
            if "strength" in mb and not _num(mb["strength"], 0, 1):
                _issue(issues, "error", where_m, "strength must be 0–1")
        elif mb is not True and not _num(mb, 0, 1):
            _issue(issues, "error", where_m,
                   "motion_blur must be true, 0–1, or {strength: 0–1}")

    afx = clip.get("audio")
    if afx is not None:
        where_a = where + ".audio"
        if kind == "overlay":
            _issue(issues, "error", where_a,
                   "the audio chain applies to base/audio clips — overlays "
                   "are video-only")
        elif skind != "media":
            _issue(issues, "error", where_a,
                   "the audio chain needs a media 'src' clip")
        elif not isinstance(afx, dict):
            _issue(issues, "error", where_a,
                   'audio must be an object — e.g. {"denoise": true, '
                   '"eq": {"preset": "voice"}, "compress": true, "deess": true}')
        else:
            _validate_audiofx(afx, where_a, issues)
            if clip.get("mute"):
                _issue(issues, "warning", where_a,
                       "clip is muted — the audio chain has no effect")
            elif (media_info is not None and clip.get("src") in media_info
                    and not media_info[clip["src"]].get("has_audio")):
                _issue(issues, "warning", where_a,
                       f"'{clip['src']}' has no audio stream — the audio "
                       "chain has no effect")

    duck = clip.get("duck")
    if duck is not None:
        if kind != "audio":
            _issue(issues, "error", where, "duck applies to audio-track clips only")
        elif isinstance(duck, dict):
            bounds = {"threshold": (0.001, 1.0), "ratio": (1, 20),
                      "attack": (0.1, 2000), "release": (10, 9000)}
            for k, (lo, hi) in bounds.items():
                if k in duck and not _num(duck[k], lo, hi):
                    _issue(issues, "error", where, f"duck.{k} must be {lo}–{hi}")
            unknown = set(duck) - set(bounds)
            if unknown:
                _issue(issues, "warning", where,
                       f"unknown duck keys ignored: {sorted(unknown)}")
        elif not isinstance(duck, bool):
            _issue(issues, "error", where, "duck must be true or an options object")

    trans = clip.get("transition_in")
    if trans is not None:
        if kind != "video":
            _issue(issues, "error", where,
                   "transition_in applies to base-track clips only")
        elif isinstance(trans, dict):
            ttype = trans.get("type", "fade")
            if (ttype != "cut" and ttype not in XFADE_TRANSITIONS
                    and ttype not in PRESET_TRANSITIONS
                    and ttype not in XFADE_PRESET_TRANSITIONS):
                _issue(issues, "error", where,
                       f"unknown transition '{ttype}' — valid: cut, wow "
                       f"presets ({', '.join(PRESET_TRANSITIONS + XFADE_PRESET_TRANSITIONS)}), "
                       "or xfade: " + ", ".join(sorted(XFADE_TRANSITIONS)))
            tdur = trans.get("duration", 0.5)
            if ttype in PRESET_TRANSITIONS:
                if not _num(tdur, 0.05, 2.5):
                    _issue(issues, "error", where,
                           f"'{ttype}' duration must be 0.05–2.5 s "
                           "(social pacing wants 0.2–0.4)")
            elif not _num(tdur, 0.05, 5.0):
                _issue(issues, "error", where,
                       "transition duration must be 0.05–5 s")
            flash = trans.get("flash")
            if flash is not None:
                if flash not in ("black", "white"):
                    _issue(issues, "error", where,
                           'flash must be "black" (default) or "white"')
                elif ttype not in ("flash_cut", "zoom_punch"):
                    _issue(issues, "warning", where,
                           "flash only affects flash_cut and zoom_punch")
        else:
            _issue(issues, "error", where, "transition_in must be an object")


def validate(comp: dict, exists=None, media_info: dict | None = None) -> list[dict]:
    """Validate a composition. Returns a list of issues
    ``{"level": "error"|"warning", "where": str, "message": str}`` — empty
    means fully valid. Pass ``exists``/``media_info`` for asset-aware checks;
    omit for pure structural validation.
    """
    issues: list[dict] = []
    if not isinstance(comp, dict):
        return [{"level": "error", "where": "root", "message": "composition must be an object"}]

    if comp.get("version") != SCHEMA_VERSION:
        _issue(issues, "error", "version",
               f"unsupported composition version {comp.get('version')!r} "
               f"(expected {SCHEMA_VERSION})")

    proj = comp.get("project")
    if not isinstance(proj, dict):
        _issue(issues, "error", "project",
               'missing project object — e.g. "project": '
               '{"width": 1920, "height": 1080, "fps": 30, '
               '"background": "#000000"} (1080x1920 for vertical)')
        return issues
    w, h, fps = proj.get("width"), proj.get("height"), proj.get("fps", 30)
    if not _num(w, 16, 4096) or not _num(h, 16, 4096):
        _issue(issues, "error", "project", "width/height must be 16–4096")
    else:
        if int(w) % 2 or int(h) % 2:
            _issue(issues, "error", "project",
                   "width/height must be even (yuv420p requirement)")
    if not _num(fps, 1, 120):
        _issue(issues, "error", "project", "fps must be 1–120")
    _validate_color_field(proj.get("color"), "project.color", issues, exists)
    if isinstance(proj.get("color"), dict) and proj["color"].get("match") is not None:
        _issue(issues, "error", "project.color",
               "match is per-clip shot matching — it cannot apply to the "
               "whole project")
    _validate_finish(proj, "project", issues)
    lb = proj.get("letterbox")
    if lb is not None:
        try:
            ratio = float(lb)
        except (TypeError, ValueError):
            _issue(issues, "error", "project",
                   'letterbox must be an aspect number — e.g. "2.39" '
                   "(cinemascope) or \"1.85\"")
        else:
            if not 1.0 <= ratio <= 4.0:
                _issue(issues, "error", "project", "letterbox must be 1.0–4.0")
            elif (_num(w, 16, 4096) and _num(h, 16, 4096)
                    and (float(h) - float(w) / ratio) / 2 < 2):
                _issue(issues, "warning", "project",
                       f"letterbox {ratio} adds no bars on a "
                       f"{w}x{h} canvas (already at least that wide)")

    tracks = comp.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        _issue(issues, "error", "tracks",
               'composition needs a tracks array — minimal shape: '
               '{"project": {"width": 1920, "height": 1080, "fps": 30}, '
               '"tracks": [{"kind": "video", "clips": '
               '[{"src": "workspace/clip.mp4"}]}]}')
        return issues
    video_tracks = [t for t in tracks if isinstance(t, dict) and t.get("kind") == "video"]
    if len(video_tracks) != 1:
        _issue(issues, "error", "tracks",
               f"exactly one 'video' (base) track is required, found {len(video_tracks)}")
    for ti, track in enumerate(tracks):
        where_t = f"tracks[{ti}]"
        if not isinstance(track, dict) or track.get("kind") not in TRACK_KINDS:
            _issue(issues, "error", where_t,
                   f"track kind must be one of {TRACK_KINDS} — each track is "
                   '{"kind": "video", "clips": [...]} (exactly one video/base '
                   "track; overlay and audio tracks are optional)")
            continue
        clips = track.get("clips", [])
        if not isinstance(clips, list):
            _issue(issues, "error", where_t, "clips must be an array")
            continue
        for ci, clip in enumerate(clips):
            _validate_clip(clip, track["kind"], f"{where_t}.clips[{ci}]",
                           issues, exists, media_info,
                           project_fps=float(fps) if _num(fps, 1, 120) else 30.0)

    if video_tracks and not video_tracks[0].get("clips"):
        _issue(issues, "error", "tracks", "the base video track has no clips")

    # Transition-vs-neighbor-duration feasibility + total timeline (only
    # meaningful once per-clip checks passed and durations are resolvable).
    if not any(i["level"] == "error" for i in issues):
        try:
            tl = compute_timeline(comp, media_info)
            base = tl["base"]
            clips_v = base_track(comp).get("clips", [])

            # A ramped clip only offers its EDGE SEGMENT to a neighboring
            # transition — the segmentation cuts it into 6, and neither an
            # xfade overlap nor preset styling can cross a cut.
            def _edge_room(idx: int, side: str) -> float:
                c = clips_v[idx]
                ramp = c.get("speed_ramp")
                if not isinstance(ramp, dict):
                    return base[idx]["duration"]
                cin = float(c.get("in", 0.0))
                cout = c.get("out")
                if cout is None:
                    cout = ((media_info or {}).get(c.get("src")) or {}).get("duration")
                    if cout is None:
                        return base[idx]["duration"]
                first, last = speedramp_mod.edge_durations(float(cout) - cin, ramp)
                return first if side == "head" else last

            for i, entry in enumerate(base):
                trans = entry["transition"]
                if not trans:
                    continue
                tdur = float(trans.get("duration", 0.5))
                room = min(_edge_room(i - 1, "tail"), _edge_room(i, "head"))
                if tdur >= room:
                    _issue(issues, "error", f"tracks[video].clips[{i}]",
                           f"transition duration {tdur}s must be shorter than "
                           f"both neighbors (shortest offers {room:.2f}s"
                           + ("; for a ramped clip that is its edge segment, "
                              "1/6 of the clip)"
                              if room != min(base[i - 1]["duration"],
                                             entry["duration"]) else ")"))
            # Preset transitions carry no overlap (not in entry["transition"])
            # but each side still needs room for its half-window of styling.
            for i, clip in enumerate(clips_v):
                tr = clip.get("transition_in") or {}
                if i > 0 and tr.get("type") in PRESET_TRANSITIONS:
                    half = float(tr.get("duration", 0.3)) / 2
                    room = min(_edge_room(i - 1, "tail"), _edge_room(i, "head"))
                    if half > room:
                        _issue(issues, "error", f"tracks[video].clips[{i}]",
                               f"'{tr['type']}' needs {half:.2f}s of styling on "
                               f"each side of the cut — a neighbor offers only "
                               f"{room:.2f}s (for a ramped clip that is its "
                               "edge segment, 1/6 of the clip)")
            total = tl["duration"]
            for ti, track in enumerate(tracks):
                if track.get("kind") in ("overlay", "audio"):
                    for ci, clip in enumerate(track.get("clips", [])):
                        start = float(clip.get("start", 0))
                        if total and start >= total:
                            _issue(issues, "warning", f"tracks[{ti}].clips[{ci}]",
                                   f"starts at {start}s — beyond the base "
                                   f"timeline end ({total:.2f}s)")
        except CompositionError as exc:
            _issue(issues, "error", "timeline", str(exc))

    caps = comp.get("captions")
    if caps is not None:
        if not isinstance(caps, dict):
            _issue(issues, "error", "captions", "captions must be an object")
        else:
            src = caps.get("source", "")
            ext = Path(str(src)).suffix.lower()
            if ext not in (".json", ".srt", ".ass"):
                _issue(issues, "error", "captions",
                       "captions.source must be a .transcript.json, .srt, or .ass file")
            elif exists is not None and not exists(src):
                _issue(issues, "error", "captions", f"file not found: {src}")
            preset = caps.get("preset", captions_mod.DEFAULT_PRESET)
            if preset not in captions_mod.PRESETS:
                _issue(issues, "error", "captions",
                       f"unknown preset '{preset}' — valid: "
                       + ", ".join(sorted(captions_mod.PRESETS)))
            if caps.get("position", "lower_third") not in CAPTION_POSITIONS:
                _issue(issues, "error", "captions",
                       f"position must be one of {CAPTION_POSITIONS}")
            if "font_size" in caps and caps["font_size"] is not None \
                    and not _num(caps["font_size"], 8, 300):
                _issue(issues, "error", "captions", "font_size must be 8–300")
            if "max_words_per_cue" in caps and not _num(caps.get("max_words_per_cue"), 1, 12):
                _issue(issues, "error", "captions", "max_words_per_cue must be 1–12")

    master = comp.get("audio_master")
    if master is not None and isinstance(master, dict):
        if "gain_db" in master and not _num(master["gain_db"], -60, 12):
            _issue(issues, "error", "audio_master", "gain_db must be -60–+12 dB")
        if master.get("eq") is not None:
            _validate_eq(master["eq"], "audio_master.eq", issues)
        if master.get("compress") is not None:
            _validate_compress(master["compress"], "audio_master.compress", issues)
        limiter = master.get("limiter")
        if limiter is not None and not isinstance(limiter, bool):
            if isinstance(limiter, dict):
                if "ceiling_db" in limiter and not _num(limiter["ceiling_db"], -9, 0):
                    _issue(issues, "error", "audio_master",
                           "limiter.ceiling_db must be -9–0 dBTP")
            else:
                _issue(issues, "error", "audio_master",
                       "limiter must be true or {ceiling_db: -1}")
        ln = master.get("loudnorm", True)
        if isinstance(ln, dict):
            if "target_lufs" in ln and not _num(ln["target_lufs"], -30, -8):
                _issue(issues, "error", "audio_master", "target_lufs must be -30–-8")
            if "true_peak" in ln and not _num(ln["true_peak"], -9, 0):
                _issue(issues, "error", "audio_master", "true_peak must be -9–0 dBTP")
        elif not isinstance(ln, bool):
            _issue(issues, "error", "audio_master",
                   "loudnorm must be true/false or an options object")

    return issues


def format_issues(issues: list[dict]) -> str:
    if not issues:
        return "Composition is valid — no issues."
    lines = []
    for i in issues:
        lines.append(f"[{i['level'].upper()}] {i['where']}: {i['message']}")
    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = len(issues) - errors
    lines.append(f"— {errors} error(s), {warnings} warning(s)")
    return "\n".join(lines)


def media_paths(comp: dict) -> list[str]:
    """Every filesystem path the composition references (media, images,
    caption source, LUT files) — for pre-probing and resolution."""
    paths: list[str] = []
    for track in comp.get("tracks", []):
        for clip in track.get("clips", []) if isinstance(track, dict) else []:
            for key in ("src", "image"):
                if isinstance(clip, dict) and clip.get(key):
                    paths.append(clip[key])
            if isinstance(clip, dict) and isinstance(clip.get("mask"), dict) \
                    and clip["mask"].get("image"):
                paths.append(clip["mask"]["image"])
            if isinstance(clip, dict) and isinstance(clip.get("color"), dict):
                lut = clip["color"].get("lut")
                if lut and not color_mod.is_builtin_look(lut):
                    paths.append(lut)
                match = clip["color"].get("match")
                if isinstance(match, dict):
                    for key in ("ref", "ramp_from", "ramp_to"):
                        ref = match.get(key)
                        if isinstance(ref, str) and "@" in ref:
                            paths.append(ref.rpartition("@")[0])
    proj_color = comp.get("project", {}).get("color")
    if isinstance(proj_color, dict):
        lut = proj_color.get("lut")
        if lut and not color_mod.is_builtin_look(lut):
            paths.append(lut)
    caps = comp.get("captions")
    if isinstance(caps, dict) and caps.get("source"):
        paths.append(caps["source"])
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Edit operations (continue-on-error, file-tools style)
# ---------------------------------------------------------------------------


def _find_track(comp: dict, selector, create_kind: str | None = None) -> dict:
    tracks = comp.setdefault("tracks", [])
    if isinstance(selector, int):
        if 0 <= selector < len(tracks):
            return tracks[selector]
        raise CompositionError(f"no track at index {selector}")
    kind = str(selector or "video")
    if kind not in TRACK_KINDS:
        raise CompositionError(f"track selector must be an index or one of {TRACK_KINDS}")
    for t in tracks:
        if t.get("kind") == kind:
            return t
    if create_kind and kind == create_kind:
        t = {"kind": kind, "clips": []}
        tracks.append(t)
        return t
    raise CompositionError(f"no '{kind}' track exists")


def _merge_patch(target: dict, patch: dict) -> None:
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _merge_patch(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


def apply_operations(comp: dict, operations: list[dict]) -> tuple[dict, list[str]]:
    """Apply edit operations sequentially; continue on per-op errors.

    Returns ``(comp, results)`` where each result line is either
    ``ok: <summary>`` or ``error: <what went wrong>``.
    """
    from shared import _op_type  # late import: shared pulls httpx

    results: list[str] = []
    for op in operations:
        kind = _op_type(op)
        try:
            if kind == "set_project":
                patch = {k: v for k, v in op.items()
                         if k in ("width", "height", "fps", "background",
                                  "color", "vignette", "grain", "sharpen",
                                  "letterbox")}
                _merge_patch(comp.setdefault("project", {}), patch)
                results.append(f"ok: project updated ({', '.join(patch)})")

            elif kind == "add_track":
                tkind = op.get("kind")
                if tkind not in ("overlay", "audio"):
                    raise CompositionError("add_track kind must be overlay or audio")
                comp.setdefault("tracks", []).append({"kind": tkind, "clips": []})
                results.append(f"ok: added {tkind} track "
                               f"(index {len(comp['tracks']) - 1})")

            elif kind == "remove_track":
                idx = op.get("track")
                if not isinstance(idx, int) or not 0 <= idx < len(comp.get("tracks", [])):
                    raise CompositionError("remove_track needs a valid track index")
                if comp["tracks"][idx].get("kind") == "video":
                    raise CompositionError("the base video track cannot be removed")
                removed = comp["tracks"].pop(idx)
                results.append(f"ok: removed {removed.get('kind')} track {idx}")

            elif kind == "add_clip":
                clip = op.get("clip")
                if not isinstance(clip, dict):
                    raise CompositionError("add_clip needs a clip object")
                track = _find_track(comp, op.get("track", "video"),
                                    create_kind=op.get("track")
                                    if op.get("track") in ("overlay", "audio") else None)
                clips = track.setdefault("clips", [])
                at = op.get("at")
                if at is None or not isinstance(at, int) or at >= len(clips):
                    clips.append(clip)
                    at = len(clips) - 1
                else:
                    clips.insert(max(0, at), clip)
                label = clip.get("src") or clip.get("image") or clip.get("fill", "clip")
                results.append(f"ok: added clip '{label}' at {track['kind']}[{at}]")

            elif kind == "update_clip":
                track = _find_track(comp, op.get("track", "video"))
                idx = op.get("index")
                clips = track.get("clips", [])
                if not isinstance(idx, int) or not 0 <= idx < len(clips):
                    raise CompositionError(
                        f"update_clip: no clip at index {idx} "
                        f"(track has {len(clips)})")
                patch = op.get("patch")
                if not isinstance(patch, dict):
                    raise CompositionError("update_clip needs a patch object")
                _merge_patch(clips[idx], patch)
                results.append(f"ok: updated {track['kind']}[{idx}] "
                               f"({', '.join(patch)})")

            elif kind == "remove_clip":
                track = _find_track(comp, op.get("track", "video"))
                idx = op.get("index")
                clips = track.get("clips", [])
                if not isinstance(idx, int) or not 0 <= idx < len(clips):
                    raise CompositionError(f"remove_clip: no clip at index {idx}")
                clips.pop(idx)
                results.append(f"ok: removed {track['kind']}[{idx}]")

            elif kind == "move_clip":
                track = _find_track(comp, op.get("track", "video"))
                clips = track.get("clips", [])
                src, dst = op.get("from"), op.get("to")
                if not (isinstance(src, int) and 0 <= src < len(clips)):
                    raise CompositionError(f"move_clip: no clip at index {src}")
                if not isinstance(dst, int):
                    raise CompositionError("move_clip needs integer 'to'")
                clip = clips.pop(src)
                clips.insert(max(0, min(dst, len(clips))), clip)
                results.append(f"ok: moved {track['kind']}[{src}] → [{dst}]")

            elif kind == "set_transition":
                track = _find_track(comp, "video")
                idx = op.get("index")
                clips = track.get("clips", [])
                if not isinstance(idx, int) or not 1 <= idx < len(clips):
                    raise CompositionError(
                        "set_transition: index must target clip 1+ "
                        "(the transition INTO that clip)")
                ttype = op.get("transition", op.get("type", "fade"))
                if ttype == "cut":
                    clips[idx].pop("transition_in", None)
                    results.append(f"ok: clip {idx} transition → hard cut")
                else:
                    clips[idx]["transition_in"] = {
                        "type": ttype,
                        "duration": float(op.get("duration", 0.5)),
                    }
                    results.append(
                        f"ok: clip {idx} transition_in → {ttype} "
                        f"({clips[idx]['transition_in']['duration']}s)")

            elif kind == "set_captions":
                if op.get("captions") is None and "source" not in op:
                    comp["captions"] = None
                    results.append("ok: captions removed")
                else:
                    caps = op.get("captions") or {
                        k: v for k, v in op.items()
                        if k in ("source", "preset", "position", "font_size",
                                 "highlight_color", "uppercase",
                                 "max_words_per_cue")}
                    comp["captions"] = caps
                    results.append(f"ok: captions set ({caps.get('source')})")

            elif kind == "set_audio_master":
                patch = {k: v for k, v in op.items() if k in ("gain_db", "loudnorm")}
                _merge_patch(comp.setdefault("audio_master", {}), patch)
                results.append(f"ok: audio_master updated ({', '.join(patch)})")

            else:
                raise CompositionError(
                    f"unknown operation '{kind}' — valid: set_project, add_track, "
                    "remove_track, add_clip, update_clip, remove_clip, move_clip, "
                    "set_transition, set_captions, set_audio_master")
        except CompositionError as exc:
            results.append(f"error: {kind or '(missing type)'}: {exc}")
        except Exception as exc:  # never let one op kill the batch
            results.append(f"error: {kind}: {exc}")
    return comp, results
