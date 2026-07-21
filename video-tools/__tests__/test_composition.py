"""Schema, timeline math, validation, and edit operations."""

import composition as comp_mod


def _comp(clips=None, tracks_extra=None, **overrides):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = clips or []
    if tracks_extra:
        comp["tracks"].extend(tracks_extra)
    comp.update(overrides)
    return comp


MEDIA = {
    "a.mp4": {"duration": 10.0, "has_video": True, "has_audio": True},
    "b.mp4": {"duration": 8.0, "has_video": True, "has_audio": True},
    "music.wav": {"duration": 60.0, "has_video": False, "has_audio": True},
}


def test_new_composition_shape():
    comp = comp_mod.new_composition()
    assert comp["version"] == comp_mod.SCHEMA_VERSION
    assert comp["tracks"][0]["kind"] == "video"
    assert comp["audio_master"]["loudnorm"] is True


def test_clip_source_kind_exclusive():
    assert comp_mod.clip_source_kind({"src": "a.mp4"}) == "media"
    assert comp_mod.clip_source_kind({"image": "x.png"}) == "image"
    assert comp_mod.clip_source_kind({"fill": "#000000", "duration": 2}) == "fill"


def test_grade_does_not_conflict_with_source_detection():
    clip = {"src": "a.mp4", "color": {"saturation": 1.1}}
    assert comp_mod.clip_source_kind(clip) == "media"


def test_timeline_xfade_overlap_math():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "fade", "duration": 0.5}},
        {"src": "a.mp4", "in": 5, "out": 8,
         "transition_in": {"type": "circleopen", "duration": 1.0}},
    ])
    tl = comp_mod.compute_timeline(comp, MEDIA)
    starts = [e["start"] for e in tl["base"]]
    assert starts == [0.0, 4.5, 7.5]
    assert tl["duration"] == 10.5  # 5 + 4 + 3 − 0.5 − 1


def test_timeline_speed_changes_duration():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 6, "speed": 2.0}])
    tl = comp_mod.compute_timeline(comp, MEDIA)
    assert tl["duration"] == 3.0


def test_timeline_missing_out_uses_media_duration():
    comp = _comp([{"src": "b.mp4"}])
    tl = comp_mod.compute_timeline(comp, MEDIA)
    assert tl["duration"] == 8.0


def test_validate_clean_composition():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "dissolve", "duration": 0.4}},
    ])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert issues == []


def test_validate_rejects_unknown_transition():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "starwipe", "duration": 0.4}},
    ])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("starwipe" in i["message"] for i in issues)


def test_validate_rejects_transition_longer_than_neighbor():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 1.0},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "fade", "duration": 1.5}},
    ])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("shorter than both neighbors" in i["message"] for i in issues)


def test_validate_rejects_out_beyond_source():
    comp = _comp([{"src": "b.mp4", "in": 0, "out": 9.5}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("exceeds source duration" in i["message"] for i in issues)


def test_validate_missing_file():
    comp = _comp([{"src": "missing.mp4", "in": 0, "out": 2}])
    issues = comp_mod.validate(comp, exists=lambda p: False)
    assert any("file not found" in i["message"] for i in issues)


def test_validate_odd_dimensions_rejected():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 2}])
    comp["project"]["width"] = 641
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("even" in i["message"] for i in issues)


def test_validate_overlay_needs_start():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay",
                       "clips": [{"image": "logo.png", "duration": 2}]}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("'start'" in i["message"] for i in issues)


def test_validate_audio_track_needs_media_src():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "audio",
                       "clips": [{"fill": "#000", "duration": 2, "start": 0}]}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("media 'src'" in i["message"] for i in issues)


def test_validate_effects_on_base_rejected():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "effects": [{"type": "chromakey"}]}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("overlay clips only" in i["message"] for i in issues)


def test_validate_caption_preset_and_source():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    comp["captions"] = {"source": "t.transcript.json", "preset": "nope"}
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("unknown preset" in i["message"] for i in issues)


def test_media_paths_collects_everything():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5,
          "color": {"lut": "mylut.cube"}},
         {"image": "still.png", "duration": 2}],
        tracks_extra=[{"kind": "audio",
                       "clips": [{"src": "music.wav", "start": 0}]}])
    comp["captions"] = {"source": "words.transcript.json"}
    comp["project"]["color"] = {"lut": "teal-orange"}  # built-in — excluded
    paths = comp_mod.media_paths(comp)
    assert paths == ["a.mp4", "mylut.cube", "still.png", "music.wav",
                     "words.transcript.json"]


def test_apply_operations_add_update_remove():
    comp = comp_mod.new_composition()
    comp, results = comp_mod.apply_operations(comp, [
        {"type": "add_clip", "clip": {"src": "a.mp4", "in": 0, "out": 5}},
        {"type": "add_clip", "clip": {"src": "b.mp4", "in": 0, "out": 4}},
        {"type": "set_transition", "index": 1, "transition": "wipeleft",
         "duration": 0.3},
        {"type": "update_clip", "track": "video", "index": 0,
         "patch": {"color": {"saturation": 1.1}}},
        {"type": "add_clip", "track": "audio",
         "clip": {"src": "music.wav", "start": 0, "duck": True}},
    ])
    assert all(r.startswith("ok:") for r in results), results
    base = comp_mod.base_track(comp)["clips"]
    assert base[1]["transition_in"] == {"type": "wipeleft", "duration": 0.3}
    assert base[0]["color"]["saturation"] == 1.1
    assert comp["tracks"][-1]["kind"] == "audio"


def test_apply_operations_patch_null_deletes():
    comp = comp_mod.new_composition()
    comp, _ = comp_mod.apply_operations(comp, [
        {"type": "add_clip", "clip": {"src": "a.mp4", "in": 0, "out": 5,
                                      "volume_db": -6}},
        {"type": "update_clip", "index": 0, "patch": {"volume_db": None}},
    ])
    assert "volume_db" not in comp_mod.base_track(comp)["clips"][0]


def test_apply_operations_continue_on_error():
    comp = comp_mod.new_composition()
    comp, results = comp_mod.apply_operations(comp, [
        {"type": "remove_clip", "index": 3},
        {"type": "add_clip", "clip": {"src": "a.mp4", "in": 0, "out": 2}},
    ])
    assert results[0].startswith("error:")
    assert results[1].startswith("ok:")
    assert len(comp_mod.base_track(comp)["clips"]) == 1


def test_apply_operations_cannot_remove_base_track():
    comp = comp_mod.new_composition()
    comp, results = comp_mod.apply_operations(
        comp, [{"type": "remove_track", "track": 0}])
    assert "cannot be removed" in results[0]
