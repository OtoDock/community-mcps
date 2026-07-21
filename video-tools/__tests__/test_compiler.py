"""Filtergraph compiler: graph atoms, input tables, stream selection."""

import composition as comp_mod
from compiler import compile_render

MEDIA = {
    "a.mp4": {"duration": 10.0, "has_video": True, "has_audio": True},
    "b.mp4": {"duration": 8.0, "has_video": True, "has_audio": False},
    "music.wav": {"duration": 60.0, "has_video": False, "has_audio": True},
    "vo.wav": {"duration": 3.0, "has_video": False, "has_audio": True},
}


def _comp(clips, tracks_extra=None, captions=None, master=None):
    comp = comp_mod.new_composition({"width": 640, "height": 360, "fps": 30})
    comp["tracks"][0]["clips"] = clips
    if tracks_extra:
        comp["tracks"].extend(tracks_extra)
    if captions is not None:
        comp["captions"] = captions
    if master is not None:
        comp["audio_master"] = master
    return comp


def test_single_clip_graph_basics():
    plan = compile_render(_comp([{"src": "a.mp4", "in": 1, "out": 4}]),
                          MEDIA, mode="final")
    assert plan.inputs == [([], "a.mp4")]
    assert "trim=start=1:end=4" in plan.graph
    assert "force_original_aspect_ratio=increase" in plan.graph  # cover fit
    assert "format=yuv420p" in plan.graph
    assert plan.video_label and plan.audio_label
    assert plan.duration == 3.0
    assert "-crf" in plan.encode_args and "18" in plan.encode_args


def test_xfade_offset_matches_timeline():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "circleopen", "duration": 0.5}},
    ]), MEDIA, mode="preview")
    assert "xfade=transition=circleopen:duration=0.5:offset=4.5" in plan.graph
    assert "acrossfade=d=0.5" in plan.graph
    assert plan.duration == 8.5


def test_cut_uses_concat():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 2},
        {"src": "a.mp4", "in": 5, "out": 7},
    ]), MEDIA)
    assert "concat=n=2:v=1:a=0" in plan.graph
    assert "concat=n=2:v=0:a=1" in plan.graph
    # Same file used twice → ONE shared input.
    assert plan.inputs == [([], "a.mp4")]


def test_crf_override_reaches_encode_args():
    clips = [{"src": "a.mp4", "in": 0, "out": 2}]
    plan = compile_render(_comp(clips), MEDIA, mode="final", crf=28)
    i = plan.encode_args.index("-crf")
    assert plan.encode_args[i + 1] == "28"
    plan = compile_render(_comp(clips), MEDIA, mode="preview", crf=20)
    i = plan.encode_args.index("-crf")
    assert plan.encode_args[i + 1] == "20"
    # default unchanged
    plan = compile_render(_comp(clips), MEDIA, mode="final")
    i = plan.encode_args.index("-crf")
    assert plan.encode_args[i + 1] == "18"


def test_timebase_normalized_for_joins():
    """Every join input must sit on tb=AVTB: per-clip chains end with
    settb/asettb and each concat re-asserts it. A concat-produced accumulator
    on a stray timebase fed into xfade aborts the graph with EINVAL (-22) —
    the 'transition on the last clip of a ≥3-clip track' bug."""
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 2},
        {"src": "a.mp4", "in": 3, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 2,
         "transition_in": {"type": "dissolve", "duration": 0.4}},
    ]), MEDIA)
    assert "settb=AVTB" in plan.graph
    assert "concat=n=2:v=1:a=0,settb=AVTB" in plan.graph
    assert "concat=n=2:v=0:a=1,asettb=AVTB" in plan.graph
    assert "xfade=transition=dissolve" in plan.graph
    # silent source: anullsrc must be format-normalized too, or acrossfade
    # rejects the fmt/timebase mismatch against decoded audio
    anull = [ln for ln in plan.graph.splitlines() if "anullsrc" in ln]
    assert anull and all("asettb=AVTB" in ln for ln in anull)
    assert anull and all("aformat" in ln for ln in anull)


def test_image_chain_keeps_cfr_for_transitions():
    """Image clips must apply setpts BEFORE fps: setpts clears the CFR
    metadata and xfade rejects a non-constant-rate input ("current rate of
    1/0 is invalid") — an image on either side of a transition aborted the
    graph with EINVAL (-22)."""
    plan = compile_render(_comp([
        {"image": "logo.png", "duration": 2.0},
        {"src": "a.mp4", "in": 0, "out": 2,
         "transition_in": {"type": "fade", "duration": 0.5}},
    ]), {**MEDIA, "logo.png": {"duration": 0.0, "has_video": True,
                               "has_audio": False}})
    img_lines = [ln for ln in plan.graph.splitlines() if "[0:v]" in ln]
    assert img_lines and "setpts=PTS-STARTPTS,fps=30" in img_lines[0]
    assert "xfade=transition=fade" in plan.graph


def test_silent_source_gets_anullsrc():
    plan = compile_render(_comp([{"src": "b.mp4", "in": 0, "out": 3}]), MEDIA)
    assert "anullsrc=r=48000:cl=stereo" in plan.graph
    assert "atrim=0:3" in plan.graph


def test_speed_affects_video_audio_and_duration():
    plan = compile_render(_comp([{"src": "a.mp4", "in": 0, "out": 6,
                                  "speed": 1.5}]), MEDIA)
    assert "setpts=(PTS-STARTPTS)/1.5" in plan.graph
    assert "atempo=1.5" in plan.graph
    assert plan.duration == 4.0


def test_image_clip_input_options_and_fill():
    plan = compile_render(_comp([
        {"image": "logo.png", "duration": 2},
        {"fill": "#0A0A12", "duration": 1.5},
    ]), MEDIA)
    assert (["-loop", "1", "-t", "2"], "logo.png") in plan.inputs
    assert "color=c=0x0A0A12:s=640x360:r=30:d=1.5" in plan.graph


def test_overlay_chain_shift_enable_and_alpha():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 1.5,
             "fade_in": 0.3, "fade_out": 0.3,
             "transform": {"scale": 0.5, "pos": [100, -50], "opacity": 0.8}},
        ]}]), MEDIA)
    g = plan.graph
    assert "setpts=PTS+1.5/TB" in g
    assert "enable='between(t,1.5,3.5)'" in g
    assert "colorchannelmixer=aa=0.8" in g
    assert "fade=t=in:st=0:d=0.3:alpha=1" in g
    assert "fade=t=out:st=1.7:d=0.3:alpha=1" in g
    assert "overlay=x=(W-w)/2+(100):y=(H-h)/2+(-50)" in g
    assert "eof_action=pass" in g


def test_overlay_media_scales_with_canvas():
    """canvas_scale must scale overlay MEDIA, not just positions — a preview
    (0.5× canvas) that composites a project-pixel overlay at native size
    renders it 2× too big, so preview stops matching final."""
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 1.0},
        ]}]), MEDIA, canvas_scale=0.5)
    assert "scale=trunc(iw*0.5/2)*2:-2" in plan.graph
    # clip scale composes with the canvas scale
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 1.0,
             "transform": {"scale": 0.5}},
        ]}]), MEDIA, canvas_scale=0.5)
    assert "scale=trunc(iw*0.25/2)*2:-2" in plan.graph


def test_audio_track_delay_gain_and_duck():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "audio", "clips": [
            {"src": "music.wav", "start": 2, "gain_db": -8,
             "fade_in": 1, "duck": True},
        ]}]), MEDIA)
    g = plan.graph
    assert "adelay=2000:all=1" in g
    assert "volume=-8dB" in g
    assert "afade=t=in:st=0:d=1" in g
    assert "asplit=2" in g
    assert "sidechaincompress=threshold=0.05:ratio=6:attack=20:release=400" in g
    assert "amix=inputs=2:duration=first:normalize=0" in g


def test_duck_keys_off_other_audio_clips_not_just_base():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 6}],
        tracks_extra=[{"kind": "audio", "clips": [
            {"src": "music.wav", "start": 0, "gain_db": -8, "duck": True},
            {"src": "vo.wav", "start": 2},
        ]}]), MEDIA)
    g = plan.graph
    # Base bus AND the non-ducked VO clip are each split into a mix leg and
    # a key leg; the key bus (amix of 2) feeds the compressor sidechain, so
    # music dips under the VO even when the base video is silent.
    assert g.count("asplit=2") == 2
    assert "amix=inputs=2:duration=first:normalize=0" in g
    assert "sidechaincompress=threshold=0.05" in g
    # Final mix still carries base + VO + ducked music.
    assert "amix=inputs=3:duration=first:normalize=0" in g


def test_loudnorm_token_only_in_final():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 3}],
                 master={"gain_db": 0, "loudnorm": {"target_lufs": -16}})
    final = compile_render(comp, MEDIA, mode="final")
    preview = compile_render(comp, MEDIA, mode="preview")
    assert "__LOUDNORM__" in final.graph
    assert final.loudnorm == {"i": -16.0, "tp": -1.5, "lra": 11.0}
    assert "__LOUDNORM__" not in preview.graph
    assert preview.loudnorm is None


def test_captions_and_global_grade_on_final_canvas():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 3}])
    comp["project"]["color"] = {"saturation": 1.1}
    plan = compile_render(comp, MEDIA, captions_ass="/tmp/x/captions.ass",
                          luts={})
    assert "ass=filename='/tmp/x/captions.ass'" in plan.graph
    assert "eq=saturation=1.1" in plan.graph


def test_lut_resolution_required():
    comp = _comp([{"src": "a.mp4", "in": 0, "out": 3,
                   "color": {"lut": "teal-orange"}}])
    plan = compile_render(comp, MEDIA, luts={"teal-orange": "/tmp/l/lut0.cube"})
    assert "lut3d=file='/tmp/l/lut0.cube'" in plan.graph


def test_canvas_scale_scales_positions_and_dims():
    comp = _comp(
        [{"src": "a.mp4", "in": 0, "out": 4}],
        tracks_extra=[{"kind": "overlay", "clips": [
            {"image": "logo.png", "duration": 2, "start": 0,
             "transform": {"pos": [200, 100]}}]}])
    plan = compile_render(comp, MEDIA, canvas_scale=0.5)
    assert plan.canvas == (320, 180)
    assert "overlay=x=(W-w)/2+(100):y=(H-h)/2+(50)" in plan.graph


def test_time_range_appends_trims():
    plan = compile_render(_comp([{"src": "a.mp4", "in": 0, "out": 8}]),
                          MEDIA, time_range=(2.0, 5.0))
    assert "trim=start=2:end=5" in plan.graph
    assert "atrim=start=2:end=5" in plan.graph
    assert plan.duration == 3.0


def test_streams_audio_only_has_no_video_chains():
    plan = compile_render(_comp([
        {"src": "a.mp4", "in": 0, "out": 5},
        {"src": "b.mp4", "in": 0, "out": 4,
         "transition_in": {"type": "fade", "duration": 0.5}},
    ]), MEDIA, mode="final", streams="a")
    assert plan.video_label == ""
    assert plan.audio_label
    assert "xfade" not in plan.graph
    assert "acrossfade=d=0.5" in plan.graph
    assert "scale=" not in plan.graph


def test_streams_video_only_has_no_audio_chains():
    plan = compile_render(_comp(
        [{"src": "a.mp4", "in": 0, "out": 5}],
        tracks_extra=[{"kind": "audio",
                       "clips": [{"src": "music.wav", "start": 0}]}]),
        MEDIA, streams="v")
    assert plan.audio_label == ""
    assert "anullsrc" not in plan.graph
    assert "amix" not in plan.graph
    assert plan.video_label


def test_graph_never_contains_hash_colors():
    comp = _comp([{"fill": "#AABBCC", "duration": 1},
                  {"src": "a.mp4", "in": 0, "out": 2, "fit": "contain"}])
    comp["project"]["background"] = "#112233"
    plan = compile_render(comp, MEDIA)
    assert "#" not in plan.graph  # '#' starts a comment in a script file
