"""Caption parsing, cue grouping, and ASS generation."""

import json

import pytest

import captions as caps


WORDS = [
    {"word": "Meet", "start": 0.10, "end": 0.35},
    {"word": "OtoDock,", "start": 0.35, "end": 0.90},
    {"word": "your", "start": 0.95, "end": 1.10},
    {"word": "agents", "start": 1.10, "end": 1.60},
    {"word": "everywhere.", "start": 1.60, "end": 2.30},
    # 1.5s gap → new cue
    {"word": "Self-hosted.", "start": 3.80, "end": 4.60},
]


def _transcript_file(tmp_path, words=WORDS):
    p = tmp_path / "demo.transcript.json"
    p.write_text(json.dumps({"text": "…", "words": words}))
    return str(p)


def test_parse_transcript_json(tmp_path):
    words = caps.parse_transcript_json(_transcript_file(tmp_path))
    assert len(words) == 6
    assert words[0] == {"word": "Meet", "start": 0.10, "end": 0.35}


def test_parse_transcript_rejects_missing_words(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"text": "no words"}))
    with pytest.raises(caps.CaptionError, match="words"):
        caps.parse_transcript_json(str(p))


def test_sanitize_strips_ass_control_chars(tmp_path):
    words = [{"word": "he{llo}\\N", "start": 0, "end": 1}]
    parsed = caps.parse_transcript_json(_transcript_file(tmp_path, words))
    assert parsed[0]["word"] == "helloN"


def test_group_words_splits_on_gap_and_punctuation():
    cues = caps.group_words(WORDS, max_words=4)
    # 'OtoDock,' doesn't end a sentence; 'everywhere.' does; the gap also splits.
    assert [len(c) for c in cues] == [4, 1, 1]


def test_group_words_splits_on_phrase_pause():
    # A ~0.6s pause is a spoken phrase break — the old 0.8s threshold glued
    # "…cove" + "Crystal…" into one cue (social-cut rehearsal).
    words = [
        {"word": "perfect", "start": 0.0, "end": 0.4},
        {"word": "cove", "start": 0.4, "end": 0.8},
        {"word": "Crystal", "start": 1.4, "end": 1.8},
        {"word": "water", "start": 1.8, "end": 2.2},
    ]
    assert [len(c) for c in caps.group_words(words, max_words=4)] == [2, 2]


def test_karaoke_k_durations_are_centiseconds(tmp_path):
    ass = caps.build_ass(_transcript_file(tmp_path), 1080, 1920,
                         preset="karaoke")
    # First word runs 0.10→0.35 but fills to the NEXT word's start (0.35):
    # 25cs. Verify a \k tag with that value exists on the first line.
    line = next(l for l in ass.splitlines() if l.startswith("Dialogue:"))
    assert "{\\k25}Meet" in line
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_karaoke_colors_sung_vs_unsung():
    assert caps.hex_to_ass("#FFD400") == "&H0000D4FF&"
    assert caps.hex_to_ass("#FFFFFF") == "&H00FFFFFF&"
    assert caps.hex_to_ass("#000000", 0x60) == "&H60000000&"


def test_word_pop_one_event_per_word_no_overlap(tmp_path):
    ass = caps.build_ass(_transcript_file(tmp_path), 1080, 1920,
                         preset="word-pop")
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == len(WORDS)
    assert "\\t(0,90," in dialogues[0]          # pop-in transform
    assert "MEET" in dialogues[0]               # uppercase default


def test_clean_preset_plain_sentence_cues(tmp_path):
    ass = caps.build_ass(_transcript_file(tmp_path), 1920, 1080,
                         preset="clean", position="lower_third")
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert all("\\k" not in d for d in dialogues)
    assert "Meet OtoDock, your agents" in dialogues[0]


def test_positions_map_to_alignment(tmp_path):
    top = caps.build_ass(_transcript_file(tmp_path), 1080, 1920,
                         preset="clean", position="top")
    center = caps.build_ass(_transcript_file(tmp_path), 1080, 1920,
                            preset="clean", position="center")
    assert ",8," in top.split("Style: Cap,")[1]
    assert ",5," in center.split("Style: Cap,")[1]


def test_offset_shifts_all_events(tmp_path):
    ass = caps.build_ass(_transcript_file(tmp_path), 1080, 1920,
                         preset="clean", offset=10.0)
    first = next(l for l in ass.splitlines() if l.startswith("Dialogue:"))
    assert first.split(",")[1] == "0:00:10.10"


def test_srt_parsing_and_plain_render(tmp_path):
    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nSecond line\n")
    cues = caps.parse_srt(str(srt))
    assert cues == [
        {"start": 1.0, "end": 2.5, "text": "Hello world"},
        {"start": 3.0, "end": 4.0, "text": "Second line"},
    ]
    ass = caps.build_ass(str(srt), 1920, 1080, preset="clean")
    assert "Hello world" in ass


def test_srt_with_karaoke_distributes_word_timing(tmp_path):
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\none two\n")
    ass = caps.build_ass(str(srt), 1080, 1920, preset="karaoke")
    line = next(l for l in ass.splitlines() if l.startswith("Dialogue:"))
    assert "{\\k100}one" in line  # 2s / 2 words = 100 cs each


def test_ass_source_passes_through(tmp_path):
    src = tmp_path / "styled.ass"
    src.write_text("[Script Info]\nScriptType: v4.00+\n")
    assert caps.build_ass(str(src), 1080, 1920) == src.read_text()


def test_font_size_scales_with_playres(tmp_path):
    small = caps.build_ass(_transcript_file(tmp_path), 540, 960, preset="karaoke")
    large = caps.build_ass(_transcript_file(tmp_path), 1080, 1920, preset="karaoke")
    size_of = lambda a: int(a.split("Style: Cap,Inter,")[1].split(",")[0])
    assert size_of(large) == 2 * size_of(small)
