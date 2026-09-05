"""Color grading: per-clip/global grade specs → ffmpeg filters, the built-in
look library (recipes baked to .cube LUTs at image build), and the colour
management that makes the timeline a Rec.709 pipeline.

A grade spec is a dict:
  {"input": {"matrix": "bt2020nc", "primaries": "bt2020", "transfer": "hlg",
             "range": "tv"},                      # declare the SOURCE colorimetry
   "convert": "hlg->rec709",                      # technical conversion into 709
   "exposure": 0.3, "brightness": 0.0, "contrast": 1.06, "saturation": 1.05,
   "gamma": 1.0, "temperature": 5800,
   "curves": {"all": [[0,0],[0.5,0.52],[1,1]], "r": ..., "g": ..., "b": ...}
             or {"preset": "increase_contrast"},
   "lut": "teal-orange" | "looks/x.cube" | ["tech.cube", {"lut": "filmic",
          "strength": 0.6}],                      # chain, per-entry strength
   "strength": 0.7,                               # mix of the creative grade
   "clarity": 0.4, "sharpness": 0.3}              # cas / unsharp, after the LUTs

Order in the chain (the compiler enforces it): input tag → convert → match →
hand grade → LUT chain → [strength mix] → clarity → sharpness. `input`,
`convert` and `match` are technical transforms and never take part in the mix.

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
# 0–1 taste knobs (strength = grade mix, clarity = cas, sharpness = unsharp).
_UNIT_KEYS = ("strength", "clarity", "sharpness")
# "match" (shot color-matching) is validated in composition.py and compiled
# from the renderer-generated LUT — to_filters ignores it by design.
_SPEC_KEYS = (set(_SPEC_BOUNDS) | set(_UNIT_KEYS)
              | {"curves", "lut", "match", "input", "convert"})


# ---------------------------------------------------------------------------
# Colour management: source tagging, the timeline contract, conversions
# ---------------------------------------------------------------------------
#
# The timeline is Rec.709 / limited range. Every media chain declares its
# source colorimetry at the head (setparams) so that every RGB conversion
# downstream — lut3d, curves, exposure, colortemperature, keying, the gbrp
# transitions — decodes with the right matrix; the canvas-fit scale converts
# matrix/range into 709/limited for real; and each chain ends pinned as 709 so
# folds see uniform properties and the encoder writes a truthful VUI + colr.
# Measured 2026-09: an untagged HD source is otherwise decoded with the 601
# matrix, and a setparams pin after the conversion point only relabels.

INPUT_KEYS = ("matrix", "primaries", "transfer", "range")

# Friendly names → ffmpeg setparams/zscale enum names.
_MATRIX_ALIASES = {
    "bt709": "bt709", "709": "bt709", "rec709": "bt709",
    "bt2020nc": "bt2020nc", "bt2020": "bt2020nc", "2020": "bt2020nc",
    "rec2020": "bt2020nc", "bt2020c": "bt2020c",
    "bt470bg": "bt470bg", "601": "bt470bg", "bt601": "bt470bg",
    "smpte170m": "smpte170m", "fcc": "fcc", "ycgco": "ycgco",
}
_PRIMARIES_ALIASES = {
    "bt709": "bt709", "709": "bt709", "rec709": "bt709",
    "bt2020": "bt2020", "2020": "bt2020", "rec2020": "bt2020",
    "bt470bg": "bt470bg", "601": "bt470bg", "bt601": "bt470bg",
    "smpte170m": "smpte170m", "bt470m": "bt470m", "film": "film",
    "smpte431": "smpte431", "smpte432": "smpte432", "p3": "smpte432",
    "dci-p3": "smpte431", "display-p3": "smpte432",
}
_TRANSFER_ALIASES = {
    "bt709": "bt709", "709": "bt709", "rec709": "bt709",
    "arib-std-b67": "arib-std-b67", "hlg": "arib-std-b67",
    "smpte2084": "smpte2084", "pq": "smpte2084", "st2084": "smpte2084",
    "smpte170m": "smpte170m", "601": "smpte170m", "bt601": "smpte170m",
    "iec61966-2-1": "iec61966-2-1", "srgb": "iec61966-2-1",
    "linear": "linear", "bt2020-10": "bt2020-10", "bt2020-12": "bt2020-12",
    "bt470m": "bt470m", "gamma22": "bt470m", "bt470bg": "bt470bg",
    "gamma28": "bt470bg",
}
_RANGE_ALIASES = {
    "tv": "tv", "limited": "tv", "mpeg": "tv",
    "pc": "pc", "full": "pc", "jpeg": "pc",
}
_ALIASES = {"matrix": _MATRIX_ALIASES, "primaries": _PRIMARIES_ALIASES,
            "transfer": _TRANSFER_ALIASES, "range": _RANGE_ALIASES}

# ffprobe reports these stream fields; the compiler maps them onto INPUT_KEYS.
PROBE_FIELDS = {"matrix": "color_space", "primaries": "color_primaries",
                "transfer": "color_transfer", "range": "color_range"}

# The timeline: what every base chain and the composited tail are pinned to.
OUTPUT_TAGS = {"matrix": "bt709", "primaries": "bt709", "transfer": "bt709",
               "range": "tv"}
# Explicit conversion point (canvas fit / fill generation): matrix + range
# into the timeline space — a no-op for 709/limited sources, a real
# conversion for 601-tagged or full-range ones.
OUTPUT_MATRIX_OPTS = "out_color_matrix=bt709:out_range=tv"

_SETPARAMS_KEY = {"matrix": "colorspace", "primaries": "color_primaries",
                  "transfer": "color_trc", "range": "range"}

# Transfer values that are SDR — a convert request on these is a mistake.
SDR_TRANSFERS = frozenset({"bt709", "smpte170m", "bt470bg", "bt470m",
                           "iec61966-2-1", "bt2020-10", "bt2020-12"})
HDR_TRANSFERS = {"arib-std-b67": "HLG", "smpte2084": "PQ"}

# Conversions into the timeline space. A preset carries the source tags it
# implies (an explicit `input` overrides per key) and the filter chain.
#
# hlg->rec709: zscale to display-linear with HLG's 1000-nit reference OOTF
# (npl=1000 puts HLG code 1.0 at linear 1.0), then anchor BT.2408 reference
# white — HLG 75 % = 203 nits — at SDR white: +2.30 EV (1000/203 = 4.926×),
# a Möbius knee (linear below 0.5, soft roll-off to the 4.926 signal peak)
# so highlights above reference white keep detail instead of clipping, then
# 709 primaries/transfer, limited range. Measured: HLG 100/75/50/25 % → SDR
# Y 235/209/139/78; 50 % grey survives an SDR→HLG→SDR round trip exactly.
CONVERT_PRESETS = {
    "hlg->rec709": {
        "input": {"matrix": "bt2020nc", "primaries": "bt2020",
                  "transfer": "arib-std-b67", "range": "tv"},
        "filters": [
            "zscale=t=linear:npl=1000",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "exposure=exposure=2.3",
            "tonemap=tonemap=mobius:param=0.5:peak=4.926:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ],
    },
}


def normalize_input(spec) -> dict:
    """A user `input` object → {key: ffmpeg enum name} for the keys given.
    Raises ValueError on an unknown key or value."""
    if not isinstance(spec, dict):
        raise ValueError("color.input must be an object with matrix / "
                         "primaries / transfer / range")
    unknown = set(spec) - set(INPUT_KEYS)
    if unknown:
        raise ValueError(f"unknown color.input keys {sorted(unknown)} "
                         f"(accepted: {list(INPUT_KEYS)})")
    out: dict[str, str] = {}
    for key, raw in spec.items():
        if raw is None:
            continue
        name = str(raw).strip().lower()
        table = _ALIASES[key]
        if name not in table:
            raise ValueError(
                f"color.input.{key} '{raw}' is not a known value — accepted: "
                + ", ".join(sorted(set(table))))
        out[key] = table[name]
    return out


def default_tags(height) -> dict:
    """Player convention for an untagged source: 601 up to SD, 709 above
    (and when the height is unknown)."""
    try:
        sd = 0 < int(height or 0) <= 576
    except (TypeError, ValueError):
        sd = False
    if sd:
        return {"matrix": "bt470bg", "primaries": "bt470bg",
                "transfer": "smpte170m", "range": "tv"}
    return dict(OUTPUT_TAGS)


def probe_tags(probe_color) -> dict:
    """ffprobe's stream colour fields → {key: value} for the KNOWN fields
    only (ffprobe omits unknown ones or prints 'unknown')."""
    out: dict[str, str] = {}
    for key, field in PROBE_FIELDS.items():
        val = (probe_color or {}).get(field) or (probe_color or {}).get(key)
        if val and str(val).lower() not in ("unknown", "unspecified"):
            out[key] = str(val)
    return out


def head_tags(spec, probe_color, height) -> dict:
    """The colour properties a media chain must DECLARE at its head.

    Explicit `input` keys always win. A `convert` preset supplies the rest of
    its implied source tags (zscale needs a fully known input). Otherwise
    only the fields the probe reports UNKNOWN are declared, with the player
    default — tagged fields already arrive on the decoded frames.
    """
    spec = spec if isinstance(spec, dict) else {}
    explicit = normalize_input(spec["input"]) if spec.get("input") else {}
    convert = spec.get("convert")
    if convert:
        tags = dict(CONVERT_PRESETS[convert]["input"])
        tags.update(explicit)
        return tags
    known = probe_tags(probe_color)
    defaults = default_tags(height)
    tags = {k: defaults[k] for k in INPUT_KEYS if k not in known}
    tags.update(explicit)
    return tags


def tag_filter(tags: dict) -> str:
    """{matrix, primaries, transfer, range} → one `setparams=` atom ('' when
    there is nothing to declare)."""
    parts = [f"{_SETPARAMS_KEY[k]}={tags[k]}" for k in INPUT_KEYS if tags.get(k)]
    return "setparams=" + ":".join(parts) if parts else ""


OUTPUT_PIN = tag_filter(OUTPUT_TAGS)


def convert_filters(spec) -> list[str]:
    name = (spec or {}).get("convert") if isinstance(spec, dict) else None
    if not name:
        return []
    return list(CONVERT_PRESETS[name]["filters"])


def sharpen_filters(spec) -> list[str]:
    """clarity (contrast-adaptive, halo-free) → sharpness (classic 5x5 USM,
    luma only). Both run AFTER every LUT and at the render canvas — the
    compiler places them after the canvas fit and the grade mix."""
    out: list[str] = []
    clarity = float((spec or {}).get("clarity") or 0)
    if clarity > 0:
        out.append(f"cas=strength={_fmt(clarity)}")
    sharp = float((spec or {}).get("sharpness") or 0)
    if sharp > 0:
        out.append(f"unsharp=5:5:{_fmt(1.5 * sharp)}:5:5:0")
    return out


def grade_strength(spec) -> float:
    s = (spec or {}).get("strength") if isinstance(spec, dict) else None
    return 1.0 if s is None else min(1.0, max(0.0, float(s)))


# ---------------------------------------------------------------------------
# LUT chain: entries, keys, strength baking
# ---------------------------------------------------------------------------


def lut_entries(spec) -> list[tuple[str, float]]:
    """`lut` (string | list of string | {"lut", "strength"}) → [(ref, strength)]
    in application order. Raises ValueError on a malformed entry."""
    raw = (spec or {}).get("lut") if isinstance(spec, dict) else None
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        raise ValueError("color.lut list is empty")
    out: list[tuple[str, float]] = []
    for item in items:
        strength = 1.0
        if isinstance(item, dict):
            ref = item.get("lut")
            unknown = set(item) - {"lut", "strength"}
            if unknown:
                raise ValueError(f"unknown lut entry keys {sorted(unknown)} "
                                 "(accepted: lut, strength)")
            if item.get("strength") is not None:
                try:
                    strength = float(item["strength"])
                except (TypeError, ValueError):
                    raise ValueError("lut entry strength must be a number 0–1")
                if not 0.0 <= strength <= 1.0:
                    raise ValueError("lut entry strength must be 0–1")
        else:
            ref = item
        if not isinstance(ref, str) or not ref:
            raise ValueError("each lut entry must be a built-in look name or a "
                             ".cube path — a string or {\"lut\": …, "
                             "\"strength\": 0–1}")
        out.append((ref, strength))
    return out


def lut_key(ref: str, strength: float = 1.0) -> str:
    """Key of a staged LUT in the compiler's `luts` map. A full-strength
    entry keys by its ref alone (unchanged contract for existing callers)."""
    s = min(1.0, max(0.0, float(strength)))
    return ref if s >= 1.0 else f"{ref}#{s:g}"


def lut_refs(spec) -> list[str]:
    """Distinct non-built-in file refs of a grade's LUT chain (for path
    resolution and existence checks)."""
    seen, out = set(), []
    for ref, _ in lut_entries(spec):
        if not is_builtin_look(ref) and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def rewrite_lut_refs(spec: dict, mapping: dict) -> None:
    """Rewrite a grade's LUT refs in place through `mapping` (path
    resolution), preserving the string / list / entry shapes."""
    raw = spec.get("lut")
    if raw is None:
        return

    def one(item):
        if isinstance(item, dict):
            if item.get("lut") in mapping:
                item = dict(item)
                item["lut"] = mapping[item["lut"]]
            return item
        return mapping.get(item, item)

    spec["lut"] = [one(i) for i in raw] if isinstance(raw, list) else one(raw)


def _parse_cube(text: str) -> dict:
    """Minimal .cube reader: {size, dim (1|3), dmin, dmax, header (kept
    lines), rows [(r,g,b), …]}. Raises ValueError on anything unreadable."""
    size = dim = None
    dmin, dmax = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    header: list[str] = []
    rows: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            header.append(line)
            continue
        up = s.upper()
        if up.startswith("LUT_3D_SIZE"):
            size, dim = int(s.split()[1]), 3
            continue
        if up.startswith("LUT_1D_SIZE"):
            size, dim = int(s.split()[1]), 1
            continue
        if up.startswith("DOMAIN_MIN"):
            dmin = tuple(float(v) for v in s.split()[1:4])
            header.append(line)
            continue
        if up.startswith("DOMAIN_MAX"):
            dmax = tuple(float(v) for v in s.split()[1:4])
            header.append(line)
            continue
        if up.startswith("TITLE") or up.startswith("LUT_1D_INPUT_RANGE") \
                or up.startswith("LUT_3D_INPUT_RANGE"):
            header.append(line)
            continue
        parts = s.split()
        try:
            rows.append((float(parts[0]), float(parts[1]), float(parts[2])))
        except (IndexError, ValueError):
            raise ValueError(f"unreadable .cube line: {s[:40]!r}")
    if size is None or dim is None:
        raise ValueError(".cube has no LUT_3D_SIZE / LUT_1D_SIZE")
    expected = size ** dim
    if len(rows) != expected:
        raise ValueError(f".cube declares {expected} entries, found {len(rows)}")
    return {"size": size, "dim": dim, "dmin": dmin, "dmax": dmax,
            "header": header, "rows": rows}


def blend_cube(src_path: str, dst_path: str, strength: float) -> None:
    """Write a copy of a .cube whose output is `identity·(1−s) + lut·s`.
    Per-pixel linear blending of the graded frame against the ungraded one
    is exactly this in LUT space, so the strength costs nothing at render
    time. Identity is computed inside the LUT's input domain."""
    import numpy as np

    s = min(1.0, max(0.0, float(strength)))
    cube = _parse_cube(Path(src_path).read_text(encoding="utf-8", errors="replace"))
    size, dim = cube["size"], cube["dim"]
    dmin = np.asarray(cube["dmin"], dtype=np.float64)
    dmax = np.asarray(cube["dmax"], dtype=np.float64)
    axis = np.linspace(0.0, 1.0, size)
    if dim == 3:
        b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
        ident = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)
    else:
        ident = np.stack([axis, axis, axis], axis=-1)
    ident = dmin + ident * (dmax - dmin)
    lut = np.asarray(cube["rows"], dtype=np.float64)
    out = ident * (1.0 - s) + lut * s
    lines = list(cube["header"])
    lines.append(f"LUT_{dim}D_SIZE {size}")
    lines.extend(f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in out)
    Path(dst_path).write_text("\n".join(lines) + "\n", encoding="ascii")


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
    for key in _UNIT_KEYS:
        if key in spec and spec[key] is not None:
            try:
                v = float(spec[key])
                if not 0.0 <= v <= 1.0:
                    problems.append(f"color.{key} must be 0–1")
            except (TypeError, ValueError):
                problems.append(f"color.{key} must be a number 0–1")
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
    if spec.get("lut") is not None:
        try:
            lut_entries(spec)
        except ValueError as exc:
            problems.append(f"color.lut: {exc}")
    if spec.get("input") is not None:
        try:
            normalize_input(spec["input"])
        except ValueError as exc:
            problems.append(str(exc))
    convert = spec.get("convert")
    if convert is not None and convert not in CONVERT_PRESETS:
        problems.append(
            f"unknown color.convert '{convert}' — available: "
            + ", ".join(sorted(CONVERT_PRESETS)))
    return problems


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _curves_channel(pts: list) -> str:
    return " ".join(f"{_fmt(float(x))}/{_fmt(float(y))}" for x, y in pts)


def to_filters(spec: dict) -> list[str]:
    """Hand-grade atoms of a spec, in order (exposure → eq → temperature →
    curves). The LUT chain, the conversion and sharpening are separate
    builders — the compiler assembles the full order."""
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
