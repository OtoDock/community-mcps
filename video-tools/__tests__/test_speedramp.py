"""speed_ramp: segment math, expansion rules, validation, and the compiled
graph shape. Execution is covered in test_render_smoke.py."""

import pytest

import composition as comp_mod
import speedramp
from compiler import compile_render

MEDIA = {
    "a.mp4": {"duration": 10.0, "has_video": True, "has_audio": True,
              "fps": 30.0},
    "b.mp4": {"duration": 8.0, "has_video": True, "has_audio": False,
              "fps": 30.0},
}


def _comp(clips):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = clips
    return comp


# ---------------------------------------------------------------------------
# Segment math
# ---------------------------------------------------------------------------


def test_segment_speeds_linear_sample_midpoints():
    speeds = speedramp.segment_speeds({"from": 1.0, "to": 0.4})
    # Midpoints 1/12, 3/12 … 11/12 of a 0.6 drop.
    assert speeds == [0.95, 0.85, 0.75, 0.65, 0.55, 0.45]
    # Monotone, never reaching the endpoints exactly.
    assert speeds[0] < 1.0 and speeds[-1] > 0.4


def test_segment_speeds_curves_bend_the_ramp():
    ease_in = speedramp.segment_speeds(
        {"from": 1.0, "to": 0.4, "curve": "ease_in"})
    ease_out = speedramp.segment_speeds(
        {"from": 1.0, "to": 0.4, "curve": "ease_out"})
    # ease_in changes slowly first (stays near 'from'), ease_out drops early.
    assert ease_in[0] > 0.95 and ease_out[0] < 0.95
    assert ease_in[-1] > ease_out[-1] - 1e-9
    assert all(a >= b for a, b in zip(ease_in, ease_in[1:]))
    assert all(a >= b for a, b in zip(ease_out, ease_out[1:]))


def test_output_duration_and_edges():
    ramp = {"from": 1.0, "to": 0.4}
    dur = speedramp.output_duration(6.0, ramp)
    # Each of the six 1 s source slices divided by its speed.
    expected = sum(1.0 / s for s in speedramp.segment_speeds(ramp))
    assert dur == pytest.approx(expected)
    assert dur > 6.0  # a slowdown lengthens the clip
    first, last = speedramp.edge_durations(6.0, ramp)
    assert first == pytest.approx(1.0 / 0.95)
    assert last == pytest.approx(1.0 / 0.45)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def test_expand_clip_field_routing():
    clip = {"src": "a.mp4", "in": 1.0, "out": 7.0,
            "speed_ramp": {"from": 1.0, "to": 0.4, "curve": "linear"},
            "transition_in": {"type": "whip_pan", "duration": 0.3},
            "fade_in": 0.2, "fade_out": 0.4, "mute": True,
            "stabilize": True, "interpolate": "flow", "motion_blur": 0.4}
    segs = speedramp.expand_clip(clip, 1.0, 7.0)
    assert len(segs) == 6
    assert [s["speed"] for s in segs] == [0.95, 0.85, 0.75, 0.65, 0.55, 0.45]
    assert segs[0]["in"] == 1.0 and segs[-1]["out"] == 7.0
    # Contiguous source coverage.
    for a, b in zip(segs, segs[1:]):
        assert a["out"] == b["in"]
    # Edge-only fields.
    assert "transition_in" in segs[0] and "fade_in" in segs[0]
    assert all("transition_in" not in s and "fade_in" not in s
               for s in segs[1:])
    assert "fade_out" in segs[-1]
    assert all("fade_out" not in s for s in segs[:-1])
    # Copied-everywhere fields; the ramp itself is consumed.
    for s in segs:
        assert s["mute"] and s["stabilize"] and s["interpolate"] == "flow"
        assert s["motion_blur"] == 0.4
        assert "speed_ramp" not in s
        assert s["_ramp"]["of"] == 6


def test_expand_composition_noop_and_idempotent():
    plain = _comp([{"src": "a.mp4", "in": 0, "out": 5}])
    assert speedramp.expand_composition(plain, MEDIA) is plain

    ramped = _comp([
        {"src": "a.mp4", "in": 0, "out": 3},
        {"src": "b.mp4", "in": 0, "out": 6,
         "speed_ramp": {"from": 1.0, "to": 0.4}},
    ])
    once = speedramp.expand_composition(ramped, MEDIA)
    assert ramped["tracks"][0]["clips"][1].get("speed_ramp")  # untouched
    clips = once["tracks"][0]["clips"]
    assert len(clips) == 7 and clips[0]["src"] == "a.mp4"
    assert speedramp.expand_composition(once, MEDIA) is once  # idempotent


def test_expand_resolves_missing_out_from_media_info():
    comp = _comp([{"src": "a.mp4",
                   "speed_ramp": {"from": 2.0, "to": 1.0}}])
    clips = speedramp.expand_composition(comp, MEDIA)["tracks"][0]["clips"]
    assert clips[-1]["out"] == 10.0


# ---------------------------------------------------------------------------
# Timeline + validation
# ---------------------------------------------------------------------------


def test_clip_duration_uses_ramp_math():
    clip = {"src": "a.mp4", "in": 0, "out": 6.0,
            "speed_ramp": {"from": 1.0, "to": 0.4}}
    assert comp_mod.clip_duration(clip, MEDIA) == pytest.approx(
        speedramp.output_duration(6.0, {"from": 1.0, "to": 0.4}))
    tl = comp_mod.compute_timeline(_comp([clip]), MEDIA)
    assert tl["duration"] == pytest.approx(
        speedramp.output_duration(6.0, {"from": 1.0, "to": 0.4}), abs=1e-4)


def _errors(comp):
    return [i for i in comp_mod.validate(comp, media_info=MEDIA)
            if i["level"] == "error"]


def test_validation_accepts_a_good_ramp_and_warns_below_half():
    def issues_for(**extra):
        return comp_mod.validate(_comp([
            {"src": "a.mp4", "in": 0, "out": 6,
             "speed_ramp": {"from": 1.0, "to": 0.25, "curve": "ease_out"},
             "interpolate": "flow", **extra},
        ]), media_info=MEDIA)

    loud = issues_for()
    assert not [i for i in loud if i["level"] == "error"]
    assert any("mute the clip" in i["message"] for i in loud)
    # No bogus "interpolate ignored" warning: the ramp reaches 0.25x.
    assert not any("ignored" in i["message"] for i in loud)
    # A muted clip already followed the advice — don't nag.
    assert not any("mute the clip" in i["message"]
                   for i in issues_for(mute=True))


def test_validation_rejects_bad_ramps():
    cases = [
        ({"from": 5.0, "to": 1.0}, "must be 0.1–4.0"),
        ({"from": 1.0, "to": 0.5, "curve": "bounce"}, "curve must be"),
        ({"to": 0.5}, "speed_ramp must be"),
    ]
    for ramp, needle in cases:
        errs = _errors(_comp([{"src": "a.mp4", "in": 0, "out": 6,
                               "speed_ramp": ramp}]))
        assert any(needle in e["message"] for e in errs), (ramp, errs)

    both = _errors(_comp([{"src": "a.mp4", "in": 0, "out": 6, "speed": 0.5,
                           "speed_ramp": {"from": 1.0, "to": 0.5}}]))
    assert any("replaces 'speed'" in e["message"] for e in both)

    kfs = _errors(_comp([
        {"src": "a.mp4", "in": 0, "out": 6,
         "speed_ramp": {"from": 1.0, "to": 0.5},
         "transform": {"keyframes": [{"t": 0, "scale": 1.0},
                                     {"t": 5, "scale": 1.2}]}}]))
    assert any("transform.keyframes" in e["message"] for e in kfs)

    match = _errors(_comp([
        {"src": "a.mp4", "in": 0, "out": 6,
         "speed_ramp": {"from": 1.0, "to": 0.5},
         "color": {"match": {"ref": "b.mp4@1.0"}}}]))
    assert any("color.match" in e["message"] for e in match)

    still = _errors(_comp([{"fill": "#000000", "duration": 3,
                            "speed_ramp": {"from": 1.0, "to": 0.5}}]))
    assert any("media 'src'" in e["message"] for e in still)


def test_validation_judder_warning_uses_ramp_floor():
    issues = comp_mod.validate(_comp([
        {"src": "a.mp4", "in": 0, "out": 6, "mute": True,
         "speed_ramp": {"from": 1.0, "to": 0.25}},
    ]), media_info=MEDIA)
    assert any("frames will" in i["message"] and "duplicate" in i["message"]
               for i in issues)


def test_transition_room_uses_edge_segment():
    # 6 s source ramp starting near 1x: first segment ≈ 1.05 s of output.
    # A 2 s xfade fits the WHOLE ramp but not the edge segment → error.
    errs = _errors(_comp([
        {"src": "a.mp4", "in": 0, "out": 6},
        {"src": "b.mp4", "in": 0, "out": 6,
         "speed_ramp": {"from": 1.0, "to": 0.4},
         "transition_in": {"type": "fade", "duration": 2.0}},
    ]))
    assert any("edge segment" in e["message"] for e in errs)
    ok = _errors(_comp([
        {"src": "a.mp4", "in": 0, "out": 6},
        {"src": "b.mp4", "in": 0, "out": 6,
         "speed_ramp": {"from": 1.0, "to": 0.4},
         "transition_in": {"type": "fade", "duration": 0.4}},
    ]))
    assert not ok


# ---------------------------------------------------------------------------
# Compiled graph
# ---------------------------------------------------------------------------


def test_compile_expands_ramp_into_segments():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 3},
        {"src": "b.mp4", "in": 0, "out": 6, "mute": True,
         "speed_ramp": {"from": 1.0, "to": 0.4}},
    ]), MEDIA)
    g = plan.graph
    for s in (0.95, 0.85, 0.75, 0.65, 0.55, 0.45):
        assert f"setpts=(PTS-STARTPTS)/{s:g}" in g
    # 7 chains → 6 pairwise concat folds, source span sliced contiguously.
    assert g.count("concat=n=2:v=1:a=0") == 6
    assert "trim=start=1:end=2" in g and "trim=start=5:end=6" in g
    assert plan.duration == pytest.approx(
        3.0 + speedramp.output_duration(6.0, {"from": 1.0, "to": 0.4}),
        abs=1e-3)


def test_compile_preset_tail_lands_on_last_segment():
    # A wow preset INTO the clip after the ramp must style the tail of the
    # ramp's LAST segment (slowest one), not the whole ramp.
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 6, "mute": True,
         "speed_ramp": {"from": 1.0, "to": 0.4}},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "flash_cut", "duration": 0.2}},
    ]), MEDIA)
    g = plan.graph
    # flash_cut tail = eq dip enabled at clip_dur - max(1.5/fps, 0.03);
    # clip_dur here is the LAST SEGMENT's duration (1 s source at 0.45x).
    last_seg_dur = round(1.0 / 0.45, 6)
    assert f"gte(t,{last_seg_dur - 0.05:.6g}" in g
