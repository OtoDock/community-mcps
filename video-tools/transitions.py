"""Wow transition presets (social pack).

Two families:
- CUT presets (PRESETS): edge treatments around a genuine HARD CUT — a
  preset of duration D puts ~D/2 of styling on the outgoing tail and ~D/2
  on the incoming head, injected into the CLIP chains (where t is
  clip-local) as enable-windowed filters and zoompan/rotate expressions.
  The fold still concats; the timeline math is untouched.
- XFADE presets (XFADE_PRESETS): an xfade core carries real motion or a
  reveal (normal overlap semantics — the timeline shortens by D), with
  optional edge treatments on top (the whips add directional blur so the
  slide reads as a camera whip that CONTINUES across the join).

Pure string builders; the compiler owns placement (after the grade,
before the format tail).
"""

PRESETS = ("whip_pan", "zoom_punch", "flash_cut", "glitch", "spin", "shake",
           "zoom_out", "zoom_in")

# Overlapping presets (normal xfade timeline semantics — the clips overlap
# by D). The whips ride an xfade slide core with blur edge treatments on
# both clip chains. luma_wipe is a structural maskedmerge join built by the
# compiler: the incoming scene grows through the OUTGOING frame's brightest
# shapes. (An xfade custom-expr version was tried first — per-plane exprs
# can't key chroma on luma, and per-channel RGB thresholds separate on
# saturated areas → purple/orange casts, seen on real footage. The
# maskedmerge mask converts gray→gbrp, so every channel wipes together.)
XFADE_PRESETS = ("whip_left", "whip_right", "luma_wipe")


def _f(v: float) -> str:
    return f"{float(v):.6g}"


def is_preset(name) -> bool:
    """Cut-based presets: zero timeline overlap (the fold concats)."""
    return isinstance(name, str) and name in PRESETS


def is_xfade_preset(name) -> bool:
    """Overlapping presets: xfade timeline semantics + optional edge fx."""
    return isinstance(name, str) and name in XFADE_PRESETS


def needs_rgb(ttype) -> bool:
    """Whips run their custom expr in gbrp: every plane is full-size, so
    the per-plane W matches the wrap arithmetic (yuv chroma planes are
    half-width and would wrap out of phase — color smear at the seam)."""
    return ttype in ("whip_left", "whip_right")


def _whip_expr(direction: str) -> str:
    """Adjacent-strip whip (operator-corrected 2026-07-20): the two shots
    are one continuous strip — whip_left rushes the frame LEFT and the
    next shot slides in attached on the right (whip_right mirrors). The
    pan covers exactly one frame-width on the eased slow-fast-slow ramp,
    and the seam between the shots is a FEATHERED blend band (5% of
    width), not a hard line — the dblur edge ramps then bury it.
    (v1 wrapped the SAME frame around Premiere-Offset style, so the old
    clip re-entered instead of the next one — wrong concept.)

    Everything is INLINED: st()/ld() in a per-pixel xfade expr share one
    variable store across the filter's threads — a data race that renders
    as full-frame static (hit live 2026-07-20). Per-frame exprs (zoompan)
    can keep st/ld; per-pixel exprs cannot."""
    # xfade's P runs 1 → 0 (1 at the transition START — the documented
    # dissolve is 'A*P+B*(1-P)'), so progress is (1-P). Using P directly
    # played the whip BACKWARD: snap to B, slide back to A, snap to B —
    # the operator's "I immediately see the other clip, then everything
    # slides, the order is reversed" (and the earlier "four changes").
    e = "((1-P)*(1-P)*(3-2*(1-P)))"
    # Direction = the CAMERA pan (operator-corrected 2026-07-20):
    # whip_left pans left → content slides right → the next shot is
    # attached on the LEFT and slides in from there; whip_right mirrors.
    if direction == "left":
        xs = f"(X-W*{e})"                       # strip [B | A], slide right
        mix = f"clip((0-{xs})/(W*0.05)+0.5,0,1)"
        ca = f"clip({xs},0,W-1)"
        cb = f"clip({xs}+W,0,W-1)"
    else:
        xs = f"(X+W*{e})"                       # strip [A | B], slide left
        mix = f"clip(({xs}-W)/(W*0.05)+0.5,0,1)"
        ca = f"clip({xs},0,W-1)"
        cb = f"clip({xs}-W,0,W-1)"
    a = f"if(eq(PLANE,0),a0({ca},Y),if(eq(PLANE,1),a1({ca},Y),a2({ca},Y)))"
    b = f"if(eq(PLANE,0),b0({cb},Y),if(eq(PLANE,1),b1({cb},Y),b2({cb},Y)))"
    return f"(1-{mix})*{a}+{mix}*{b}"


def xfade_option(ttype: str, tdur: float, offset: float) -> str:
    """The fold's xfade filter string for a transition type — plain xfade
    names pass through; whips become the wrap-around custom expr (the
    compiler wraps them in gbrp — see needs_rgb). (luma_wipe never
    reaches here — the compiler builds its maskedmerge join
    structurally.)"""
    if ttype in ("whip_left", "whip_right"):
        expr = _whip_expr("left" if ttype == "whip_left" else "right")
        return (f"xfade=transition=custom:expr='{expr}'"
                f":duration={_f(tdur)}:offset={_f(offset)}")
    return (f"xfade=transition={ttype}:duration={_f(tdur)}"
            f":offset={_f(offset)}")


def luma_wipe_mask_geq(tdur: float) -> str:
    """Animated wipe map from the outgoing overlap's luma (gray stream):
    progress Pr descends a brightness threshold, softness 6, with a bias
    ramp that delays onset slightly and guarantees full completion at
    Pr=1 (383 > 255 + softness margin — near-black pixels must switch
    too, or the wipe never finishes)."""
    pr = f"clip(T/{_f(tdur)},0,1)"
    return (f"geq='clip((p(X,Y)-255*(1-{pr}))*6+383*{pr}-128,0,255)'")


def _zoompan(z_expr: str, w: int, h: int, fps: float,
             x_extra: str = "", y_extra: str = "") -> str:
    x = f"(iw-iw/zoom)/2{x_extra}"
    y = f"(ih-ih/zoom)/2{y_extra}"
    return (f"zoompan=z='{z_expr}':x='{x}':y='{y}'"
            f":d=1:s={w}x{h}:fps={_f(fps)}")


def _mirror_zoom(z_expr: str, w: int, h: int, fps: float) -> list[str]:
    """The Kolder-zoom canvas: 2×2 mirror tile (Premiere's Motion Tile
    with Mirror Edges — pad then fillborders=mirror) so zooming below
    full frame reveals mirrored surroundings instead of black, then an
    animated zoompan on the tile. z=2 shows exactly the original frame;
    z=1 pulls back to 50% inside the mirrors. Costs 4× pixels for the
    clip that carries it — keep such clips short."""
    return [f"pad={2 * w}:{2 * h}:{w // 2}:{h // 2}:color=black",
            f"fillborders=left={w // 2}:right={w // 2}"
            f":top={h // 2}:bottom={h // 2}:mode=mirror",
            _zoompan(z_expr, w, h, fps)]


def _eased(q_expr: str) -> str:
    """Store smoothstep(clip(q)) in ld(0): slow → fast → slow. For ramps
    that live entirely inside ONE window (the whip). Callers append the
    formula reading ld(0)."""
    return f"st(0,{q_expr});st(0,ld(0)*ld(0)*(3-2*ld(0)))"


def _ease_in(q_expr: str) -> str:
    """Accelerate: slow → max AT the window end. The outgoing side of a
    split ramp — max speed lands exactly on the cut."""
    return f"st(0,{q_expr});st(0,ld(0)*ld(0))"


def _ease_out(q_expr: str) -> str:
    """Decelerate: max at the window start → rest. The incoming side of a
    split ramp — together with _ease_in the speed is one continuous bell
    across the cut, not fast-STOP-fast (the 'bump' the operator saw when
    both sides ran full smoothstep)."""
    return f"st(0,{q_expr});st(0,1-(1-ld(0))*(1-ld(0)))"


# Zoom timing split (operator-tuned): the MIRRORED phase is the giveaway,
# so it gets less of the duration than the clean phase. zoom_in mirrors on
# the incoming side (head short); zoom_out mirrors on the outgoing (tail
# short). (tail_frac, head_frac) of the transition duration.
_ZOOM_FRACS = {"zoom_in": (0.6, 0.4), "zoom_out": (0.4, 0.6)}


def _blur_steps(windows: list[tuple[float, float, float]]) -> list[str]:
    return [f"gblur=sigma={_f(s)}:enable='between(t,{_f(a)},{_f(b)})'"
            for a, b, s in windows]


def _flash_sign(opts: dict | None) -> float:
    """flash_cut / zoom_punch dip color: black (default, brightness dips
    negative) or white (the classic pop) via transition_in {flash: ...}."""
    return 1.0 if (opts or {}).get("flash") == "white" else -1.0


def head_filters(preset: str, d: float, w: int, h: int, fps: float,
                 opts: dict | None = None) -> list[str]:
    """Treatment on the INCOMING clip, clip-local window starting at t=0."""
    half = max(d / 2.0, 1.0 / fps)
    t = f"(on/{_f(fps)})"
    if preset == "flash_cut":
        f1 = max(1.5 / fps, 0.03)
        s = _flash_sign(opts)
        return [f"eq=brightness={_f(0.9 * s)}:enable='lt(t,{_f(f1)})'",
                f"eq=brightness={_f(0.45 * s)}"
                f":enable='between(t,{_f(f1)},{_f(2 * f1)})'"]
    if preset in ("whip_pan", "whip_left", "whip_right"):
        step = half / 3.0
        return [f"dblur=angle=0:radius={_f(r)}"
                f":enable='between(t,{_f(i * step)},{_f((i + 1) * step)})'"
                for i, r in enumerate((18, 11, 5))]
    if preset == "luma_wipe":
        return []  # the reveal is entirely in the xfade core
    if preset == "zoom_punch":
        z = f"if(lt({t},{_f(half)}),1+0.25*pow(1-{t}/{_f(half)},2),1)"
        return [_zoompan(z, w, h, fps),
                f"eq=brightness={_f(0.5 * _flash_sign(opts))}"
                f":enable='lt(t,{_f(1.5 / fps)})'"]
    if preset == "glitch":
        s = d / 3.0
        return [
            f"rgbashift=rh={round(w * 0.006)}:bv=-{round(w * 0.006)}"
            f":enable='lt(t,{_f(s)})'",
            f"rgbashift=rh=-{round(w * 0.008)}:gv={round(w * 0.005)}"
            f":enable='between(t,{_f(s)},{_f(2 * s)})'",
            f"rgbashift=rh={round(w * 0.003)}:bh=-{round(w * 0.003)}"
            f":enable='between(t,{_f(2 * s)},{_f(d)})'",
            f"noise=alls=30:allf=t+u:enable='lt(t,{_f(d)})'",
        ]
    if preset == "spin":
        a = f"if(lt(t,{_f(half)}),pow(1-t/{_f(half)},2)*0.6,0)"
        return [f"rotate=a='{a}':c=black",
                f"gblur=sigma=9:enable='lt(t,{_f(half * 0.6)})'",
                f"gblur=sigma=3:enable='between(t,{_f(half * 0.6)},{_f(half)})'"]
    if preset == "shake":
        # Impact shake: zoom + offset decay smoothly to exactly 1.0/0 — no
        # pop when the effect ends. Deterministic (incommensurate sines).
        k = 6.0 / half
        amp = w * 0.012
        z = f"1+0.06*exp(-{t}*{_f(k)})"
        jx = f"+{_f(amp)}*sin(on*2.9)*exp(-{t}*{_f(k)})/zoom"
        jy = f"+{_f(amp * 0.7)}*cos(on*3.7)*exp(-{t}*{_f(k)})/zoom"
        return [_zoompan(z, w, h, fps, x_extra=jx, y_extra=jy)]
    if preset in ("zoom_out", "zoom_in"):
        # Incoming side continues the SAME motion direction, DECELERATING
        # from the cut (max speed at the join — the outgoing side
        # accelerates into it) and settles at rest (z=2 = exact frame).
        win = max(d * _ZOOM_FRACS[preset][1], 1.0 / fps)
        e = _ease_out(f"clip({t}/{_f(win)},0,1)")
        z = (f"{e};1+ld(0)" if preset == "zoom_in"     # 1 → 2: keep pushing
             else f"{e};6-4*ld(0)")                     # 6 → 2: keep pulling
        step = win / 3.0
        return (_mirror_zoom(z, w, h, fps)
                + _blur_steps([(0, step, 12), (step, 2 * step, 6),
                               (2 * step, 3 * step, 2)]))
    raise ValueError(f"unknown transition preset '{preset}'")


def tail_filters(preset: str, d: float, w: int, h: int, fps: float,
                 clip_dur: float, opts: dict | None = None) -> list[str]:
    """Treatment on the OUTGOING clip, clip-local window ending at the cut
    (t = clip_dur)."""
    half = max(d / 2.0, 1.0 / fps)
    t0 = max(0.0, clip_dur - half)
    t = f"(on/{_f(fps)})"
    if preset == "flash_cut":
        f1 = max(1.5 / fps, 0.03)
        s = _flash_sign(opts)
        return [f"eq=brightness={_f(0.35 * s)}"
                f":enable='gte(t,{_f(clip_dur - f1)})'"]
    if preset in ("whip_pan", "whip_left", "whip_right"):
        step = half / 3.0
        return [f"dblur=angle=0:radius={_f(r)}"
                f":enable='between(t,{_f(t0 + i * step)},{_f(t0 + (i + 1) * step)})'"
                for i, r in enumerate((5, 11, 18))]
    if preset == "luma_wipe":
        return []
    if preset == "zoom_punch":
        z = f"if(gte({t},{_f(t0)}),1+0.18*pow(({t}-{_f(t0)})/{_f(half)},2),1)"
        return [_zoompan(z, w, h, fps)]
    if preset == "glitch":
        f2 = 2.5 / fps
        return [f"rgbashift=rh={round(w * 0.004)}:bv=-{round(w * 0.003)}"
                f":enable='gte(t,{_f(clip_dur - f2)})'"]
    if preset == "spin":
        a = f"if(gte(t,{_f(t0)}),-pow((t-{_f(t0)})/{_f(half)},2)*0.6,0)"
        return [f"rotate=a='{a}':c=black",
                f"gblur=sigma=3:enable='between(t,{_f(t0)},{_f(t0 + half * 0.4)})'",
                f"gblur=sigma=9:enable='gte(t,{_f(t0 + half * 0.4)})'"]
    if preset == "shake":
        return []  # impact shake lives entirely on the incoming side
    if preset in ("zoom_out", "zoom_in"):
        # Outgoing side: leave FROM rest, ACCELERATING so max speed lands
        # exactly on the cut (the incoming side decelerates from it).
        win = max(d * _ZOOM_FRACS[preset][0], 1.0 / fps)
        tz = max(0.0, clip_dur - win)
        e = _ease_in(f"clip(({t}-{_f(tz)})/{_f(win)},0,1)")
        z = (f"{e};2-ld(0)" if preset == "zoom_out"    # 2 → 1: pull back
             else f"{e};2+4*ld(0)")                     # 2 → 6: punch in
        step = win / 3.0
        return (_mirror_zoom(z, w, h, fps)
                + _blur_steps([(tz, tz + step, 2), (tz + step, tz + 2 * step, 6),
                               (tz + 2 * step, clip_dur, 12)]))
    raise ValueError(f"unknown transition preset '{preset}'")
