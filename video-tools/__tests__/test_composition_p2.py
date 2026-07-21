"""Phase-2 validation: keyframes, effects, masks, rotation."""

import composition as comp_mod

MEDIA = {
    "a.mp4": {"duration": 10.0, "has_video": True, "has_audio": True},
    "b.mp4": {"duration": 8.0, "has_video": True, "has_audio": False},
}


def _comp(clips, tracks_extra=None):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = clips
    if tracks_extra:
        comp["tracks"].extend(tracks_extra)
    return comp


def test_keyframes_valid_ken_burns():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5, "transform": {
        "keyframes": [{"t": 0, "scale": 1.0}, {"t": 5, "scale": 1.15}]}}])
    assert comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA) == []


def test_keyframes_reject_static_conflict():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5, "transform": {
        "scale": 1.2,
        "keyframes": [{"t": 0, "scale": 1.0}, {"t": 5, "scale": 1.15}]}}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("conflicts with keyframes" in i["message"] for i in issues)


def test_keyframes_require_consistent_props():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5, "transform": {
        "keyframes": [{"t": 0, "scale": 1.0, "pos": [0, 0]},
                      {"t": 5, "scale": 1.2}]}}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("EVERY keyframe" in i["message"] for i in issues)


def test_keyframes_must_increase_in_time():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5, "transform": {
        "keyframes": [{"t": 2, "scale": 1.0}, {"t": 1, "scale": 1.2}]}}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("increasing" in i["message"] for i in issues)


def test_keyframes_base_scale_below_one_rejected():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5, "transform": {
        "keyframes": [{"t": 0, "scale": 0.8}, {"t": 5, "scale": 1.0}]}}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("zoompan" in i["message"] for i in issues)


def test_keyframes_overlay_scale_rejected_pos_ok():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "l.png", "duration": 2, "start": 0, "transform": {
                "keyframes": [{"t": 0, "scale": 1.0}, {"t": 2, "scale": 2.0}]}}]}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("overlay scale keyframes" in i["message"] for i in issues)

    comp2 = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "l.png", "duration": 2, "start": 0, "transform": {
                "keyframes": [{"t": 0, "pos": [0, 0]}, {"t": 2, "pos": [50, 0]}]}}]}])
    assert comp_mod.validate(comp2, exists=lambda p: True, media_info=MEDIA) == []


def test_effects_valid_on_overlay_rejected_on_base():
    over = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 0,
             "effects": [{"type": "chromakey", "color": "#00FF00"}]}]}])
    assert comp_mod.validate(over, exists=lambda p: True, media_info=MEDIA) == []

    base = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "effects": [{"type": "chromakey"}]}])
    issues = comp_mod.validate(base, exists=lambda p: True, media_info=MEDIA)
    assert any("overlay clips only" in i["message"] for i in issues)


def test_effect_unknown_type_rejected():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 0,
             "effects": [{"type": "glow"}]}]}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("chromakey" in i["message"] for i in issues)


def test_mask_validation_and_media_paths():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "l.png", "duration": 2, "start": 0,
             "mask": {"image": "m.png"}}]}])
    assert comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA) == []
    assert "m.png" in comp_mod.media_paths(comp)

    missing = comp_mod.validate(comp, exists=lambda p: p != "m.png",
                                media_info=MEDIA)
    assert any("mask image not found" in i["message"] for i in missing)


def test_stabilize_valid_forms():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 5, "stabilize": True},
        {"src": "b.mp4", "in": 0, "out": 4,
         "stabilize": {"strength": "high", "smoothing": 30, "zoom": 2}},
    ])
    assert comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA) == []


def test_stabilize_rejected_on_stills_audio_and_bad_values():
    still = _comp([{"image": "l.png", "duration": 2, "stabilize": True},
                   {"src": "a.mp4", "in": 0, "out": 5}])
    issues = comp_mod.validate(still, exists=lambda p: True, media_info=MEDIA)
    assert any("media 'src' clip" in i["message"] for i in issues)

    audio = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "audio", "clips": [
            {"src": "a.mp4", "start": 0, "stabilize": True}]}])
    issues = comp_mod.validate(audio, exists=lambda p: True, media_info=MEDIA)
    assert any("video/overlay clips only" in i["message"] for i in issues)

    bad = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                  "stabilize": {"strength": "maximum"}}])
    issues = comp_mod.validate(bad, exists=lambda p: True, media_info=MEDIA)
    assert any("strength must be one of" in i["message"] for i in issues)

    bad2 = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "stabilize": {"smoothing": 500}}])
    issues = comp_mod.validate(bad2, exists=lambda p: True, media_info=MEDIA)
    assert any("smoothing must be" in i["message"] for i in issues)


def test_speed_floor_and_interpolate_field():
    ok = _comp([{"src": "a.mp4", "in": 0, "out": 5, "speed": 0.1,
                 "interpolate": "flow", "mute": True}])
    issues = comp_mod.validate(ok, exists=lambda p: True, media_info=MEDIA)
    assert [i for i in issues if i["level"] == "error"] == []

    too_slow = _comp([{"src": "a.mp4", "in": 0, "out": 5, "speed": 0.05}])
    issues = comp_mod.validate(too_slow, exists=lambda p: True, media_info=MEDIA)
    assert any("0.1–4.0" in i["message"] for i in issues)

    bad_mode = _comp([{"src": "a.mp4", "in": 0, "out": 5, "speed": 0.5,
                       "interpolate": "mci"}])
    issues = comp_mod.validate(bad_mode, exists=lambda p: True, media_info=MEDIA)
    assert any("interpolate must be one of" in i["message"] for i in issues)

    normal_speed = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                           "interpolate": "flow"}])
    issues = comp_mod.validate(normal_speed, exists=lambda p: True,
                               media_info=MEDIA)
    assert any("only affects slow motion" in i["message"] for i in issues)


def test_slowmo_judder_warning_is_fps_aware():
    media = {
        "slow.mp4": {"duration": 10.0, "has_video": True, "has_audio": True,
                     "fps": 30.0},
        "action60.mp4": {"duration": 10.0, "has_video": True,
                         "has_audio": True, "fps": 60.0},
    }
    juddery = _comp([{"src": "slow.mp4", "in": 0, "out": 5, "speed": 0.25}])
    issues = comp_mod.validate(juddery, exists=lambda p: True, media_info=media)
    assert any("frames will duplicate" in i["message"] for i in issues)

    # 60 fps at 0.5× on a 30 fps timeline: native frames cover it — clean.
    native = _comp([{"src": "action60.mp4", "in": 0, "out": 5, "speed": 0.5,
                     "mute": True}])
    issues = comp_mod.validate(native, exists=lambda p: True, media_info=media)
    assert not any("frames will duplicate" in i["message"] for i in issues)

    # Choosing flow silences the judder warning too.
    fixed = _comp([{"src": "slow.mp4", "in": 0, "out": 5, "speed": 0.25,
                    "interpolate": "flow"}])
    issues = comp_mod.validate(fixed, exists=lambda p: True, media_info=media)
    assert not any("frames will duplicate" in i["message"] for i in issues)


def test_audio_chain_validation():
    ok = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5,
          "audio": {"denoise": "voice", "eq": {"preset": "voice"},
                    "compress": True, "deess": True}}],
        tracks_extra=[{"kind": "audio", "clips": [
            {"src": "a.mp4", "start": 0,
             "audio": {"eq": {"bands": [{"f": 3000, "gain_db": 2, "q": 1.2}]},
                       "compress": {"threshold_db": -20, "ratio": 4}}}]}])
    ok["audio_master"] = {"loudnorm": False, "limiter": {"ceiling_db": -1},
                          "compress": True}
    assert comp_mod.validate(ok, exists=lambda p: True, media_info=MEDIA) == []

    bad_preset = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                         "audio": {"eq": {"preset": "podcast"}}}])
    issues = comp_mod.validate(bad_preset, exists=lambda p: True, media_info=MEDIA)
    assert any("eq preset must be one of" in i["message"] for i in issues)

    on_overlay = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 0,
             "audio": {"denoise": True}}]}])
    issues = comp_mod.validate(on_overlay, exists=lambda p: True, media_info=MEDIA)
    assert any("overlays are video-only" in i["message"] for i in issues)

    bad_deess = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                        "audio": {"deess": {"intensity": 3}}}])
    issues = comp_mod.validate(bad_deess, exists=lambda p: True, media_info=MEDIA)
    assert any("intensity must be 0–1" in i["message"] for i in issues)

    # b.mp4 has no audio stream → the chain is a silent no-op; say so.
    silent = _comp([{"src": "b.mp4", "in": 0, "out": 5,
                     "audio": {"denoise": True}}])
    issues = comp_mod.validate(silent, exists=lambda p: True, media_info=MEDIA)
    assert any("no audio stream" in i["message"] and i["level"] == "warning"
               for i in issues)

    bad_master = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    bad_master["audio_master"] = {"limiter": {"ceiling_db": -20}}
    issues = comp_mod.validate(bad_master, exists=lambda p: True, media_info=MEDIA)
    assert any("ceiling_db must be" in i["message"] for i in issues)


def test_match_color_validation():
    single = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                     "color": {"match": {"ref": "b.mp4@1.0", "strength": 0.8}}}])
    assert comp_mod.validate(single, exists=lambda p: True, media_info=MEDIA) == []
    assert "b.mp4" in comp_mod.media_paths(single)

    ramp = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "color": {"match": {"ramp_from": "b.mp4@7.9",
                                       "ramp_to": "a.mp4@0.1"}}}])
    assert comp_mod.validate(ramp, exists=lambda p: True, media_info=MEDIA) == []

    both = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "color": {"match": {"ref": "b.mp4@1", "ramp_from": "b.mp4@1"}}}])
    issues = comp_mod.validate(both, exists=lambda p: True, media_info=MEDIA)
    assert any("EITHER ref" in i["message"] for i in issues)

    half_ramp = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                        "color": {"match": {"ramp_from": "b.mp4@1"}}}])
    issues = comp_mod.validate(half_ramp, exists=lambda p: True, media_info=MEDIA)
    assert any("BOTH ramp_from and ramp_to" in i["message"] for i in issues)

    bad_syntax = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                         "color": {"match": {"ref": "b.mp4"}}}])
    issues = comp_mod.validate(bad_syntax, exists=lambda p: True, media_info=MEDIA)
    assert any("path@seconds" in i["message"] for i in issues)

    missing = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                      "color": {"match": {"ref": "gone.mp4@1"}}}])
    issues = comp_mod.validate(missing, exists=lambda p: p != "gone.mp4",
                               media_info=MEDIA)
    assert any("reference file not found" in i["message"] for i in issues)

    on_overlay = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 0,
             "color": {"match": {"ref": "a.mp4@1"}}}]}])
    issues = comp_mod.validate(on_overlay, exists=lambda p: True, media_info=MEDIA)
    assert any("base-track clips only" in i["message"] for i in issues)

    proj = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    proj["project"]["color"] = {"match": {"ref": "a.mp4@1"}}
    issues = comp_mod.validate(proj, exists=lambda p: True, media_info=MEDIA)
    assert any("per-clip" in i["message"] for i in issues)


def test_finish_and_letterbox_validation():
    ok = _comp([{"src": "a.mp4", "in": 0, "out": 5, "grain": 0.3,
                 "vignette": {"strength": 0.6}, "sharpen": True}])
    ok["project"]["letterbox"] = "2.39"
    ok["project"]["vignette"] = 0.4
    issues = comp_mod.validate(ok, exists=lambda p: True, media_info=MEDIA)
    assert [i for i in issues if i["level"] == "error"] == []

    on_overlay = _comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "l.png", "duration": 2, "start": 0, "grain": 0.5}]}])
    issues = comp_mod.validate(on_overlay, exists=lambda p: True, media_info=MEDIA)
    assert any("base-track clips" in i["message"] for i in issues)

    bad = _comp([{"src": "a.mp4", "in": 0, "out": 5, "grain": 3}])
    issues = comp_mod.validate(bad, exists=lambda p: True, media_info=MEDIA)
    assert any("must be true, 0–1" in i["message"] for i in issues)

    lb_noop = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    lb_noop["project"]["letterbox"] = 1.5   # 640x360 already wider
    issues = comp_mod.validate(lb_noop, exists=lambda p: True, media_info=MEDIA)
    assert any("adds no bars" in i["message"] and i["level"] == "warning"
               for i in issues)

    lb_bad = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    lb_bad["project"]["letterbox"] = "wide"
    issues = comp_mod.validate(lb_bad, exists=lambda p: True, media_info=MEDIA)
    assert any("aspect number" in i["message"] for i in issues)


def test_preset_transitions_validate_and_add_no_overlap():
    ok = _comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "zoom_punch", "duration": 0.4}},
    ])
    assert comp_mod.validate(ok, exists=lambda p: True, media_info=MEDIA) == []
    tl = comp_mod.compute_timeline(ok, MEDIA)
    assert tl["duration"] == 7.0                      # no shortening
    assert tl["base"][1]["transition"] is None        # fold = concat

    too_long = _comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "glitch", "duration": 3.0}},
    ])
    issues = comp_mod.validate(too_long, exists=lambda p: True, media_info=MEDIA)
    assert any("0.05–2.5 s" in i["message"] for i in issues)

    # Overlapping premium presets validate like xfades (and shorten the
    # timeline), flash colors validate, motion_blur validates.
    premium = _comp([
        {"src": "a.mp4", "in": 0, "out": 4, "motion_blur": 0.5},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "whip_left", "duration": 0.4}},
        {"src": "a.mp4", "in": 5, "out": 8,
         "transition_in": {"type": "luma_wipe", "duration": 0.8}},
    ])
    assert comp_mod.validate(premium, exists=lambda p: True, media_info=MEDIA) == []
    tl = comp_mod.compute_timeline(premium, MEDIA)
    assert abs(tl["duration"] - 8.8) < 1e-6         # 4+3+3 − 0.4 − 0.8

    bad_flash = _comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "flash_cut", "duration": 0.3,
                           "flash": "red"}},
    ])
    issues = comp_mod.validate(bad_flash, exists=lambda p: True, media_info=MEDIA)
    assert any('"black" (default) or "white"' in i["message"] for i in issues)

    bad_mb = _comp([{"src": "a.mp4", "in": 0, "out": 4, "motion_blur": 5}])
    issues = comp_mod.validate(bad_mb, exists=lambda p: True, media_info=MEDIA)
    assert any("motion_blur must be" in i["message"] for i in issues)

    tight = _comp([
        {"src": "a.mp4", "in": 0, "out": 0.2},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "spin", "duration": 0.9}},
    ])
    issues = comp_mod.validate(tight, exists=lambda p: True, media_info=MEDIA)
    assert any("styling on each side" in i["message"] for i in issues)


def test_rotate_overlay_only():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 5,
                   "transform": {"rotate": 10}}])
    issues = comp_mod.validate(comp, exists=lambda p: True, media_info=MEDIA)
    assert any("overlay clips only" in i["message"] for i in issues)
