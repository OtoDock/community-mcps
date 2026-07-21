"""Speed ramps: ``speed_ramp: {from, to, curve}`` compiled as segments.

ffmpeg has no variable-rate retiming that stays CFR without judder
(a time-varying setpts expression lands frames at irregular PTS and the
fps filter then drops/duplicates unpredictably), so a ramp is compiled the
honest way: N (=6) constant-speed sub-clips over equal SOURCE-time slices,
each retimed exactly like a plain ``speed`` clip. Slow segments route
through the same native-first / blend / flow-mezzanine machinery as
ordinary slow motion. Six steps over a typical 2–6 s ramp means a step
every few output frames — monotone and effectively invisible; motion blur
on the clip smooths the steps further.

The clip keeps its single ``speed_ramp`` field in the .vproj.json; the
expansion below happens on the renderer's RESOLVED copy (and idempotently
inside compile_render, keeping the pure compiler self-sufficient).
"""

import copy

RAMP_CURVES = ("linear", "ease_in", "ease_out")
SEGMENTS = 6

# Segmentation breaks clip-local output-time state: keyframed motion would
# restart per segment, a match ramp would re-run its 0→1 dissolve in every
# slice. Validation rejects these combinations (composition._validate_clip).
_FIRST_ONLY = ("transition_in", "fade_in")
_LAST_ONLY = ("fade_out",)


def _curve(u: float, curve: str) -> float:
    if curve == "ease_in":
        return u * u
    if curve == "ease_out":
        return 1 - (1 - u) * (1 - u)
    return u


def segment_speeds(ramp: dict, n: int = SEGMENTS) -> list[float]:
    """Constant speed per segment, sampled at the segment's SOURCE-time
    midpoint on the curve. Rounded so graph text and downstream cache keys
    (mezzanine, .trf) stay stable across runs."""
    lo, hi = float(ramp["from"]), float(ramp["to"])
    curve = ramp.get("curve", "linear")
    return [round(lo + (hi - lo) * _curve((i + 0.5) / n, curve), 4)
            for i in range(n)]


def output_duration(span: float, ramp: dict, n: int = SEGMENTS) -> float:
    seg = span / n
    return sum(seg / s for s in segment_speeds(ramp, n))


def edge_durations(span: float, ramp: dict, n: int = SEGMENTS) -> tuple[float, float]:
    """(first_segment, last_segment) OUTPUT durations — the room a preset
    transition's edge styling actually has on a ramped clip."""
    speeds = segment_speeds(ramp, n)
    seg = span / n
    return seg / speeds[0], seg / speeds[-1]


def describe(ramp: dict, n: int = SEGMENTS) -> str:
    return (f"{float(ramp['from']):g}x -> {float(ramp['to']):g}x "
            f"({ramp.get('curve', 'linear')}, {n} constant-speed segments)")


def expand_clip(clip: dict, cin: float, cout: float,
                n: int = SEGMENTS) -> list[dict]:
    speeds = segment_speeds(clip["speed_ramp"], n)
    seg = (cout - cin) / n
    out: list[dict] = []
    for i, s in enumerate(speeds):
        c = copy.deepcopy(clip)
        c.pop("speed_ramp", None)
        c["in"] = round(cin + i * seg, 6)
        c["out"] = round(cout if i == n - 1 else cin + (i + 1) * seg, 6)
        c["speed"] = s
        if i > 0:
            for k in _FIRST_ONLY:
                c.pop(k, None)
        if i < n - 1:
            for k in _LAST_ONLY:
                c.pop(k, None)
        c["_ramp"] = {"seg": i, "of": n}
        out.append(c)
    return out


def ramp_notes(comp: dict) -> list[str]:
    """Human summaries of every ramped base clip (renderer warnings)."""
    notes = []
    for track in comp.get("tracks", []):
        if track.get("kind") != "video":
            continue
        for clip in track.get("clips", []):
            if isinstance(clip, dict) and isinstance(clip.get("speed_ramp"), dict):
                notes.append(f"'{clip.get('src')}': speed ramp "
                             + describe(clip["speed_ramp"]))
    return notes


def expand_composition(comp: dict, media_info: dict | None = None,
                       n: int = SEGMENTS) -> dict:
    """Replace every base-track ``speed_ramp`` clip with its segments.

    Returns the SAME object untouched when nothing is ramped (the common
    case — and what makes the double expansion renderer→compiler a no-op),
    otherwise a deep copy with segments in place.
    """
    track = next((t for t in comp.get("tracks", [])
                  if isinstance(t, dict) and t.get("kind") == "video"), None)
    clips = track.get("clips", []) if track else []
    if not any(isinstance(c, dict) and c.get("speed_ramp") for c in clips):
        return comp
    comp = copy.deepcopy(comp)
    track = next(t for t in comp["tracks"]
                 if isinstance(t, dict) and t.get("kind") == "video")
    expanded: list[dict] = []
    for clip in track.get("clips", []):
        if not (isinstance(clip, dict) and clip.get("speed_ramp")):
            expanded.append(clip)
            continue
        cin = float(clip.get("in", 0.0))
        cout = clip.get("out")
        if cout is None:
            cout = float((media_info or {}).get(clip["src"], {}).get("duration", 0.0))
        expanded.extend(expand_clip(clip, cin, float(cout), n))
    track["clips"] = expanded
    return comp
