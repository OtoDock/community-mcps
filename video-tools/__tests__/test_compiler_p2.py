"""Phase-2 compiler features: keyframes (zoompan / overlay motion),
keying effects, rotation, image masks."""

import composition as comp_mod
from compiler import compile_render, piecewise

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


def test_piecewise_expression_shape():
    expr = piecewise([(0.0, 1.0), (2.0, 1.2)], "T")
    assert expr == "if(lt(T,0),1,if(lt(T,2),1+(1.2-1)*(T-0)/(2),1.2))"
    assert piecewise([(1.0, 5.0)], "T") == "5"


def test_base_keyframes_use_zoompan():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 5,
         "transform": {"keyframes": [
             {"t": 0, "scale": 1.0, "pos": [0, 0]},
             {"t": 5, "scale": 1.2, "pos": [40, -20]},
         ]}},
    ]), MEDIA)
    g = plan.graph
    assert "zoompan=z='" in g
    assert "(on/30)" in g                      # time variable
    assert ":d=1:s=640x360:fps=30" in g
    assert "(iw-iw/zoom)/2-(" in g             # pan offset in x
    # No static zoom path when keyframed.
    assert "crop=640:360:(iw-640)/2" not in g


def test_overlay_pos_keyframes_animate_overlay_xy():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 3, "start": 1,
             "transform": {"keyframes": [
                 {"t": 0, "pos": [-200, 0]},
                 {"t": 3, "pos": [200, 0]},
             ]}},
        ]}]), MEDIA)
    g = plan.graph
    assert "overlay=x='(W-w)/2+(if(lt((t-1)," in g
    assert "y='(H-h)/2+(" in g


def test_overlay_effects_and_rotate():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 1,
             "effects": [{"type": "chromakey", "color": "#00FF00",
                          "similarity": 0.12, "blend": 0.08},
                         {"type": "despill", "channel": "green"}],
             "transform": {"rotate": -8}},
        ]}]), MEDIA)
    g = plan.graph
    assert "chromakey=color=0x00FF00:similarity=0.12:blend=0.08" in g
    assert "despill=type=green" in g
    assert "rotate=a=-8*PI/180:c=black@0:ow='hypot(iw,ih)':oh=ow" in g


def test_overlay_mask_uses_scale2ref_alphamerge():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 0,
             "mask": {"image": "mask.png"}},
        ]}]), MEDIA)
    g = plan.graph
    assert "scale2ref" in g
    assert "alphamerge" in g
    assert "format=gray" in g
    # Mask image becomes its own looped still input.
    assert any(p == "mask.png" for _, p in plan.inputs)


def test_keyframed_zoompan_scales_with_preview_canvas():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 5,
         "transform": {"keyframes": [
             {"t": 0, "pos": [100, 0]}, {"t": 5, "pos": [0, 0]}]}},
    ]), MEDIA, canvas_scale=0.5)
    # pos values halve with the canvas.
    assert "s=320x180" in plan.graph
    assert "50" in plan.graph


def test_stab_transform_sits_between_setpts_and_fps():
    """The renderer injects `_stab` after its detect pre-pass; the compiler
    must place vidstabtransform on SOURCE frames — after the trim/setpts,
    before the fps resample (the .trf indexes frames by order)."""
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0.5, "out": 5.5,
         "_stab": {"trf": "/tmp/r/stab0.trf", "smoothing": 25, "zoom": 0.0}},
    ]), MEDIA)
    g = plan.graph
    assert ("setpts=PTS-STARTPTS,"
            "vidstabtransform=input=/tmp/r/stab0.trf:smoothing=25"
            ":optzoom=1:interpol=bicubic,"
            "unsharp=5:5:0.8:3:3:0.4,"
            "fps=30") in g


def test_stab_on_overlay_chain_and_absent_without_injection():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "b.mp4", "in": 0, "out": 2, "start": 1,
             "_stab": {"trf": "/tmp/r/stab1.trf", "smoothing": 15, "zoom": 0.0}},
        ]}]), MEDIA)
    g = plan.graph
    assert "vidstabtransform=input=/tmp/r/stab1.trf" in g
    assert g.count("vidstabtransform") == 1   # base clip untouched

    plain = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6, "stabilize": True}]), MEDIA)
    # `stabilize` alone does nothing in the pure compiler — only the
    # renderer-injected `_stab` (with a staged trf) activates the filter.
    assert "vidstabtransform" not in plain.graph


def test_blend_interp_inserted_only_when_native_frames_run_out():
    media = {
        "slow.mp4": {"duration": 10.0, "has_video": True, "has_audio": True,
                     "fps": 30.0},
        "action60.mp4": {"duration": 10.0, "has_video": True,
                         "has_audio": False, "fps": 60.0},
    }
    plan = compile_render(_comp([
        {"src": "slow.mp4", "in": 0, "out": 2, "speed": 0.25,
         "interpolate": "blend"},
    ]), media)
    assert ("setpts=(PTS-STARTPTS)/0.25,"
            "minterpolate=fps=30:mi_mode=blend,fps=30") in plan.graph

    # 60 fps at 0.5× covers a 30 fps timeline natively — no synthesis.
    native = compile_render(_comp([
        {"src": "action60.mp4", "in": 0, "out": 2, "speed": 0.5,
         "interpolate": "blend"},
    ]), media)
    assert "minterpolate" not in native.graph


def test_slomo_mezzanine_replaces_trim_speed_and_stab():
    media = dict(MEDIA)
    media["/tmp/cache/mezz-abc.mp4"] = {
        "duration": 8.0, "has_video": True, "has_audio": False, "fps": 30.0}
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 1, "out": 3, "speed": 0.25,
         "interpolate": "flow",
         "_slomo": {"src": "/tmp/cache/mezz-abc.mp4"}},
    ]), media)
    g = plan.graph
    assert any(p == "/tmp/cache/mezz-abc.mp4" for _, p in plan.inputs)
    # Trim/speed/interpolation are baked into the mezzanine (video chain
    # trims nothing — note "]trim", not the audio chain's atrim).
    assert "/0.25" not in g
    assert "minterpolate" not in g
    assert "]trim=start=1" not in g
    # The original source still feeds the AUDIO chain (mezzanine is -an).
    assert any(p == "a.mp4" for _, p in plan.inputs)
    assert "atrim=start=1:end=3" in g
    assert "atempo=0.5,atempo=0.5" in g

    # Flow WITHOUT the renderer pre-pass (no _slomo) must not silently
    # break the pure compiler — it just falls back to duplicate.
    fallback = compile_render(_comp([
        {"src": "a.mp4", "in": 1, "out": 3, "speed": 0.25,
         "interpolate": "flow"},
    ]), MEDIA)
    assert "minterpolate" not in fallback.graph
    assert "setpts=(PTS-STARTPTS)/0.25" in fallback.graph


def test_clip_audio_chain_lands_before_resample_and_fades():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6,
          "audio": {"denoise": True, "compress": True}}],
        tracks_extra=[{"kind": "audio", "clips": [
            {"src": "b.mp4", "start": 0, "gain_db": -6, "fade_out": 1,
             "audio": {"eq": {"preset": "music"}}},
        ]}]), {
        "a.mp4": {"duration": 10.0, "has_video": True, "has_audio": True},
        "b.mp4": {"duration": 8.0, "has_video": False, "has_audio": True},
    })
    g = plan.graph
    base_chain = next(c for c in g.split(";\n") if "afftdn" in c)
    assert base_chain.index("afftdn") < base_chain.index("acompressor")
    assert base_chain.index("acompressor") < base_chain.index("aresample")
    track_chain = next(c for c in g.split(";\n") if "bass=" in c)
    # Sweetening processes the raw signal; fades shape the processed one.
    assert track_chain.index("volume=-6dB") < track_chain.index("bass=")
    assert track_chain.index("treble=") < track_chain.index("afade=t=out")


def test_master_chain_precedes_loudnorm_token():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 4}])
    comp["audio_master"] = {"gain_db": -2, "compress": True, "limiter": True,
                            "loudnorm": True}
    plan = compile_render(comp, MEDIA, mode="final")
    g = plan.graph
    tail = next(c for c in g.split(";\n") if "__LOUDNORM__" in c)
    assert tail.index("volume=-2dB") < tail.index("acompressor")
    assert tail.index("acompressor") < tail.index("alimiter")
    assert tail.index("alimiter") < tail.index("__LOUDNORM__")


def test_match_single_lut_precedes_creative_grade():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4,
         "color": {"match": {"ref": "b.mp4@1.0"}, "saturation": 1.1},
         "_match": {"cube": "/tmp/r/match0.cube"}},
    ]), MEDIA)
    chain = next(c for c in plan.graph.split(";\n") if "lut3d" in c)
    assert "lut3d=file='/tmp/r/match0.cube'" in chain
    # Normalize to the neighbor FIRST, style on top.
    assert chain.index("lut3d") < chain.index("saturation=1.1")


def test_match_ramp_splits_and_blends_two_grades():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 6},
        {"src": "b.mp4", "in": 0, "out": 6,
         "color": {"match": {"ramp_from": "a.mp4@5.9", "ramp_to": "a.mp4@0.1"}},
         "_match": {"a": "/tmp/r/m0a.cube", "b": "/tmp/r/m0b.cube",
                    "duration": 6.0}},
    ]), MEDIA)
    g = plan.graph
    assert "split=2" in g
    assert "lut3d=file='/tmp/r/m0a.cube'" in g
    assert "lut3d=file='/tmp/r/m0b.cube'" in g
    assert "blend=all_expr='A+(B-A)*min(T/6,1)'" in g
    # No xfade: the joins stay hard cuts (concat fold).
    assert "xfade" not in g
    # Without renderer injection the pure compiler ignores match cleanly.
    plain = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4,
         "color": {"match": {"ref": "b.mp4@1.0"}}},
    ]), MEDIA)
    assert "lut3d" not in plain.graph and "split" not in plain.graph


def test_finish_filters_on_clip_and_project():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 4, "grain": 0.4,
         "vignette": True, "sharpen": {"amount": 0.8}},
    ])
    comp["project"]["grain"] = {"strength": 0.2}
    comp["project"]["letterbox"] = "2.39"
    plan = compile_render(comp, MEDIA)
    g = plan.graph
    clip_chain = next(c for c in g.split(";\n") if "unsharp=5:5:1.1" in c)
    # Order: sharpen → grain → vignette, after the grade position.
    assert clip_chain.index("unsharp") < clip_chain.index("noise=alls=8")
    assert clip_chain.index("noise") < clip_chain.index("vignette=angle=0.525")
    # Project tail: its own grain + letterbox bars; captions would follow.
    tail = next(c for c in g.split(";\n") if "drawbox" in c)
    assert "noise=alls=4" in tail
    # 640x360 @ 2.39 → bar height (360 − 640/2.39)/2 ≈ 46.
    assert "drawbox=x=0:y=0:w=iw:h=46:color=black:t=fill" in tail
    assert "drawbox=x=0:y=ih-46:w=iw:h=46:color=black:t=fill" in tail


def test_letterbox_noop_when_canvas_already_wide():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 4}])
    comp["project"]["letterbox"] = 1.7  # 640x360 is already 1.78
    plan = compile_render(comp, MEDIA)
    assert "drawbox" not in plan.graph


def test_preset_transition_keeps_hard_cut_and_styles_both_edges():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "whip_pan", "duration": 0.4}},
    ]), MEDIA)
    g = plan.graph
    # The cut stays a cut: concat fold, no xfade, full duration.
    assert "xfade" not in g
    assert "concat=n=2:v=1:a=0" in g
    assert plan.duration == 7.0
    chains = g.split(";\n")
    out_chain = next(c for c in chains if "trim=start=0:end=4" in c and c.startswith("[0:v]"))
    in_chain = next(c for c in chains if "trim=start=0:end=3" in c)
    # Outgoing tail: blur builds into the cut (t→4); incoming head decays.
    assert "dblur" in out_chain and "between(t,3.8," in out_chain
    assert "dblur" in in_chain and "between(t,0,0.0666667)" in in_chain


def test_whip_left_overlaps_with_wrap_expr_in_gbrp_and_blur_both_sides():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "whip_left", "duration": 0.4}},
    ]), MEDIA)
    g = plan.graph
    # Adjacent-strip custom expr, wrapped in gbrp and pinned back.
    # (whip_left = camera pans left → content slides right, X-W term.)
    assert "xfade=transition=custom:expr='" in g
    assert "clip((X-W*((1-P)*(1-P)*(3-2*(1-P)))),0,W-1)" in g
    assert g.count("format=gbrp") == 2
    assert "format=yuv420p,settb=AVTB" in g
    assert plan.duration == 6.6                     # overlap shortens
    assert g.count("dblur") == 6                    # 3-step ramp each side


def test_zoom_out_preset_builds_mirror_tile_on_both_clips():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "zoom_out", "duration": 0.4}},
    ]), MEDIA)
    g = plan.graph
    assert plan.duration == 7.0                     # cut preset: no overlap
    assert "xfade" not in g and "concat=n=2:v=1:a=0" in g
    assert g.count("fillborders") == 2              # tile on tail AND head
    assert g.count("pad=1280:720:320:180") == 2     # 2× the 640x360 canvas


def test_luma_wipe_builds_maskedmerge_join_no_edge_fx():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 3,
         "transition_in": {"type": "luma_wipe", "duration": 0.8}},
    ]), MEDIA)
    g = plan.graph
    assert "maskedmerge" in g and "xfade" not in g
    # Accumulator split at the overlap (offset 3.2, end 4.0); the mask
    # taps luma via extractplanes (format=gray would back-propagate its
    # constraint and grayscale the timeline); every segment ends pinned.
    assert "trim=end=3.2" in g
    assert "trim=start=3.2:end=4" in g
    assert "extractplanes=y,geq='clip((p(X,Y)-255*(1-clip(T/0.8,0,1)))" in g
    assert g.count("format=gbrp") == 3
    assert g.count("settb=AVTB,format=yuv420p") == 4   # pinned segments
    assert "concat=n=3:v=1:a=0,fps=30" in g
    assert "dblur" not in g and "rgbashift" not in g
    assert plan.duration == 6.2                     # 4 + 3 − 0.8 overlap


def test_motion_blur_tmix_after_fps():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 4, "motion_blur": 0.75},
    ]), MEDIA)
    assert "fps=30,tmix=frames=5" in plan.graph
    none = compile_render(_comp([{"src": "a.mp4", "in": 0, "out": 4}]), MEDIA)
    assert "tmix" not in none.graph


def test_preset_on_image_and_fill_clips_compiles():
    plan = compile_render(_comp([
        {"fill": "#101010", "duration": 2},
        {"image": "logo.png", "duration": 2,
         "transition_in": {"type": "flash_cut", "duration": 0.2}},
    ]), MEDIA)
    g = plan.graph
    assert "eq=brightness=-0.9" in g      # head dip-to-black on the image clip
    assert "eq=brightness=-0.35" in g     # pre-cut dip on the fill clip
    assert plan.duration == 4.0


def test_alpha_webm_forces_libvpx_decoder():
    media = dict(MEDIA)
    media["sting.webm"] = {"duration": 3.0, "has_video": True,
                           "has_audio": False, "alpha_codec": "libvpx-vp9"}
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"src": "sting.webm", "start": 0.5}]}]), media)
    assert (["-c:v", "libvpx-vp9"], "sting.webm") in plan.inputs
    assert (["-c:v", "libvpx-vp9"], "a.mp4") not in plan.inputs


# ---------------------------------------------------------------------------
# Low-RAM windowed rendering: pruning + segment planning
# ---------------------------------------------------------------------------

from compiler import (bare_cut_points, estimate_window_bytes,  # noqa: E402
                      plan_segments, window_pruned)


def test_window_pruned_replaces_far_clips_and_keeps_timeline():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 4},
        {"src": "a.mp4", "in": 4, "out": 8,
         "transition_in": {"type": "fade", "duration": 0.5}},
    ], tracks_extra=[{"kind": "overlay", "clips": [
        {"image": "logo.png", "duration": 2, "start": 1},
        {"image": "logo2.png", "duration": 2, "start": 9},
    ]}])
    pruned = window_pruned(comp, MEDIA, 0.0, 3.0)

    clips = comp_mod.base_track(pruned)["clips"]
    assert clips[0]["src"] == "a.mp4"                     # inside the window
    assert clips[1] == {"fill": "#000000", "duration": 4.0}
    assert clips[2]["fill"] and clips[2]["duration"] == 4.0
    assert clips[2]["transition_in"]["type"] == "fade"    # fold math preserved

    # The pruned timeline is bit-identical to the original.
    assert (comp_mod.compute_timeline(pruned, MEDIA)
            == comp_mod.compute_timeline(comp, MEDIA))

    # Overlays outside the window are dropped entirely.
    ovl = [t for t in pruned["tracks"] if t.get("kind") == "overlay"][0]
    assert [c["image"] for c in ovl["clips"]] == ["logo.png"]

    # The point of it all: far media is never opened.
    plan = compile_render(pruned, MEDIA, streams="v")
    paths = [p for _, p in plan.inputs]
    assert "b.mp4" not in paths
    assert paths == ["a.mp4", "logo.png"]


def test_window_pruned_pad_keeps_edge_clips():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 4},
        {"src": "b.mp4", "in": 0, "out": 4},
    ])
    # Window ends exactly at the cut: without pad the second clip is
    # replaced, with pad it survives (transitions/styling reach past edges).
    assert comp_mod.base_track(
        window_pruned(comp, MEDIA, 0.0, 4.0))["clips"][1].get("fill")
    assert window_pruned(
        comp, MEDIA, 0.0, 4.0, pad=1.0)["tracks"][0]["clips"][1]["src"] == "b.mp4"


def test_bare_cut_points_skip_transitions_and_presets():
    comp = _comp([
        {"src": "a.mp4", "in": 0, "out": 2},
        {"src": "a.mp4", "in": 2, "out": 4},
        {"src": "a.mp4", "in": 4, "out": 6,
         "transition_in": {"type": "fade", "duration": 0.5}},
        {"src": "a.mp4", "in": 6, "out": 8,
         "transition_in": {"type": "glitch", "duration": 0.3}},
        {"src": "a.mp4", "in": 0, "out": 2,
         "transition_in": {"type": "cut"}},
    ])
    # Only the true hard cuts: the xfade overlaps its boundary and the
    # glitch preset styles the outgoing tail across its cut.
    assert bare_cut_points(comp, MEDIA) == [2.0, 7.5]


def test_plan_segments_split_at_bare_cuts_within_budget():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 2} for _ in range(6)])
    full = estimate_window_bytes(comp, MEDIA, 0.0, 12.0)
    assert full == 12 * 30 * 640 * 360 * 1.5

    assert plan_segments(comp, MEDIA, full + 1) == []      # fits → single pass

    segs = plan_segments(comp, MEDIA, full / 3)
    assert [round(b - a, 6) for a, b in segs] == [4.0, 4.0, 4.0]
    assert segs[0][0] == 0.0 and segs[-1][1] == 12.0
    assert all(segs[i][1] == segs[i + 1][0] for i in range(len(segs) - 1))

    # A span between adjacent bare cuts that alone busts the budget stays
    # one segment — there is nowhere safe to split it.
    segs = plan_segments(comp, MEDIA, full / 12)
    assert [round(b - a, 6) for a, b in segs] == [2.0] * 6


def test_estimate_counts_overlays_rgba_and_skips_fills():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 4},
         {"fill": "#101010", "duration": 4}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 1}]}])
    media = dict(MEDIA)
    media["logo.png"] = {"duration": 0.0, "still": True, "has_video": True,
                         "has_audio": False, "width": 200, "height": 100}
    est = estimate_window_bytes(comp, media, 0.0, 8.0)
    # 4 s of source frames (project-size fallback) + the overlay in rgba;
    # the fill costs nothing — generated on demand, never buffered.
    assert est == 4 * 30 * 640 * 360 * 1.5 + 2 * 30 * 200 * 100 * 4
