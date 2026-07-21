"""Caption styling: word-timestamped transcripts / SRT → styled ASS.

Consumes the platform transcript format (``{"words": [{"word", "start",
"end"}, ...]}`` — what transcribe-mcp writes as ``<stem>.transcript.json``),
plain SRT, or a ready-made ASS (burned as-is). Presets cover the current
short-form styles plus broadcast-clean; all sizes are PlayResY-relative so
the same preset renders correctly at any project resolution — including
preview renders at reduced scale, because the ASS is regenerated against the
render canvas.

ASS notes (the classic bugs, so they stay fixed):
- colors are &HAABBGGRR (BGR, alpha 00=opaque FF=transparent)
- \\k karaoke durations are CENTISECONDS; \\t() times are MILLISECONDS
- Bold in a Style line is -1 (true) / 0 (false), not 1
- '{' '}' and '\\' inside caption text corrupt override blocks — sanitized out
"""

import json
import re
from pathlib import Path

DEFAULT_PRESET = "karaoke"

# Per-preset defaults. font_scale is fraction of PlayResY; margin_v_scale is
# the bottom/top margin for lower_third/top positions.
PRESETS: dict[str, dict] = {
    # Active word fills with the highlight color as it is spoken (TikTok
    # karaoke). Cues of a few words, bottom-centered.
    "karaoke": {
        "font": "Inter",
        "font_scale": 0.050,
        "max_words": 4,
        "per_word": False,
        "karaoke": True,
        "uppercase": False,
        "outline_scale": 0.045,
        "shadow_scale": 0.020,
    },
    # One word at a time, big and bold with a pop-in (Hormozi style).
    "word-pop": {
        "font": "Inter",
        "font_scale": 0.062,
        "max_words": 1,
        "per_word": True,
        "karaoke": False,
        "uppercase": True,
        "outline_scale": 0.055,
        "shadow_scale": 0.025,
    },
    # Broadcast-style sentence cues, no per-word effects.
    "clean": {
        "font": "Inter",
        "font_scale": 0.042,
        "max_words": 7,
        "per_word": False,
        "karaoke": False,
        "uppercase": False,
        "outline_scale": 0.035,
        "shadow_scale": 0.015,
    },
    # Small, unobtrusive, on a translucent box.
    "minimal": {
        "font": "Inter",
        "font_scale": 0.034,
        "max_words": 8,
        "per_word": False,
        "karaoke": False,
        "uppercase": False,
        "boxed": True,
        "outline_scale": 0.012,
        "shadow_scale": 0.0,
    },
}

_POSITION_ALIGN = {"lower_third": 2, "center": 5, "top": 8}

DEFAULT_HIGHLIGHT = "#FFD400"
_MAX_CUE_SPAN = 3.4       # seconds a multi-word cue may cover
# Silence gap that forces a new cue. Within-phrase word gaps in real speech
# sit under ~0.3s; a phrase pause runs 0.5-0.9s. The original 0.8 let a
# 0.79s pause glue two phrases into one cue ("...cove CRYSTAL") — found in
# the Phase-4 social dress rehearsal.
_CUE_GAP_SPLIT = 0.45


class CaptionError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    """Strip characters that would corrupt ASS override blocks."""
    return re.sub(r"[{}\\]", "", str(text)).strip()


def parse_transcript_json(path: str) -> list[dict]:
    """Word list from a platform transcript JSON → [{word, start, end}]."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaptionError(f"transcript is not valid JSON: {exc}")
    if isinstance(data, dict):
        words = data.get("words")
        if words is None and isinstance(data.get("transcript"), dict):
            words = data["transcript"].get("words")
    elif isinstance(data, list):
        words = data
    else:
        words = None
    if not isinstance(words, list) or not words:
        raise CaptionError(
            "transcript JSON has no 'words' array with per-word timestamps — "
            "expected the transcribe-mcp <stem>.transcript.json format")
    out = []
    for w in words:
        if not isinstance(w, dict):
            continue
        text = _sanitize(w.get("word", ""))
        try:
            start, end = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and end >= start >= 0:
            out.append({"word": text, "start": start, "end": end})
    if not out:
        raise CaptionError("transcript contained no usable timestamped words")
    return out


_SRT_TIME = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)


def parse_srt(path: str) -> list[dict]:
    """SRT → cues [{start, end, text}]."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1.ljust(3, "0")) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2.ljust(3, "0")) / 1000
        lines = block[m.end():].strip().splitlines()
        body = _sanitize(" ".join(line.strip() for line in lines if line.strip()))
        if body and end > start:
            cues.append({"start": start, "end": end, "text": body})
    if not cues:
        raise CaptionError("no cues found in SRT file")
    return cues


def _cues_to_words(cues: list[dict]) -> list[dict]:
    """Approximate per-word timing by splitting each cue evenly — used when a
    karaoke preset is asked to render from an SRT (no word timestamps)."""
    words = []
    for cue in cues:
        parts = [p for p in cue["text"].split() if p]
        if not parts:
            continue
        span = (cue["end"] - cue["start"]) / len(parts)
        for i, p in enumerate(parts):
            words.append({
                "word": p,
                "start": cue["start"] + i * span,
                "end": cue["start"] + (i + 1) * span,
            })
    return words


# ---------------------------------------------------------------------------
# Cue grouping
# ---------------------------------------------------------------------------


def group_words(words: list[dict], max_words: int) -> list[list[dict]]:
    """Group words into cues: at most ``max_words`` each, split on silence
    gaps > _CUE_GAP_SPLIT and spans > _MAX_CUE_SPAN, and break after
    sentence-ending punctuation."""
    cues: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            span = w["end"] - current[0]["start"]
            ended = current[-1]["word"][-1:] in ".!?;"
            if (len(current) >= max_words or gap > _CUE_GAP_SPLIT
                    or span > _MAX_CUE_SPAN or ended):
                cues.append(current)
                current = []
        current.append(w)
    if current:
        cues.append(current)
    return cues


# ---------------------------------------------------------------------------
# ASS generation
# ---------------------------------------------------------------------------


def hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' → '&HAABBGGRR&' (ASS BGR order; alpha 0=opaque 255=clear)."""
    c = hex_color.lstrip("#")
    if len(c) != 6:
        raise CaptionError(f"expected #RRGGBB color, got '{hex_color}'")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha:02X}{b}{g}{r}&".upper()


def _ts(seconds: float) -> str:
    """ASS timestamp H:MM:SS.cc."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(
    source_path: str,
    play_w: int,
    play_h: int,
    preset: str = DEFAULT_PRESET,
    position: str = "lower_third",
    font_size: int | None = None,
    highlight_color: str | None = None,
    uppercase: bool | None = None,
    max_words_per_cue: int | None = None,
    offset: float = 0.0,
) -> str:
    """Build a styled ASS document from a transcript JSON or SRT.

    ``play_w``/``play_h`` must match the RENDER canvas — regenerate for
    preview renders so text scales with the frame.
    """
    ext = Path(source_path).suffix.lower()
    if ext == ".ass":
        return Path(source_path).read_text(encoding="utf-8-sig", errors="replace")
    if preset not in PRESETS:
        raise CaptionError(f"unknown preset '{preset}' — valid: {', '.join(sorted(PRESETS))}")
    cfg = PRESETS[preset]

    plain_cues: list[dict] | None = None
    if ext == ".json":
        words = parse_transcript_json(source_path)
    elif ext == ".srt":
        cues = parse_srt(source_path)
        if cfg["karaoke"] or cfg["per_word"]:
            words = _cues_to_words(cues)
        else:
            words, plain_cues = [], cues
    else:
        raise CaptionError(f"unsupported caption source '{ext}' (use .json/.srt/.ass)")

    if offset:
        if plain_cues is not None:
            plain_cues = [{**c, "start": c["start"] + offset, "end": c["end"] + offset}
                          for c in plain_cues]
        else:
            words = [{**w, "start": w["start"] + offset, "end": w["end"] + offset}
                     for w in words]

    upper = cfg["uppercase"] if uppercase is None else bool(uppercase)
    size = int(font_size or round(cfg["font_scale"] * play_h))
    # Outline/shadow proportional to font size (≈9% / 4% of the glyph height
    # reads as a solid short-form stroke without swallowing thin fonts).
    outline = max(1, round(size * 0.09)) if cfg["outline_scale"] else 0
    shadow = round(size * 0.04) if cfg["shadow_scale"] else 0
    align = _POSITION_ALIGN.get(position, 2)
    margin_v = round(play_h * (0.13 if align != 5 else 0.02))
    margin_lr = round(play_w * 0.06)

    white = hex_to_ass("#FFFFFF")
    highlight = hex_to_ass(highlight_color or DEFAULT_HIGHLIGHT)
    black = hex_to_ass("#000000")
    box = hex_to_ass("#000000", alpha=0x60)

    if cfg.get("boxed"):
        border_style, outline_col = 3, box
        outline = max(4, round(size * 0.22))  # box padding
    else:
        border_style, outline_col = 1, black

    # Karaoke: PrimaryColour is the SUNG color, SecondaryColour the unsung.
    primary = highlight if cfg["karaoke"] else white
    secondary = white

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{cfg['font']},{size},{primary},{secondary},{outline_col},{hex_to_ass('#000000', 0x50)},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{align},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []

    def _event(start: float, end: float, text: str) -> None:
        events.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{text}"
        )

    if plain_cues is not None:
        for cue in plain_cues:
            text = cue["text"].upper() if upper else cue["text"]
            _event(cue["start"], cue["end"], text)
    elif cfg["per_word"]:
        # One word per event; end clamps at the next word's start so pops
        # never overlap. \t times are MILLISECONDS.
        for i, w in enumerate(words):
            text = w["word"].upper() if upper else w["word"]
            start = w["start"]
            hard_end = w["end"] + 0.28
            if i + 1 < len(words):
                hard_end = min(hard_end, words[i + 1]["start"])
            end = max(hard_end, start + 0.12)
            pop = r"{\fscx70\fscy70\t(0,90,\fscx100\fscy100)}"
            _event(start, end, pop + text)
    elif cfg["karaoke"]:
        # \k durations are CENTISECONDS; each word's fill runs to the next
        # word's start so the highlight moves continuously.
        for cue in group_words(words, max_words_per_cue or cfg["max_words"]):
            start, end = cue[0]["start"], cue[-1]["end"]
            parts = []
            for i, w in enumerate(cue):
                nxt = cue[i + 1]["start"] if i + 1 < len(cue) else w["end"]
                kdur = max(1, round((max(nxt, w["end"]) - w["start"]) * 100))
                text = w["word"].upper() if upper else w["word"]
                parts.append(f"{{\\k{kdur}}}{text}")
            _event(start, end + 0.05, " ".join(parts))
    else:
        for cue in group_words(words, max_words_per_cue or cfg["max_words"]):
            text = " ".join(w["word"] for w in cue)
            if upper:
                text = text.upper()
            _event(cue[0]["start"], cue[-1]["end"] + 0.05, text)

    return header + "\n".join(events) + "\n"
