"""Color grading: per-clip/global grade specs → ffmpeg filters, plus the
built-in look library (recipes baked to .cube LUTs at image build).

A grade spec is a dict:
  {"exposure": 0.3, "brightness": 0.0, "contrast": 1.06, "saturation": 1.05,
   "gamma": 1.0, "temperature": 5800,
   "curves": {"all": [[0,0],[0.5,0.52],[1,1]], "r": ..., "g": ..., "b": ...}
             or {"preset": "increase_contrast"},
   "lut": "teal-orange" (built-in look) or a .cube path in the workspace}

The look library is OURS: every .cube is generated from the numeric recipes
below — nothing third-party is redistributed (most "free LUT" packs are
free-to-use, not free-to-redistribute).
"""

import os
from pathlib import Path

LOOKS_DIR = os.environ.get("VIDEO_TOOLS_LOOKS_DIR", "/app/looks")

_CURVES_PRESETS = {
    "color_negative", "cross_process", "darker", "increase_contrast",
    "lighter", "linear_contrast", "medium_contrast", "negative",
    "strong_contrast", "vintage",
}

_SPEC_BOUNDS = {
    "exposure": (-3.0, 3.0),
    "brightness": (-0.5, 0.5),
    "contrast": (0.3, 2.0),
    "saturation": (0.0, 2.5),
    "gamma": (0.4, 2.5),
    "temperature": (2000, 12000),
}
# "match" (shot color-matching) is validated in composition.py and compiled
# from the renderer-generated LUT — to_filters ignores it by design.
_SPEC_KEYS = set(_SPEC_BOUNDS) | {"curves", "lut", "match"}


# ---------------------------------------------------------------------------
# Built-in look recipes
# ---------------------------------------------------------------------------
#
# Recipe fields (applied in this order, all in normalized [0,1] float):
#   gains    (r, g, b) channel multipliers — white balance / tint
#   lift     raised black point (0.0–0.1 — the "matte/faded" look)
#   contrast factor around a 0.5 pivot
#   saturation factor (Rec.709 luma-preserving)
#   curves   per-channel control points [(x, y), ...] — spline via interp

BUILTIN_LOOKS: dict[str, dict] = {
    "teal-orange": {
        "description": "The modern blockbuster grade: teal shadows, warm highlights.",
        "gains": (1.0, 1.0, 1.0),
        "contrast": 1.05,
        "saturation": 1.06,
        "curves": {
            "r": [(0.0, 0.0), (0.25, 0.22), (0.5, 0.50), (0.75, 0.79), (1.0, 1.0)],
            "b": [(0.0, 0.07), (0.25, 0.29), (0.5, 0.50), (0.75, 0.71), (1.0, 0.93)],
        },
    },
    "filmic": {
        "description": "Soft S-curve, gently lifted blacks, restrained saturation.",
        "lift": 0.03,
        "contrast": 1.04,
        "saturation": 0.95,
        "curves": {
            "all": [(0.0, 0.0), (0.25, 0.22), (0.5, 0.5), (0.75, 0.78), (1.0, 1.0)],
        },
    },
    "clean-punch": {
        "description": "Neutral with a little extra contrast and color — safe default.",
        "contrast": 1.08,
        "saturation": 1.08,
    },
    "bw-classic": {
        "description": "Black & white with strong midtone contrast.",
        "saturation": 0.0,
        "contrast": 1.12,
        "lift": 0.02,
    },
    "warm-golden": {
        "description": "Golden-hour warmth for people and interiors.",
        "gains": (1.06, 1.0, 0.92),
        "saturation": 1.04,
        "lift": 0.01,
    },
    "cool-matte": {
        "description": "Cool, low-contrast matte — tech/product mood.",
        "gains": (0.95, 1.0, 1.07),
        "lift": 0.05,
        "contrast": 0.94,
        "saturation": 0.92,
    },
    "vivid": {
        "description": "Saturated and contrasty — thumbnails, motion graphics.",
        "saturation": 1.18,
        "contrast": 1.10,
    },
    "faded-retro": {
        "description": "Lifted blacks, muted color, slightly warm — nostalgic.",
        "lift": 0.07,
        "contrast": 0.88,
        "saturation": 0.85,
        "gains": (1.03, 1.0, 0.95),
    },
}


def is_builtin_look(name) -> bool:
    return isinstance(name, str) and name in BUILTIN_LOOKS


def resolve_lut(lut: str, resolve_path) -> str:
    """A grade's ``lut`` → absolute .cube path (built-in look or user file)."""
    if is_builtin_look(lut):
        return str(Path(LOOKS_DIR) / f"{lut}.cube")
    return resolve_path(lut)


# ---------------------------------------------------------------------------
# Spec validation + ffmpeg filter mapping
# ---------------------------------------------------------------------------


def validate_color_spec(spec: dict) -> list[str]:
    """Structural validation of a grade spec → list of problems."""
    problems: list[str] = []
    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        problems.append(
            f"unknown color keys {sorted(unknown)} (accepted: {sorted(_SPEC_KEYS)})")
    for key, (lo, hi) in _SPEC_BOUNDS.items():
        if key in spec and spec[key] is not None:
            try:
                v = float(spec[key])
                if not lo <= v <= hi:
                    problems.append(f"color.{key} must be {lo}–{hi}")
            except (TypeError, ValueError):
                problems.append(f"color.{key} must be a number")
    curves = spec.get("curves")
    if curves is not None:
        if not isinstance(curves, dict):
            problems.append("color.curves must be an object")
        elif "preset" in curves:
            if curves["preset"] not in _CURVES_PRESETS:
                problems.append(
                    f"unknown curves preset '{curves['preset']}' — valid: "
                    + ", ".join(sorted(_CURVES_PRESETS)))
        else:
            for ch, pts in curves.items():
                if ch not in ("r", "g", "b", "all"):
                    problems.append(f"curves channel must be r/g/b/all, got '{ch}'")
                    continue
                if (not isinstance(pts, list) or len(pts) < 2
                        or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pts)):
                    problems.append(f"curves.{ch} must be a list of [x, y] pairs")
                    continue
                xs = [p[0] for p in pts]
                if not all(0 <= p[i] <= 1 for p in pts for i in (0, 1)):
                    problems.append(f"curves.{ch} points must be within 0–1")
                elif xs != sorted(xs):
                    problems.append(f"curves.{ch} x values must be increasing")
    lut = spec.get("lut")
    if lut is not None and not isinstance(lut, str):
        problems.append("color.lut must be a built-in look name or a .cube path")
    return problems


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _curves_channel(pts: list) -> str:
    return " ".join(f"{_fmt(float(x))}/{_fmt(float(y))}" for x, y in pts)


def to_filters(spec: dict) -> list[str]:
    """Grade spec → ordered ffmpeg filter atoms (lut3d appended by the
    compiler after path resolution)."""
    filters: list[str] = []
    if spec.get("exposure"):
        filters.append(f"exposure=exposure={_fmt(float(spec['exposure']))}")

    eq_parts = []
    if spec.get("brightness"):
        eq_parts.append(f"brightness={_fmt(float(spec['brightness']))}")
    if spec.get("contrast") not in (None, 1, 1.0):
        eq_parts.append(f"contrast={_fmt(float(spec['contrast']))}")
    if spec.get("saturation") not in (None, 1, 1.0):
        eq_parts.append(f"saturation={_fmt(float(spec['saturation']))}")
    if spec.get("gamma") not in (None, 1, 1.0):
        eq_parts.append(f"gamma={_fmt(float(spec['gamma']))}")
    if eq_parts:
        filters.append("eq=" + ":".join(eq_parts))

    if spec.get("temperature"):
        filters.append(f"colortemperature=temperature={int(spec['temperature'])}")

    curves = spec.get("curves")
    if isinstance(curves, dict) and curves:
        if "preset" in curves:
            filters.append(f"curves=preset={curves['preset']}")
        else:
            parts = []
            chan_opt = {"r": "red", "g": "green", "b": "blue", "all": "all"}
            for ch in ("all", "r", "g", "b"):
                if ch in curves:
                    parts.append(f"{chan_opt[ch]}='{_curves_channel(curves[ch])}'")
            if parts:
                filters.append("curves=" + ":".join(parts))
    return filters


# ---------------------------------------------------------------------------
# Look baking (recipes → .cube)
# ---------------------------------------------------------------------------


def _apply_recipe(rgb, recipe: dict):
    """Apply a recipe to an (N, 3) float array in [0, 1]."""
    import numpy as np

    out = rgb.astype(np.float64).copy()

    gains = recipe.get("gains")
    if gains:
        out *= np.asarray(gains, dtype=np.float64)

    lift = float(recipe.get("lift", 0.0))
    if lift:
        out = lift + out * (1.0 - lift)

    contrast = float(recipe.get("contrast", 1.0))
    if contrast != 1.0:
        out = (out - 0.5) * contrast + 0.5

    sat = recipe.get("saturation")
    if sat is not None and float(sat) != 1.0:
        luma = (out * np.array([0.2126, 0.7152, 0.0722])).sum(axis=-1, keepdims=True)
        out = luma + (out - luma) * float(sat)

    curves = recipe.get("curves")
    if curves:
        import numpy as np  # noqa: F811 — keep local for clarity

        def _interp(vals, pts):
            xs = np.array([p[0] for p in pts], dtype=np.float64)
            ys = np.array([p[1] for p in pts], dtype=np.float64)
            return np.interp(vals, xs, ys)

        if "all" in curves:
            for c in range(3):
                out[:, c] = _interp(out[:, c].clip(0, 1), curves["all"])
        for ch, c in (("r", 0), ("g", 1), ("b", 2)):
            if ch in curves:
                out[:, c] = _interp(out[:, c].clip(0, 1), curves[ch])

    return out.clip(0.0, 1.0)


def bake_cube(recipe: dict, size: int = 33) -> str:
    """Bake a recipe into .cube text (red axis fastest, per the spec)."""
    import numpy as np

    axis = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)
    graded = _apply_recipe(grid, recipe)
    lines = [
        "# Generated by OtoDock video-tools - recipe-owned look, redistributable",
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    lines.extend(
        f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in graded
    )
    return "\n".join(lines) + "\n"


def emit_builtin_looks(target_dir: str, size: int = 33) -> list[str]:
    """Write every built-in look as <name>.cube into ``target_dir``."""
    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, recipe in BUILTIN_LOOKS.items():
        path = out_dir / f"{name}.cube"
        path.write_text(bake_cube(recipe, size), encoding="ascii")
        written.append(str(path))
    return written
