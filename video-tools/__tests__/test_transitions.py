"""Unit tests for transitions.py — wow-preset filter builders (pure
strings; execution is covered by the render smoke tests)."""

import pytest

import transitions


def test_is_preset():
    assert transitions.is_preset("whip_pan")
    assert not transitions.is_preset("fade")
    assert not transitions.is_preset(None)


def test_flash_cut_dips_black_by_default_white_on_request():
    head = transitions.head_filters("flash_cut", 0.3, 1920, 1080, 30)
    assert head[0].startswith("eq=brightness=-0.9:enable='lt(t,")
    assert "eq=brightness=-0.45" in head[1]
    tail = transitions.tail_filters("flash_cut", 0.3, 1920, 1080, 30, 5.0)
    assert len(tail) == 1 and "brightness=-0.35" in tail[0] \
        and "gte(t,4.95)" in tail[0]

    white = transitions.head_filters("flash_cut", 0.3, 1920, 1080, 30,
                                     {"flash": "white"})
    assert white[0].startswith("eq=brightness=0.9:")
    punch = transitions.head_filters("zoom_punch", 0.4, 1280, 720, 30)
    assert any("brightness=-0.5" in f for f in punch)


def test_xfade_presets_map_to_cores():
    assert transitions.is_xfade_preset("whip_left")
    assert not transitions.is_xfade_preset("whip_pan")
    # Whips: eased wrap-around custom expr (the Premiere Offset trick) —
    # both frames share the wrap phase; swap crossfades in the 30–70%
    # window under peak blur. Runs in gbrp (needs_rgb). Fully INLINED:
    # st/ld in a per-pixel expr race across threads → static.
    left = transitions.xfade_option("whip_left", 0.4, 3.6)
    assert left.startswith("xfade=transition=custom:expr='")
    assert "st(" not in left and "ld(" not in left
    # Adjacent strip, direction = CAMERA pan: whip_left pans left, so the
    # content slides RIGHT and the next shot comes from the LEFT.
    assert "clip((X-W*((1-P)*(1-P)*(3-2*(1-P)))),0,W-1)" in left
    assert "clip((X-W*((1-P)*(1-P)*(3-2*(1-P))))+W,0,W-1)" in left
    assert "/(W*0.05)+0.5,0,1)" in left
    assert ":duration=0.4:offset=3.6" in left
    right = transitions.xfade_option("whip_right", 0.4, 3.6)
    assert "clip((X+W*((1-P)*(1-P)*(3-2*(1-P))))-W,0,W-1)" in right
    assert transitions.needs_rgb("whip_left")
    assert not transitions.needs_rgb("luma_wipe")
    # Plain xfade names pass through untouched.
    assert transitions.xfade_option("dissolve", 0.5, 2.0) == \
        "xfade=transition=dissolve:duration=0.5:offset=2"
    # luma_wipe is a structural maskedmerge join — only its mask expr
    # lives here. The bias ramp must guarantee completion at Pr=1.
    geq = transitions.luma_wipe_mask_geq(0.8)
    assert geq.startswith("geq='clip((p(X,Y)-255*(1-clip(T/0.8,0,1)))*6")
    assert "+383*clip(T/0.8,0,1)-128" in geq

    # Whips get the same blur edge treatment as whip_pan; luma_wipe none.
    assert transitions.head_filters("whip_left", 0.4, 1920, 1080, 30) == \
        transitions.head_filters("whip_pan", 0.4, 1920, 1080, 30)
    assert transitions.head_filters("luma_wipe", 0.8, 1920, 1080, 30) == []
    assert transitions.tail_filters("luma_wipe", 0.8, 1920, 1080, 30, 5.0) == []


def test_whip_pan_blur_ramps_toward_the_cut():
    head = transitions.head_filters("whip_pan", 0.4, 1920, 1080, 30)
    # Head: strongest blur first, decaying after the cut.
    assert [f.split(":")[1] for f in head] == ["radius=18", "radius=11", "radius=5"]
    tail = transitions.tail_filters("whip_pan", 0.4, 1920, 1080, 30, 4.0)
    # Tail: blur builds INTO the cut.
    assert [f.split(":")[1] for f in tail] == ["radius=5", "radius=11", "radius=18"]
    assert "between(t,3.8," in tail[0]


def test_zoom_punch_uses_zoompan_with_flash():
    head = transitions.head_filters("zoom_punch", 0.4, 1280, 720, 30)
    assert head[0].startswith("zoompan=z='if(lt((on/30),0.2),1+0.25*")
    assert ":s=1280x720" in head[0]
    assert head[1].startswith("eq=brightness=-0.5")
    tail = transitions.tail_filters("zoom_punch", 0.4, 1280, 720, 30, 3.0)
    assert "1+0.18*pow(((on/30)-2.8)/0.2,2)" in tail[0]


def test_shake_decays_and_has_no_tail():
    [zp] = transitions.head_filters("shake", 0.5, 1920, 1080, 30)
    assert "exp(-(on/30)*24)" in zp        # k = 6/(0.5/2)
    assert "sin(on*2.9)" in zp and "cos(on*3.7)" in zp
    assert transitions.tail_filters("shake", 0.5, 1920, 1080, 30, 5.0) == []


def test_glitch_windows_cover_the_duration():
    head = transitions.head_filters("glitch", 0.3, 1920, 1080, 30)
    assert sum(1 for f in head if f.startswith("rgbashift")) == 3
    assert head[-1].startswith("noise=alls=30")
    assert "lt(t,0.3)" in head[-1]


def test_spin_rotates_opposite_directions():
    head = transitions.head_filters("spin", 0.4, 1920, 1080, 30)
    tail = transitions.tail_filters("spin", 0.4, 1920, 1080, 30, 4.0)
    assert head[0].startswith("rotate=a='if(lt(t,0.2),pow(")
    assert tail[0].startswith("rotate=a='if(gte(t,3.8),-pow(")


def test_kolder_zoom_mirror_tile_and_eased_ramp():
    tail = transitions.tail_filters("zoom_out", 0.4, 1920, 1080, 30, 4.0)
    # Mirror tile: pad to 2x + fillborders mirror, then eased zoompan.
    assert tail[0] == "pad=3840:2160:960:540:color=black"
    assert tail[1] == ("fillborders=left=960:right=960:top=540:bottom=540"
                       ":mode=mirror")
    # zoom_out mirrors on the OUTGOING side → tail gets the SHORT window
    # (40% of 0.4s = 0.16s, tz=3.84), ease-IN: max speed lands on the cut.
    assert "zoompan=z='st(0,clip(((on/30)-3.84)/0.16,0,1));" in tail[2]
    assert "st(0,ld(0)*ld(0));2-ld(0)" in tail[2]               # 2 → 1
    # Blur ramp builds INTO the cut.
    assert "gblur=sigma=2" in tail[3] and "gblur=sigma=12" in tail[5]

    # ONE CONTINUOUS MOTION across the cut: the incoming side continues,
    # DECELERATING (ease-out) over the LONG window — a single speed bell,
    # no mid-transition bump.
    head = transitions.head_filters("zoom_out", 0.4, 1920, 1080, 30)
    assert "clip((on/30)/0.24,0,1)" in head[2]
    assert "st(0,1-(1-ld(0))*(1-ld(0)));6-4*ld(0)" in head[2]   # 6 → 2 pull
    assert "gblur=sigma=12" in head[3] and "gblur=sigma=2" in head[5]

    # zoom_in mirrors on the INCOMING side → tail long, head short.
    tail_in = transitions.tail_filters("zoom_in", 0.4, 1920, 1080, 30, 4.0)
    assert "clip(((on/30)-3.76)/0.24,0,1)" in tail_in[2]
    assert ";2+4*ld(0)" in tail_in[2]                           # 2 → 6 punch
    head_in = transitions.head_filters("zoom_in", 0.4, 1920, 1080, 30)
    assert "clip((on/30)/0.16,0,1)" in head_in[2]
    assert ";1+ld(0)" in head_in[2]                             # 1 → 2 push


def test_unknown_preset_raises():
    with pytest.raises(ValueError):
        transitions.head_filters("swoosh", 0.3, 1920, 1080, 30)
