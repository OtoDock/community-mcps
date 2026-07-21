"""Composition → ffmpeg filtergraph compiler.

Pure translation layer: takes a composition whose media/LUT/caption paths are
ALREADY resolved to container paths (the renderer does that), plus a probe
map, and emits a RenderPlan — input table, filtergraph text, and encode args.
No filesystem access, no subprocesses: fully unit-testable.

Graph shape:
  base track   → per-clip chains [vb0..] folded pairwise with xfade/concat
                 (+ matching audio chains folded with acrossfade/concat)
  overlays     → per-clip chains composited with overlay=…:enable=…
  captions     → ass=<generated file> on the composited video
  audio tracks → per-clip chains, optional sidechain ducking against the
                 base bus, amix, master gain, loudnorm (final renders)

Timeline math mirrors composition.compute_timeline: a transition of duration
D overlaps the incoming clip D seconds into its predecessor, so the xfade
`offset` is exactly the incoming clip's timeline start.
"""

from dataclasses import dataclass, field

import audiofx as audiofx_mod
import color as color_mod
import composition as comp_mod
import slowmo as slowmo_mod
import speedramp as speedramp_mod
import stab as stab_mod
import transitions as transitions_mod
from fftools import atempo_chain, ff_color

_AUDIO_NORM = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"

_DUCK_DEFAULTS = {"threshold": 0.05, "ratio": 6, "attack": 20, "release": 400}


@dataclass
class RenderPlan:
    inputs: list = field(default_factory=list)   # [(options list, path), ...]
    graph: str = ""
    video_label: str = ""
    audio_label: str = ""
    encode_args: list = field(default_factory=list)
    output_path: str = ""
    duration: float = 0.0
    canvas: tuple = (0, 0)
    fps: float = 30.0
    loudnorm: dict | None = None   # set when the renderer must run 2-pass


def _f(v: float) -> str:
    return f"{v:.6g}"


class _GraphBuilder:
    def __init__(self, media_info: dict | None = None):
        self.inputs: list[tuple[list[str], str]] = []
        self._media_index: dict[str, int] = {}
        self._media_info = media_info or {}
        self.chains: list[str] = []
        self._n = 0

    def label(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def media_input(self, path: str) -> int:
        """Shared input for a media path (trim happens in-graph). Alpha WebM
        needs the libvpx decoder forced or transparency decodes as opaque
        black. NOTE: shared inputs preclude per-clip `-ss` fast seeks; for
        hour-long sources a per-clip seeking optimization can come later."""
        if path not in self._media_index:
            opts: list[str] = []
            alpha_codec = (self._media_info.get(path) or {}).get("alpha_codec")
            if alpha_codec:
                opts = ["-c:v", alpha_codec]
            self._media_index[path] = len(self.inputs)
            self.inputs.append((opts, path))
        return self._media_index[path]

    def still_input(self, path: str, duration: float) -> int:
        idx = len(self.inputs)
        self.inputs.append((["-loop", "1", "-t", _f(duration)], path))
        return idx

    def chain(self, src: str, filters: list[str], out: str) -> str:
        flt = ",".join(f for f in filters if f)
        self.chains.append(f"[{src}]{flt}[{out}]")
        return out

    def chain2(self, a: str, b: str, filt: str, out: str) -> str:
        self.chains.append(f"[{a}][{b}]{filt}[{out}]")
        return out

    def source_chain(self, source_filter: str, filters: list[str], out: str) -> str:
        flt = ",".join([source_filter] + [f for f in filters if f])
        self.chains.append(f"{flt}[{out}]")
        return out

    def graph(self) -> str:
        return ";\n".join(self.chains)


def piecewise(points: list[tuple[float, float]], var: str) -> str:
    """Clamped piecewise-linear ffmpeg expression over ``var``.

    [(0, 1.0), (4, 1.2)] → holds 1.0 before t=0, lerps to 1.2 at t=4,
    holds after. Single point → constant. The result contains commas, so
    the CALLER must embed it inside a single-quoted option value
    (x='…') — quoted values need no comma escaping.
    """
    if not points:
        raise comp_mod.CompositionError("piecewise needs at least one point")
    if len(points) == 1:
        return _f(points[0][1])
    expr = _f(points[-1][1])
    for i in range(len(points) - 2, -1, -1):
        t0, v0 = points[i]
        t1, v1 = points[i + 1]
        seg = (f"{_f(v0)}+({_f(v1)}-{_f(v0)})*"
               f"({var}-{_f(t0)})/({_f(t1 - t0)})")
        expr = f"if(lt({var},{_f(t1)}),{seg},{expr})"
    return f"if(lt({var},{_f(points[0][0])}),{_f(points[0][1])},{expr})"


def _keyframe_points(kfs: list[dict], prop: str) -> list[tuple[float, float]] | None:
    if not any(prop in kf for kf in kfs):
        return None
    return [(float(kf["t"]), float(kf[prop])) for kf in kfs]


def _keyframe_pos_points(kfs: list[dict]) -> tuple[list, list] | None:
    if not any("pos" in kf for kf in kfs):
        return None
    xs = [(float(kf["t"]), float(kf["pos"][0])) for kf in kfs]
    ys = [(float(kf["t"]), float(kf["pos"][1])) for kf in kfs]
    return xs, ys


def _zoompan_filter(kfs: list[dict], w: int, h: int, fps: float) -> str:
    """Ken Burns on a base clip: animated zoom/pan via zoompan, applied to
    the already-fitted canvas. Positions are output-pixel offsets of the
    content center from the canvas center (divided by zoom → input space)."""
    t_var = f"(on/{_f(fps)})"
    scale_pts = _keyframe_points(kfs, "scale")
    z_expr = piecewise(scale_pts, t_var) if scale_pts else "1"
    pos_pts = _keyframe_pos_points(kfs)
    if pos_pts:
        px = piecewise(pos_pts[0], t_var)
        py = piecewise(pos_pts[1], t_var)
    else:
        px = py = "0"
    x_expr = f"(iw-iw/zoom)/2-({px})/zoom"
    y_expr = f"(ih-ih/zoom)/2-({py})/zoom"
    return (f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
            f":d=1:s={w}x{h}:fps={_f(fps)}")


_EFFECT_FILTERS = {
    "chromakey": lambda e: (
        f"chromakey=color={ff_color(e.get('color', '#00FF00'))}"
        f":similarity={_f(float(e.get('similarity', 0.1)))}"
        f":blend={_f(float(e.get('blend', 0.05)))}"),
    "colorkey": lambda e: (
        f"colorkey=color={ff_color(e.get('color', '#000000'))}"
        f":similarity={_f(float(e.get('similarity', 0.1)))}"
        f":blend={_f(float(e.get('blend', 0.05)))}"),
    "despill": lambda e: f"despill=type={e.get('channel', 'green')}",
}


def _effect_filters(effects: list | None) -> list[str]:
    return [_EFFECT_FILTERS[e["type"]](e) for e in (effects or [])]


def _fit_filters(fit: str, w: int, h: int, bg: str) -> list[str]:
    if fit == "contain":
        return [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg}",
        ]
    return [
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={w}:{h}",
    ]


def _zoom_filters(scale: float, px: float, py: float, w: int, h: int, bg: str) -> list[str]:
    """Static zoom/reposition applied after canvas fit. pos is the offset of
    the clip's center from the canvas center, in project pixels."""
    if scale == 1.0 and px == 0 and py == 0:
        return []
    sw = f"trunc(iw*{_f(scale)}/2)*2"
    sh = f"trunc(ih*{_f(scale)}/2)*2"
    out = [f"scale={sw}:{sh}:flags=lanczos"]
    if scale >= 1.0:
        out.append(f"crop={w}:{h}:(iw-{w})/2-({_f(px)}):(ih-{h})/2-({_f(py)})")
    else:
        out.append(f"pad={w}:{h}:(ow-iw)/2+({_f(px)}):(oh-ih)/2+({_f(py)}):color={bg}")
    return out


def _color_filters(spec: dict | None, luts: dict[str, str]) -> list[str]:
    if not spec:
        return []
    filters = color_mod.to_filters(spec)
    lut = spec.get("lut")
    if lut:
        cube = luts.get(lut)
        if not cube:
            raise comp_mod.CompositionError(f"unresolved LUT '{lut}'")
        filters.append(f"lut3d=file='{cube}'")
    return filters


def _stab_filters(clip: dict) -> list[str]:
    """vidstabtransform+unsharp for a clip the renderer pre-passed (`_stab`
    injected after its detect run). Must sit between setpts and fps: the
    .trf transform file indexes SOURCE frames — an fps resample before it
    would misalign every frame's correction."""
    st = clip.get("_stab")
    return stab_mod.transform_filters(st["trf"], st) if st else []


def _motion_blur_filters(clip: dict) -> list[str]:
    """Cinematic motion blur via temporal frame stacking (tmix). strength
    0–1 → 2–6 stacked frames. Real shutter-drag needs the frames to
    differ — strongest on high-fps sources and flow-interpolated slow
    motion; on duplicated frames it does nothing."""
    mb = clip.get("motion_blur")
    if not mb:
        return []
    strength = float(mb.get("strength", 0.5)) if isinstance(mb, dict) \
        else (0.5 if mb is True else float(mb))
    return [f"tmix=frames={2 + round(strength * 4)}"]


def _blend_interp_filter(clip: dict, media_info: dict, fps: float,
                         speed: float) -> list[str]:
    """Inline blend interpolation for slow motion — pure compiler territory
    (unlike flow, which needs the renderer's mezzanine pre-render). Only
    fires when the retimed native frames can't fill the timeline rate.
    Runs after the setpts stretch, so the synthesis target is simply the
    timeline fps."""
    if speed >= 1.0 or clip.get("interpolate") != "blend" or clip.get("_slomo"):
        return []
    src_fps = float((media_info.get(clip.get("src")) or {}).get("fps") or 0)
    if slowmo_mod.native_sufficient(src_fps, speed, fps):
        return []
    return [slowmo_mod.BLEND_CHAIN.format(fps=_f(fps))]


def _finish_filters(spec: dict) -> list[str]:
    """Filmic finishing (base clips + project): sharpen → grain → vignette.
    Values are taste-bounded 0–1 knobs, not raw ffmpeg params."""
    out: list[str] = []
    sharpen = spec.get("sharpen")
    if sharpen:
        amount = float(sharpen.get("amount", 0.5)) if isinstance(sharpen, dict) \
            else (0.5 if sharpen is True else float(sharpen))
        out.append(f"unsharp=5:5:{_f(0.3 + 1.0 * amount)}:5:5:0")
    grain = spec.get("grain")
    if grain:
        strength = float(grain.get("strength", 0.3)) if isinstance(grain, dict) \
            else (0.3 if grain is True else float(grain))
        # t+u: temporal + uniform — film-like live grain, not static dust.
        out.append(f"noise=alls={max(1, round(20 * strength))}:allf=t+u")
    vignette = spec.get("vignette")
    if vignette:
        strength = float(vignette.get("strength", 0.5)) if isinstance(vignette, dict) \
            else (0.5 if vignette is True else float(vignette))
        out.append(f"vignette=angle={_f(0.25 + 0.55 * strength)}")
    return out


def _letterbox_filters(proj: dict, w: int, h: int) -> list[str]:
    """project.letterbox: '2.39' → black bars over the composited frame
    (canvas size unchanged; captions render on top, over the bars)."""
    ratio = proj.get("letterbox")
    if not ratio:
        return []
    ratio = float(ratio)
    bar = int((h - w / ratio) / 2)
    if bar < 2:
        return []
    return [f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill",
            f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill"]


def _match_filters(clip: dict) -> list[str]:
    """Single-reference shot match: the renderer-generated LUT (`_match`
    with a tmp-staged cube) applies BEFORE any creative grade — normalize
    the shot to its neighbor first, style on top. Ramp mode is handled
    structurally in _base_video_chain."""
    m = clip.get("_match")
    if m and m.get("cube"):
        return [f"lut3d=file='{m['cube']}'"]
    return []


def _clip_span(clip: dict, media_info: dict) -> tuple[float, float, float]:
    """(in, out, source_span) for a media clip, resolving a missing 'out'."""
    cin = float(clip.get("in", 0.0))
    cout = clip.get("out")
    if cout is None:
        cout = float(media_info[clip["src"]]["duration"])
    return cin, float(cout), float(cout) - cin


def _duration_pin(clip: dict, media_info: dict, fps: float) -> list[str]:
    """Pin a media clip chain to its COMPUTED duration (after the fps CFR
    conversion): clone-pad if short, trim if long. A trimmed span of a VFR
    source (phone/drone footage) decodes short of span/speed — the last
    frame's pts lands before 'out' — and slow motion divides the deficit
    by the speed. Without the pin the whole timeline drifts: xfade offsets
    fire late, preset windows mistime, and the (sample-exact) audio runs
    long against the video. Found live: a 6-segment VFR ramp came out
    0.8 s short of compute_timeline. The trailing fps= re-stamps the CFR
    rate metadata the trim clears — xfade rejects a rate-less input."""
    dur = (_clip_span(clip, media_info)[2]) / float(clip.get("speed", 1.0))
    return ["tpad=stop=-1:stop_mode=clone", f"trim=end={_f(dur)}",
            f"fps={_f(fps)}"]


def _preset_fx(base_clips: list, base_entries: list, i: int,
               w: int, h: int, fps: float) -> list[str]:
    """Wow-preset edge treatments for base clip i: head styling from its
    OWN transition_in, tail styling from the NEXT clip's. Cut presets keep
    the cut a cut (no overlap); xfade presets get their styling on top of
    the overlapping core."""
    def styled(tr):
        t = tr.get("type")
        return transitions_mod.is_preset(t) or transitions_mod.is_xfade_preset(t)

    fx: list[str] = []
    tr = base_clips[i].get("transition_in") or {}
    if i > 0 and styled(tr):
        fx += transitions_mod.head_filters(
            tr["type"], float(tr.get("duration", 0.3)), w, h, fps, tr)
    if i + 1 < len(base_clips):
        nxt = base_clips[i + 1].get("transition_in") or {}
        if styled(nxt):
            fx += transitions_mod.tail_filters(
                nxt["type"], float(nxt.get("duration", 0.3)), w, h, fps,
                base_entries[i]["duration"], nxt)
    return fx


def _base_video_chain(g: _GraphBuilder, clip: dict, media_info: dict,
                      w: int, h: int, fps: float, bg: str,
                      luts: dict[str, str],
                      trans_fx: list[str] | None = None) -> str:
    kind = comp_mod.clip_source_kind(clip)
    speed = float(clip.get("speed", 1.0))
    tr = clip.get("transform") or {}
    kfs = tr.get("keyframes")
    if kfs:
        motion = [_zoompan_filter(kfs, w, h, fps)]
    else:
        motion = _zoom_filters(float(tr.get("scale", 1.0)),
                               float((tr.get("pos") or [0, 0])[0]),
                               float((tr.get("pos") or [0, 0])[1]), w, h, bg)
    grade = (_match_filters(clip) + _color_filters(clip.get("color"), luts)
             + _finish_filters(clip) + (trans_fx or []))
    # settb=AVTB: every base clip leaves its chain on ONE explicit timebase.
    # Without it, a concat-produced fold accumulator can carry a different tb
    # than the next clip's chain, and feeding that pair into xfade makes
    # ffmpeg abort the whole graph with EINVAL (-22) — the "transition on the
    # last clip of a ≥3-clip track" failure.
    tail = motion + grade + ["format=yuv420p", "setsar=1", "settb=AVTB"]
    out = g.label("vb")

    if kind == "fill":
        dur = float(clip["duration"])
        src = (f"color=c={ff_color(clip['fill'])}:s={w}x{h}:r={_f(fps)}"
               f":d={_f(dur)}")
        return g.source_chain(
            src, ["format=yuv420p", "setsar=1"] + grade + ["settb=AVTB"], out)

    if kind == "image":
        dur = float(clip["duration"])
        idx = g.still_input(clip["image"], dur)
        # setpts BEFORE fps (matching the media branch): setpts clears the
        # stream's CFR metadata, and xfade rejects a non-constant-rate input
        # ("current rate of 1/0 is invalid") — an image clip on either side
        # of a transition killed the whole graph with EINVAL (-22).
        filters = ["setpts=PTS-STARTPTS", f"fps={_f(fps)}"]
        filters += _fit_filters(clip.get("fit", "cover"), w, h, bg)
        return g.chain(f"{idx}:v", filters + tail, out)

    slomo = clip.get("_slomo")
    if slomo:
        # Renderer-built flow mezzanine: trim, speed, interpolation (and
        # stabilization, when present) are baked in — consume it plain.
        idx = g.media_input(slomo["src"])
        filters = ["setpts=PTS-STARTPTS"]
    else:
        cin, cout, _ = _clip_span(clip, media_info)
        idx = g.media_input(clip["src"])
        filters = [f"trim=start={_f(cin)}:end={_f(cout)}"]
        if speed != 1.0:
            filters.append(f"setpts=(PTS-STARTPTS)/{_f(speed)}")
        else:
            filters.append("setpts=PTS-STARTPTS")
        filters += _stab_filters(clip)
        filters += _blend_interp_filter(clip, media_info, fps, speed)
    filters.append(f"fps={_f(fps)}")
    filters += _duration_pin(clip, media_info, fps)
    filters += _motion_blur_filters(clip)
    filters += _fit_filters(clip.get("fit", "cover"), w, h, bg)

    m = clip.get("_match")
    if m and m.get("a"):
        # Ramped two-endpoint match (AI-bridge joins): dissolve between two
        # GRADES of the same footage — an invisible grade ramp; the cuts
        # themselves stay hard cuts (operator decision 2026-07-20 over a
        # concealer micro-crossfade). T is clip-local after setpts, so the
        # blend weight runs 0→1 across the clip's duration.
        pre = g.chain(f"{idx}:v", filters + motion, g.label("vpre"))
        s1, s2 = g.label("vma"), g.label("vmb")
        g.chains.append(f"[{pre}]split=2[{s1}][{s2}]")
        ga = g.chain(s1, [f"lut3d=file='{m['a']}'"], g.label("vga"))
        gb = g.chain(s2, [f"lut3d=file='{m['b']}'"], g.label("vgb"))
        mixed = g.chain2(
            ga, gb,
            f"blend=all_expr='A+(B-A)*min(T/{_f(float(m['duration']))},1)'",
            g.label("vmr"))
        return g.chain(mixed,
                       grade + ["format=yuv420p", "setsar=1", "settb=AVTB"],
                       out)
    return g.chain(f"{idx}:v", filters + tail, out)


def _base_audio_chain(g: _GraphBuilder, clip: dict, media_info: dict,
                      timeline_dur: float) -> str:
    kind = comp_mod.clip_source_kind(clip)
    out = g.label("ab")
    has_audio = (kind == "media"
                 and media_info.get(clip.get("src"), {}).get("has_audio"))
    if not has_audio or clip.get("mute"):
        # _AUDIO_NORM + asettb: silence must be joinable with decoded audio
        # by both concat and acrossfade (same fmt/rate/layout AND timebase).
        return g.source_chain(
            "anullsrc=r=48000:cl=stereo",
            [f"atrim=0:{_f(timeline_dur)}", "asetpts=PTS-STARTPTS",
             _AUDIO_NORM, "asettb=AVTB"], out)

    cin, cout, _ = _clip_span(clip, media_info)
    speed = float(clip.get("speed", 1.0))
    filters = [f"atrim=start={_f(cin)}:end={_f(cout)}", "asetpts=PTS-STARTPTS"]
    filters += atempo_chain(speed)
    vol = clip.get("volume_db")
    if vol:
        filters.append(f"volume={_f(float(vol))}dB")
    filters += audiofx_mod.clip_chain(clip.get("audio"))
    filters.append(_AUDIO_NORM)
    filters.append("asettb=AVTB")
    idx = g.media_input(clip["src"])
    return g.chain(f"{idx}:a", filters, out)


def _fold_luma_wipe(g: _GraphBuilder, va: str, vb: str,
                    offset: float, tdur: float, fps: float) -> str:
    """Structural luma-wipe join: split the accumulator at the overlap,
    build an animated wipe map from the outgoing segment's own luma, and
    maskedmerge in gbrp (the gray mask replicates into every channel, so
    all three wipe together — no chroma casts), then reassemble with
    concat. Same timeline semantics as an xfade at (offset, tdur)."""
    # Every segment ends on an explicit format pin: format constraints
    # back-propagate through format-agnostic filters (split/trim/setpts),
    # and an unpinned graph let the mask's gray requirement grayscale the
    # ENTIRE upstream timeline (hit live 2026-07-20). extractplanes=y
    # (not format=gray) taps the luma without constraining its input.
    end = offset + tdur
    vaM, vaO = g.label("vw"), g.label("vw")
    g.chains.append(f"[{va}]split=2[{vaM}][{vaO}]")
    va_pre = g.chain(vaM, [f"trim=end={_f(offset)}", "setpts=PTS-STARTPTS",
                           "settb=AVTB", "format=yuv420p"], g.label("vw"))
    va_ov = g.chain(vaO, [f"trim=start={_f(offset)}:end={_f(end)}",
                          "setpts=PTS-STARTPTS", "settb=AVTB",
                          "format=yuv420p"], g.label("vw"))
    vbO, vbR = g.label("vw"), g.label("vw")
    g.chains.append(f"[{vb}]split=2[{vbO}][{vbR}]")
    vb_ov = g.chain(vbO, [f"trim=end={_f(tdur)}", "setpts=PTS-STARTPTS",
                          "settb=AVTB", "format=yuv420p"], g.label("vw"))
    vb_rest = g.chain(vbR, [f"trim=start={_f(tdur)}", "setpts=PTS-STARTPTS",
                            "settb=AVTB", "format=yuv420p"], g.label("vw"))
    m1, m2 = g.label("vw"), g.label("vw")
    g.chains.append(f"[{va_ov}]split=2[{m1}][{m2}]")
    mask = g.chain(m1, ["extractplanes=y",
                        transitions_mod.luma_wipe_mask_geq(tdur),
                        "format=gbrp"], g.label("vw"))
    base = g.chain(m2, ["format=gbrp"], g.label("vw"))
    over = g.chain(vb_ov, ["format=gbrp"], g.label("vw"))
    wipe = g.label("vw")
    g.chains.append(f"[{base}][{over}][{mask}]maskedmerge[{wipe}]")
    wiped = g.chain(wipe, ["format=yuv420p", "settb=AVTB"], g.label("vw"))
    out = g.label("vx")
    # fps= re-establishes CFR metadata: the trim/setpts segments clear it,
    # and a later xfade rejects a non-CFR accumulator with EINVAL (-22) —
    # hit live on a reel with fadeblack after the wipe.
    g.chains.append(f"[{va_pre}][{wiped}][{vb_rest}]"
                    f"concat=n=3:v=1:a=0,fps={_f(fps)},settb=AVTB[{out}]")
    return out


def _fold_base(g: _GraphBuilder, vlabels: list[str] | None,
               alabels: list[str] | None, timeline: list[dict],
               fps: float = 30.0) -> tuple[str, str]:
    """Pairwise-fold the base clips. Either stream list may be None when the
    plan is video-only (frame extraction) or audio-only (loudnorm pass 1).

    Invariant: the accumulator is always on tb=AVTB. Per-clip chains end with
    settb/asettb, and each concat re-asserts it — concat may pick its own
    output timebase, and an off-timebase accumulator fed into a later
    xfade/acrossfade kills the whole graph with EINVAL (-22)."""
    v = vlabels[0] if vlabels else ""
    a = alabels[0] if alabels else ""
    count = len(vlabels or alabels or [])
    for i in range(1, count):
        entry = timeline[i]
        trans = entry.get("transition")
        if trans:
            ttype = trans.get("type", "fade")
            tdur = float(trans.get("duration", 0.5))
            if vlabels:
                if ttype == "luma_wipe":
                    v = _fold_luma_wipe(g, v, vlabels[i],
                                        entry["start"], tdur, fps)
                elif transitions_mod.needs_rgb(ttype):
                    # Whip wrap expr runs in gbrp (full-size planes — yuv
                    # chroma would wrap out of phase). No shared splits
                    # here, so the gbrp constraint stops at the pinned
                    # clip chains.
                    va2 = g.chain(v, ["format=gbrp"], g.label("vx"))
                    vb2 = g.chain(vlabels[i], ["format=gbrp"], g.label("vx"))
                    mixed = g.chain2(va2, vb2,
                                     transitions_mod.xfade_option(
                                         ttype, tdur, entry["start"]),
                                     g.label("vx"))
                    v = g.chain(mixed, ["format=yuv420p", "settb=AVTB"],
                                g.label("vx"))
                else:
                    v = g.chain2(v, vlabels[i],
                                 transitions_mod.xfade_option(
                                     ttype, tdur, entry["start"]),
                                 g.label("vx"))
            if alabels:
                a = g.chain2(a, alabels[i],
                             f"acrossfade=d={_f(tdur)}:c1=tri:c2=tri", g.label("ax"))
        else:
            if vlabels:
                v = g.chain2(v, vlabels[i], "concat=n=2:v=1:a=0,settb=AVTB",
                             g.label("vx"))
            if alabels:
                a = g.chain2(a, alabels[i], "concat=n=2:v=0:a=1,asettb=AVTB",
                             g.label("ax"))
    return v, a


def _overlay_chain(g: _GraphBuilder, clip: dict, media_info: dict,
                   fps: float, luts: dict[str, str],
                   canvas_scale: float = 1.0) -> tuple[str, float, float, str, str]:
    """Prepare one overlay clip → (label, start, end, x_opt, y_opt).

    Filter order: keying effects (native res) → scale → grade → alpha →
    rotate → mask (alphamerge, scaled to the clip via scale2ref) →
    opacity → fades → timeline shift. Position comes back as ready overlay
    x=/y= option strings (single-quoted expressions when animated).

    ``canvas_scale``: overlay media is authored in project pixels, so a
    downscaled render canvas (preview / render_frames) must scale the
    overlay with it — positions alone are scaled by the caller.
    """
    kind = comp_mod.clip_source_kind(clip)
    start = float(clip.get("start", 0.0))
    speed = float(clip.get("speed", 1.0))
    tr = clip.get("transform") or {}
    scale = float(tr.get("scale", 1.0)) * canvas_scale
    opacity = float(tr.get("opacity", 1.0))
    rotate = float(tr.get("rotate", 0.0))
    kfs = tr.get("keyframes")
    fade_in = float(clip.get("fade_in", 0.0))
    fade_out = float(clip.get("fade_out", 0.0))

    if kind == "media":
        cin, cout, span = _clip_span(clip, media_info)
        dur = span / speed
        slomo = clip.get("_slomo")
        if slomo:
            idx = g.media_input(slomo["src"])
            src = f"{idx}:v"
            filters = ["setpts=PTS-STARTPTS"]
        else:
            idx = g.media_input(clip["src"])
            src = f"{idx}:v"
            filters = [f"trim=start={_f(cin)}:end={_f(cout)}"]
            filters.append(f"setpts=(PTS-STARTPTS)/{_f(speed)}" if speed != 1.0
                           else "setpts=PTS-STARTPTS")
            filters += _stab_filters(clip)
            filters += _blend_interp_filter(clip, media_info, fps, speed)
        filters.append(f"fps={_f(fps)}")
        filters += _duration_pin(clip, media_info, fps)
    elif kind == "image":
        dur = float(clip["duration"])
        idx = g.still_input(clip["image"], dur)
        src = f"{idx}:v"
        filters = [f"fps={_f(fps)}", "setpts=PTS-STARTPTS"]
    else:
        raise comp_mod.CompositionError("fill clips are not valid overlays")

    filters += _effect_filters(clip.get("effects"))
    if scale != 1.0:
        filters.append(f"scale=trunc(iw*{_f(scale)}/2)*2:-2:flags=lanczos")
    filters += _color_filters(clip.get("color"), luts)
    filters.append("format=rgba")
    if rotate:
        filters.append(f"rotate=a={_f(rotate)}*PI/180:c=black@0"
                       f":ow='hypot(iw,ih)':oh=ow")

    mask = clip.get("mask")
    if mask:
        pre = g.label("ovp")
        g.chain(src, filters, pre)
        midx = g.still_input(mask["image"], dur)
        mpre = g.label("mk")
        g.chain(f"{midx}:v", [f"fps={_f(fps)}", "format=gray"], mpre)
        ms, ov2 = g.label("mks"), g.label("ovr")
        g.chains.append(f"[{mpre}][{pre}]scale2ref[{ms}][{ov2}]")
        merged = g.label("ovm")
        g.chain2(ov2, ms, "alphamerge", merged)
        src, filters = merged, []

    post: list[str] = []
    if opacity < 1.0:
        post.append(f"colorchannelmixer=aa={_f(opacity)}")
    if fade_in > 0:
        post.append(f"fade=t=in:st=0:d={_f(fade_in)}:alpha=1")
    if fade_out > 0:
        post.append(f"fade=t=out:st={_f(max(0.0, dur - fade_out))}:d={_f(fade_out)}:alpha=1")
    # Shift into timeline time LAST so enable=/overlay see final PTS.
    post.append(f"setpts=PTS+{_f(start)}/TB")

    out = g.label("ov")
    g.chain(src, filters + post, out)

    pos_pts = _keyframe_pos_points(kfs) if kfs else None
    if pos_pts:
        local = f"(t-{_f(start)})"
        x_opt = f"x='(W-w)/2+({piecewise(pos_pts[0], local)})'"
        y_opt = f"y='(H-h)/2+({piecewise(pos_pts[1], local)})'"
    else:
        pos = tr.get("pos") or [0, 0]
        x_opt = f"x=(W-w)/2+({_f(float(pos[0]))})"
        y_opt = f"y=(H-h)/2+({_f(float(pos[1]))})"
    return out, start, start + dur, x_opt, y_opt


def _audio_track_chain(g: _GraphBuilder, clip: dict, media_info: dict) -> tuple[str, dict | None]:
    start = float(clip.get("start", 0.0))
    cin = float(clip.get("in", 0.0))
    cout = clip.get("out")
    speed = float(clip.get("speed", 1.0))
    idx = g.media_input(clip["src"])

    filters = []
    if cin or cout is not None:
        end = f":end={_f(float(cout))}" if cout is not None else ""
        filters.append(f"atrim=start={_f(cin)}{end}")
    filters.append("asetpts=PTS-STARTPTS")
    filters += atempo_chain(speed)
    gain = clip.get("gain_db", clip.get("volume_db"))
    if gain:
        filters.append(f"volume={_f(float(gain))}dB")
    filters += audiofx_mod.clip_chain(clip.get("audio"))
    fade_in = float(clip.get("fade_in", 0.0))
    fade_out = float(clip.get("fade_out", 0.0))
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={_f(fade_in)}")
    if fade_out > 0:
        dur = ((float(cout) if cout is not None
                else float(media_info[clip["src"]]["duration"])) - cin) / speed
        filters.append(f"afade=t=out:st={_f(max(0.0, dur - fade_out))}:d={_f(fade_out)}")
    filters.append(_AUDIO_NORM)
    if start > 0:
        ms = int(round(start * 1000))
        filters.append(f"adelay={ms}:all=1")

    out = g.label("at")
    g.chain(f"{idx}:a", filters, out)

    duck = clip.get("duck")
    duck_opts = None
    if duck:
        duck_opts = dict(_DUCK_DEFAULTS)
        if isinstance(duck, dict):
            duck_opts.update({k: duck[k] for k in _DUCK_DEFAULTS if k in duck})
    return out, duck_opts


def compile_render(
    comp: dict,
    media_info: dict,
    *,
    mode: str = "final",
    output_path: str = "",
    canvas_scale: float = 1.0,
    time_range: tuple[float, float] | None = None,
    captions_ass: str | None = None,
    luts: dict[str, str] | None = None,
    streams: str = "av",
    crf: int | None = None,
) -> RenderPlan:
    """Compile a resolved composition into a RenderPlan.

    ``media_info``: path → {duration, has_audio, has_video} for every media
    path in the composition. ``captions_ass``: pre-generated ASS file path
    (the renderer builds it against the render canvas). ``luts``: lut
    reference (as written in the composition) → .cube file path.
    ``streams``: "av" full render, "v" video-only (frame extraction),
    "a" audio-only (the loudnorm measurement pass — skips all frame decode).
    ``crf``: override the mode's default x264 CRF (final 18, preview 27) —
    the size knob for web deliverables (e.g. 28 for a ≤20 MB site MP4).
    """
    luts = luts or {}
    proj = comp["project"]
    w = int(round(int(proj["width"]) * canvas_scale / 2) * 2)
    h = int(round(int(proj["height"]) * canvas_scale / 2) * 2)
    fps = float(proj.get("fps", 30))
    bg = ff_color(proj.get("background", "#000000"))

    # Positions are authored in project pixels — scale them (static AND
    # keyframed) with the canvas.
    def scaled(comp_dict: dict) -> dict:
        if canvas_scale == 1.0:
            return comp_dict
        import copy as _copy
        out = _copy.deepcopy(comp_dict)
        for track in out.get("tracks", []):
            for clip in track.get("clips", []):
                tr = clip.get("transform") or {}
                pos = tr.get("pos")
                if pos:
                    tr["pos"] = [pos[0] * canvas_scale, pos[1] * canvas_scale]
                for kf in tr.get("keyframes") or []:
                    if isinstance(kf, dict) and kf.get("pos"):
                        kf["pos"] = [kf["pos"][0] * canvas_scale,
                                     kf["pos"][1] * canvas_scale]
        return out

    comp = scaled(comp)
    # No-op when the renderer already expanded (its pre-passes must see the
    # segments to route slow ones through the mezzanine) — running it here
    # too keeps the compiler self-sufficient for direct callers.
    comp = speedramp_mod.expand_composition(comp, media_info)
    timeline = comp_mod.compute_timeline(comp, media_info)
    base_entries = timeline["base"]

    g = _GraphBuilder(media_info)
    base_clips = comp_mod.base_track(comp)["clips"]

    want_v = "v" in streams
    want_a = "a" in streams

    vlabels = [] if want_v else None
    alabels = [] if want_a else None
    for i, clip in enumerate(base_clips):
        if want_v:
            fx = _preset_fx(base_clips, base_entries, i, w, h, fps)
            vlabels.append(_base_video_chain(g, clip, media_info, w, h, fps,
                                             bg, luts, trans_fx=fx))
        if want_a:
            alabels.append(_base_audio_chain(g, clip, media_info,
                                             base_entries[i]["duration"]))

    v, a = _fold_base(g, vlabels, alabels, base_entries, fps=fps)

    vout = ""
    if want_v:
        # Overlays composite in track order, then clip order (z-order).
        for track in comp.get("tracks", []):
            if track.get("kind") != "overlay":
                continue
            for clip in track.get("clips", []):
                ov, start, end, x_opt, y_opt = _overlay_chain(
                    g, clip, media_info, fps, luts, canvas_scale)
                v = g.chain2(
                    v, ov,
                    f"overlay={x_opt}:{y_opt}"
                    f":enable='between(t,{_f(start)},{_f(end)})'"
                    f":eof_action=pass:format=auto",
                    g.label("vc"))

        # Global grade, finishing, letterbox, captions, final pixel format.
        # Captions go AFTER the letterbox so they can sit on the bars.
        tail: list[str] = []
        tail += _color_filters(proj.get("color"), luts)
        tail += _finish_filters(proj)
        tail += _letterbox_filters(proj, w, h)
        if captions_ass:
            tail.append(f"ass=filename='{captions_ass}'")
        tail.append("format=yuv420p")
        if time_range:
            t0, t1 = time_range
            tail.append(f"trim=start={_f(t0)}:end={_f(t1)}")
            tail.append("setpts=PTS-STARTPTS")
            # trim clears the CFR rate metadata; without a re-pin the CLI
            # falls back to 25 fps at the sink and silently resamples the
            # slice (dropped frames, drifted duration — broke the windowed
            # render's frame-exact concat). The kept frames already sit on
            # the fps grid from 0, so this re-stamps without dup/drop.
            tail.append(f"fps={_f(fps)}")
        vout = g.label("vout")
        g.chain(v, tail, vout)

    # Audio tracks + ducking + mix.
    track_labels: list[tuple[str, dict | None]] = []
    if want_a:
        for track in comp.get("tracks", []):
            if track.get("kind") != "audio":
                continue
            for clip in track.get("clips", []):
                track_labels.append(_audio_track_chain(g, clip, media_info))

    aout = ""
    loudnorm_cfg = None
    duck_count = sum(1 for _, opts in track_labels if opts)
    if want_a and duck_count:
        # The sidechain key is everything that is NOT ducked: the base bus
        # plus every non-ducked audio clip. Screen captures are often silent,
        # so a music bed with duck=true must dip under a voice-over clip, not
        # just under base-track audio. Each key source is split into a mix leg
        # and a key leg.
        mix_leg = g.label("am")
        base_key = g.label("sc")
        g.chains.append(f"[{a}]asplit=2[{mix_leg}][{base_key}]")
        a = mix_leg
        key_srcs = [base_key]
        for i, (label, opts) in enumerate(track_labels):
            if opts:
                continue
            clip_mix, clip_key = g.label("at"), g.label("sc")
            g.chains.append(f"[{label}]asplit=2[{clip_mix}][{clip_key}]")
            track_labels[i] = (clip_mix, None)
            key_srcs.append(clip_key)
        key_bus = key_srcs[0]
        if len(key_srcs) > 1:
            key_bus = g.label("sk")
            g.chains.append(
                "".join(f"[{s}]" for s in key_srcs)
                + f"amix=inputs={len(key_srcs)}:duration=first:normalize=0"
                + f"[{key_bus}]")
        legs = [key_bus]
        if duck_count > 1:
            legs = [g.label("sc") for _ in range(duck_count)]
            g.chains.append(
                f"[{key_bus}]asplit={duck_count}"
                + "".join(f"[{leg}]" for leg in legs))
        li = 0
        for i, (label, opts) in enumerate(track_labels):
            if not opts:
                continue
            out = g.label("ad")
            g.chain2(label, legs[li],
                     f"sidechaincompress=threshold={_f(opts['threshold'])}"
                     f":ratio={_f(opts['ratio'])}:attack={_f(opts['attack'])}"
                     f":release={_f(opts['release'])}", out)
            track_labels[i] = (out, None)
            li += 1

    if want_a:
        mix_inputs = [a] + [label for label, _ in track_labels]
        if len(mix_inputs) > 1:
            amixed = g.label("amix")
            g.chains.append(
                "".join(f"[{l}]" for l in mix_inputs)
                + f"amix=inputs={len(mix_inputs)}:duration=first:normalize=0[{amixed}]")
            a = amixed

        master = comp.get("audio_master") or {}
        atail: list[str] = []
        gain = master.get("gain_db")
        if gain:
            atail.append(f"volume={_f(float(gain))}dB")
        # Master sweetening before normalization: loudnorm must measure the
        # processed bus, and the limiter is the true-peak safety when
        # loudnorm is off.
        atail += audiofx_mod.master_chain(master)

        ln = master.get("loudnorm", True)
        if mode == "final" and ln:
            opts = ln if isinstance(ln, dict) else {}
            loudnorm_cfg = {
                "i": float(opts.get("target_lufs", -14.0)),
                "tp": float(opts.get("true_peak", -1.5)),
                "lra": float(opts.get("lra", 11.0)),
            }
            # The renderer measures pass 1 and substitutes measured_* values.
            atail.append("__LOUDNORM__")

        if time_range:
            t0, t1 = time_range
            atail.append(f"atrim=start={_f(t0)}:end={_f(t1)}")
            atail.append("asetpts=PTS-STARTPTS")
        aout = g.label("aout")
        g.chain(a, atail or ["anull"], aout)

    encode = (
        ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf or 18),
         "-profile:v", "high",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        if mode == "final" else
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf or 27),
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    )

    duration = timeline["duration"]
    if time_range:
        duration = max(0.0, min(duration, time_range[1]) - time_range[0])

    return RenderPlan(
        inputs=g.inputs,
        graph=g.graph(),
        video_label=vout,
        audio_label=aout,
        encode_args=encode,
        output_path=output_path,
        duration=duration,
        canvas=(w, h),
        fps=fps,
        loudnorm=loudnorm_cfg,
    )


# ---------------------------------------------------------------------------
# Low-RAM windowed rendering (pure planning helpers)
# ---------------------------------------------------------------------------
#
# ffmpeg runs the whole composition as ONE filtergraph, and its scheduler
# lets decoded frames pile up in unbounded filtergraph queues at the
# fold/overlay junctions — peak RSS ≈ every decoded SOURCE frame of the
# render window at once (measured 2026-07-20: a 70 s 1080p timeline
# buffered ~12.7 GB and OOM-killed a 15 GB host; a 72 s toy comp buffered
# exactly timeline × fps × frame_size, and a 6 s time_range slice cost the
# same as the full render because nothing was pruned). The fix: render the
# timeline in windows split at bare cuts, compiling each window from a
# window-pruned sub-composition so far-away media is never even opened.


def window_pruned(comp: dict, media_info: dict, t0: float, t1: float,
                  pad: float = 0.0) -> dict:
    """Sub-composition that renders [t0, t1] only: base clips that cannot
    contribute frames to the (padded) window become solid-color fills of
    IDENTICAL duration — timeline math, fold structure and every transition
    offset stay bit-identical, the substituted spans are trimmed away —
    and overlays outside the window are dropped. Audio clips stay: audio
    frames are never the memory problem, and pruning them would change the
    loudnorm/duck mix. This is what makes a windowed render cheap: the
    pruned comp no longer references the far media, so those inputs are
    never opened or decoded."""
    import copy

    out = copy.deepcopy(comp)
    entries = comp_mod.compute_timeline(out, media_info)["base"]
    bg = out["project"].get("background", "#000000")
    lo, hi = t0 - pad, t1 + pad
    clips = comp_mod.base_track(out)["clips"]
    for i, clip in enumerate(clips):
        e = entries[i]
        if e["end"] > lo and e["start"] < hi:
            continue
        # transition_in rides along: it drives the fold's xfade offsets and
        # the tail styling of the PREVIOUS clip (_preset_fx) — dropping it
        # would change kept clips.
        fill = {"fill": bg, "duration": e["duration"]}
        if clip.get("transition_in") is not None:
            fill["transition_in"] = copy.deepcopy(clip["transition_in"])
        clips[i] = fill
    for track in out.get("tracks", []):
        if track.get("kind") != "overlay":
            continue
        track["clips"] = [
            c for c in track["clips"]
            if float(c.get("start", 0.0)) < hi
            and float(c.get("start", 0.0)) + comp_mod.clip_duration(c, media_info) > lo
        ]
    return out


def bare_cut_points(comp: dict, media_info: dict) -> list[float]:
    """Timeline times where the base track can split without touching a
    join: TRUE hard cuts only. Any transition_in besides a plain cut
    disqualifies — xfades overlap the boundary, and preset cuts style the
    outgoing tail across it."""
    clips = comp_mod.base_track(comp).get("clips", [])
    entries = comp_mod.compute_timeline(comp, media_info)["base"]
    return [
        entries[i]["start"] for i in range(1, len(clips))
        if not clips[i].get("transition_in")
        or clips[i]["transition_in"].get("type", "cut") == "cut"
    ]


def estimate_window_bytes(comp: dict, media_info: dict,
                          t0: float, t1: float) -> float:
    """Worst-case decoded-frame bytes ffmpeg buffers while rendering
    [t0, t1): the window's base frames at SOURCE resolution (the pile sits
    before the canvas scale) plus every overlapping overlay's full clip in
    rgba. A deliberate over-estimate — the cost of an extra segment split
    is a few seconds of re-opened inputs, the cost of an under-estimate is
    an OOM-killed host."""
    proj = comp["project"]
    fps = float(proj.get("fps", 30))

    def src_pixels(clip: dict) -> int:
        mi = media_info.get(clip.get("src") or clip.get("image")) or {}
        return (int(mi.get("width") or proj["width"])
                * int(mi.get("height") or proj["height"]))

    total = 0.0
    entries = comp_mod.compute_timeline(comp, media_info)["base"]
    clips = comp_mod.base_track(comp).get("clips", [])
    for e, clip in zip(entries, clips):
        overlap = min(e["end"], t1) - max(e["start"], t0)
        if overlap <= 0 or comp_mod.clip_source_kind(clip) == "fill":
            continue
        total += overlap * fps * src_pixels(clip) * 1.5
    for track in comp.get("tracks", []):
        if track.get("kind") != "overlay":
            continue
        for clip in track["clips"]:
            start = float(clip.get("start", 0.0))
            dur = comp_mod.clip_duration(clip, media_info)
            if start < t1 and start + dur > t0:
                total += dur * fps * src_pixels(clip) * 4
    return total


def plan_segments(comp: dict, media_info: dict,
                  budget_bytes: float) -> list[tuple[float, float]]:
    """Split the timeline at bare cuts so no window's estimate exceeds the
    budget. Returns [] when the whole timeline fits (single-pass render).
    A span between adjacent bare cuts that alone exceeds the budget stays
    one segment — there is nowhere safe to split it."""
    duration = comp_mod.compute_timeline(comp, media_info)["duration"]
    if duration <= 0:
        return []
    if estimate_window_bytes(comp, media_info, 0.0, duration) <= budget_bytes:
        return []
    segs: list[tuple[float, float]] = []
    seg_start, prev = 0.0, None
    for b in [*bare_cut_points(comp, media_info), duration]:
        if b <= seg_start + 1e-6:
            continue
        if (prev is not None
                and estimate_window_bytes(comp, media_info, seg_start, b)
                > budget_bytes):
            segs.append((seg_start, prev))
            seg_start = prev
        prev = b
    segs.append((seg_start, duration))
    return segs if len(segs) > 1 else []
