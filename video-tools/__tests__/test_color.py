"""Grade validation, filter mapping, LUT baking, and the colour-management
helpers (source tagging, conversion presets, LUT chains, strength baking)."""

import os
from pathlib import Path

import numpy as np
import pytest

import color


def test_validate_bounds():
    assert color.validate_color_spec({"saturation": 1.1, "contrast": 1.05}) == []
    problems = color.validate_color_spec({"saturation": 9, "exposure": -5})
    assert len(problems) == 2


def test_validate_unknown_keys():
    problems = color.validate_color_spec({"vibrance": 2})
    assert any("vibrance" in p for p in problems)


def test_validate_curves_points():
    ok = {"curves": {"all": [[0, 0], [0.5, 0.55], [1, 1]]}}
    assert color.validate_color_spec(ok) == []
    bad = {"curves": {"all": [[0.5, 0], [0.1, 1]]}}
    assert any("increasing" in p for p in color.validate_color_spec(bad))
    bad_ch = {"curves": {"x": [[0, 0], [1, 1]]}}
    assert any("r/g/b/all" in p for p in color.validate_color_spec(bad_ch))


def test_to_filters_order_and_atoms():
    filters = color.to_filters({
        "exposure": 0.5, "contrast": 1.1, "saturation": 1.2,
        "temperature": 5200,
        "curves": {"all": [[0, 0], [0.5, 0.52], [1, 1]]},
    })
    assert filters[0] == "exposure=exposure=0.5"
    assert filters[1] == "eq=contrast=1.1:saturation=1.2"
    assert filters[2] == "colortemperature=temperature=5200"
    assert filters[3] == "curves=all='0/0 0.5/0.52 1/1'"


def test_to_filters_noop_spec_is_empty():
    assert color.to_filters({"contrast": 1.0, "saturation": 1}) == []


def test_curves_preset():
    assert color.to_filters({"curves": {"preset": "vintage"}}) == ["curves=preset=vintage"]


def test_builtin_looks_baked_by_conftest():
    looks_dir = Path(os.environ["VIDEO_TOOLS_LOOKS_DIR"])
    for name in color.BUILTIN_LOOKS:
        assert (looks_dir / f"{name}.cube").exists()


def test_cube_format_and_size():
    text = color.bake_cube(color.BUILTIN_LOOKS["clean-punch"], size=17)
    lines = text.splitlines()
    assert "LUT_3D_SIZE 17" in lines[1]
    data = [l for l in lines if l and not l.startswith(("#", "LUT", "DOMAIN"))]
    assert len(data) == 17 ** 3
    r, g, b = map(float, data[0].split())
    assert all(0.0 <= v <= 1.0 for v in (r, g, b))


def test_identity_recipe_is_identity():
    text = color.bake_cube({}, size=5)
    data = [l for l in text.splitlines()
            if l and not l.startswith(("#", "LUT", "DOMAIN"))]
    # Red axis fastest: entry 1 is r=0.25, g=0, b=0.
    assert data[1].split() == ["0.250000", "0.000000", "0.000000"]
    assert data[-1].split() == ["1.000000", "1.000000", "1.000000"]


def test_teal_orange_pushes_shadows_blue():
    text = color.bake_cube(color.BUILTIN_LOOKS["teal-orange"], size=9)
    data = [l for l in text.splitlines()
            if l and not l.startswith(("#", "LUT", "DOMAIN"))]
    # A dark neutral gray (r=g=b=0.25): index r=2, g=2, b=2 → 2*81+2*9+2.
    r, g, b = map(float, data[2 * 81 + 2 * 9 + 2].split())
    assert b > r  # shadows lean teal/blue


def test_resolve_lut_builtin_vs_user():
    builtin = color.resolve_lut("teal-orange", lambda p: p)
    assert builtin.endswith("teal-orange.cube")
    user = color.resolve_lut("my/custom.cube", lambda p: "/resolved/" + p)
    assert user == "/resolved/my/custom.cube"


# ---------------------------------------------------------------------------
# Colour management
# ---------------------------------------------------------------------------


def test_normalize_input_aliases_and_errors():
    assert color.normalize_input({"matrix": "2020", "primaries": "rec2020",
                                  "transfer": "HLG", "range": "limited"}) == {
        "matrix": "bt2020nc", "primaries": "bt2020",
        "transfer": "arib-std-b67", "range": "tv"}
    assert color.normalize_input({"transfer": "pq", "range": None}) == {
        "transfer": "smpte2084"}
    for bad in ({"matrix": "bt2021"}, {"gamma": "x"}, "hlg", {"range": "wide"}):
        with pytest.raises(ValueError):
            color.normalize_input(bad)


def test_head_tags_declare_only_unknown_fields():
    # Untagged HD (or unknown height) → the full 709 set; SD → 601.
    assert color.head_tags(None, {}, 1080) == color.OUTPUT_TAGS
    assert color.head_tags(None, None, None) == color.OUTPUT_TAGS
    assert color.head_tags(None, {}, 576) == {
        "matrix": "bt470bg", "primaries": "bt470bg",
        "transfer": "smpte170m", "range": "tv"}
    tagged = {"color_space": "bt709", "color_transfer": "bt709",
              "color_primaries": "bt709", "color_range": "tv"}
    assert color.head_tags(None, tagged, 1080) == {}
    # Partially tagged (matrix only, the CLI-flag shape): fill the rest.
    assert color.head_tags(None, {"color_space": "bt2020nc"}, 2160) == {
        "primaries": "bt709", "transfer": "bt709", "range": "tv"}
    # Explicit input overrides a tagged source, per key.
    assert color.head_tags({"input": {"transfer": "hlg"}}, tagged, 1080) == {
        "transfer": "arib-std-b67"}
    # A convert preset implies its full source set; input overrides per key.
    assert color.head_tags({"convert": "hlg->rec709", "input": {"range": "full"}},
                           tagged, 1080) == {
        "matrix": "bt2020nc", "primaries": "bt2020",
        "transfer": "arib-std-b67", "range": "pc"}


def test_tag_filter_and_output_pin():
    assert color.tag_filter({}) == ""
    assert color.tag_filter({"transfer": "arib-std-b67"}) == "setparams=color_trc=arib-std-b67"
    assert color.OUTPUT_PIN == ("setparams=colorspace=bt709:color_primaries=bt709"
                                ":color_trc=bt709:range=tv")


def test_convert_chain_shape():
    chain = color.convert_filters({"convert": "hlg->rec709"})
    assert chain[0] == "zscale=t=linear:npl=1000"
    assert "format=gbrpf32le" in chain and "exposure=exposure=2.3" in chain
    assert any(c.startswith("tonemap=tonemap=mobius:param=0.5:peak=4.926") for c in chain)
    assert chain[-2:] == ["zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]
    assert color.convert_filters({}) == [] and color.convert_filters(None) == []


def test_pq_convert_chain_and_verdict_map():
    chain = color.convert_filters({"convert": "pq->rec709"})
    # PQ is absolute: npl=203 IS the BT.2408 anchor (measured a pure scale),
    # and ffmpeg's exposure filter caps at ±3 EV anyway.
    assert chain[0] == "zscale=t=linear:npl=203"
    assert not any(c.startswith("exposure") for c in chain)
    assert any(c.startswith("tonemap=tonemap=mobius:param=0.5:peak=49.26") for c in chain)
    assert chain[-2:] == ["zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]
    assert color.CONVERT_PRESETS["pq->rec709"]["input"] == {
        "matrix": "bt2020nc", "primaries": "bt2020",
        "transfer": "smpte2084", "range": "tv"}
    assert color.head_tags({"convert": "pq->rec709"}, {}, 1080)["transfer"] == "smpte2084"
    assert color.validate_color_spec({"convert": "pq->rec709"}) == []
    assert color.CONVERT_FOR == {"HLG": "hlg->rec709", "PQ": "pq->rec709"}


_TAGGED_709 = {"color_space": "bt709", "color_transfer": "bt709",
               "color_primaries": "bt709", "color_range": "tv"}


def test_edit_contract_sdr_sources_join_709():
    # Untagged HD: declared 709, nothing to convert, 8-bit, pinned.
    c = color.edit_contract({}, 1080)
    assert c.head == color.OUTPUT_PIN and not c.keep and c.convert == ""
    assert c.tail() == ["format=yuv420p", color.OUTPUT_PIN]
    assert c.tail(has_scale=True) == ["format=yuv420p", color.OUTPUT_PIN]
    assert c.scale("640:-2:flags=lanczos") == (
        "scale=640:-2:flags=lanczos:" + color.OUTPUT_MATRIX_OPTS)
    # cv2 read it with 601 → return through 601, relabel as the 709 it means.
    assert c.blur_chain() == ["scale=out_color_matrix=bt601:out_range=tv",
                              color.OUTPUT_PIN, "format=yuv420p", color.OUTPUT_PIN]
    # Fully tagged 709: nothing to declare, nothing to convert.
    t = color.edit_contract(_TAGGED_709, 1080)
    assert t.head == "" and t.convert == ""
    assert t.tail() == ["format=yuv420p", color.OUTPUT_PIN]
    assert t.blur_chain()[0] == "scale=out_color_matrix=bt709:out_range=tv"
    # 601-tagged SD: a real conversion, carried by the op's own scale or by
    # a no-size-change one when the op has none.
    sd = color.edit_contract({"color_space": "bt470bg", "color_transfer": "smpte170m",
                              "color_primaries": "bt470bg", "color_range": "tv"}, 576)
    assert sd.head == "" and sd.convert == "scale=" + color.OUTPUT_MATRIX_OPTS
    assert sd.tail() == [sd.convert, "format=yuv420p", color.OUTPUT_PIN]
    assert sd.tail(has_scale=True) == ["format=yuv420p", color.OUTPUT_PIN]
    assert sd.blur_chain() == [
        "scale=out_color_matrix=bt601:out_range=tv",
        "setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=smpte170m:range=tv",
        sd.convert, "format=yuv420p", color.OUTPUT_PIN]
    # Untagged SD is 601 by convention: declared, then converted.
    usd = color.edit_contract({}, 480)
    assert usd.head.startswith("setparams=colorspace=bt470bg") and usd.convert
    # A full-range 709 phone clip converts its range.
    pc = color.edit_contract(dict(_TAGGED_709, color_range="pc"), 1080)
    assert pc.convert == "scale=" + color.OUTPUT_MATRIX_OPTS
    assert pc.blur_chain()[0] == "scale=out_color_matrix=bt709:out_range=pc"


def test_edit_contract_hdr_and_wide_gamut_pass_through():
    hlg = {"color_space": "bt2020nc", "color_transfer": "arib-std-b67",
           "color_primaries": "bt2020", "color_range": "tv"}
    c = color.edit_contract(hlg, 2160)
    assert c.keep and c.head == "" and c.convert == "" and c.scale_opts == ""
    assert c.target == c.source
    pin = ("setparams=colorspace=bt2020nc:color_primaries=bt2020"
           ":color_trc=arib-std-b67:range=tv")
    assert c.tail() == [pin] and c.tail(has_scale=True) == [pin]
    assert c.scale("640:-2") == "scale=640:-2"
    assert c.blur_chain() == ["scale=out_color_matrix=bt2020:out_range=tv", pin,
                              "format=yuv420p", pin]
    # VUI-only PQ with an unknown matrix: the unknown fields are declared
    # with the player default, the file still passes through as HDR.
    pq = color.edit_contract({"color_transfer": "smpte2084",
                              "color_primaries": "bt2020"}, 2160)
    assert pq.keep and pq.head == "setparams=colorspace=bt709:range=tv"
    assert pq.pin.endswith("color_trc=smpte2084:range=tv")
    # BT.2020 primaries under an SDR transfer: wide gamut, kept too.
    wg = color.edit_contract(dict(hlg, color_transfer="bt2020-10"), 2160)
    assert wg.keep
    assert color.effective_tags({}, 720) == color.OUTPUT_TAGS
    assert not color.keeps_source_space(color.OUTPUT_TAGS)


def test_sharpen_filters_order_and_mapping():
    assert color.sharpen_filters({"sharpness": 1.0, "clarity": 0.5}) == [
        "cas=strength=0.5", "unsharp=5:5:1.5:5:5:0"]
    assert color.sharpen_filters({"sharpness": 0, "clarity": None}) == []
    assert color.grade_strength({"strength": 0.25}) == 0.25
    assert color.grade_strength({}) == 1.0 and color.grade_strength(None) == 1.0


def test_lut_entries_forms_keys_and_refs():
    assert color.lut_entries({"lut": "filmic"}) == [("filmic", 1.0)]
    assert color.lut_entries({"lut": ["a.cube", {"lut": "filmic", "strength": 0.6}]}) == [
        ("a.cube", 1.0), ("filmic", 0.6)]
    assert color.lut_entries({}) == [] and color.lut_entries(None) == []
    for bad in ({"lut": []}, {"lut": [{"strength": 0.5}]},
                {"lut": [{"lut": "x", "strength": 2}]},
                {"lut": [{"lut": "x", "foo": 1}]}, {"lut": 3}, {"lut": [""]}):
        with pytest.raises(ValueError):
            color.lut_entries(bad)
    assert color.lut_key("filmic", 1.0) == "filmic"
    assert color.lut_key("filmic", 0.6) == "filmic#0.6"
    assert color.lut_refs({"lut": ["filmic", "x.cube",
                                   {"lut": "x.cube", "strength": 0.3}]}) == ["x.cube"]
    spec = {"lut": ["x.cube", {"lut": "y.cube", "strength": 0.5}, "filmic"]}
    color.rewrite_lut_refs(spec, {"x.cube": "/r/x.cube", "y.cube": "/r/y.cube"})
    assert spec["lut"] == ["/r/x.cube", {"lut": "/r/y.cube", "strength": 0.5}, "filmic"]


def test_validate_new_keys():
    assert color.validate_color_spec({
        "strength": 0.5, "clarity": 1, "sharpness": 0,
        "convert": "hlg->rec709", "input": {"range": "pc"},
        "lut": [{"lut": "filmic", "strength": 0.4}]}) == []
    problems = color.validate_color_spec({
        "strength": 1.5, "convert": "rec2020->rec709",
        "input": {"matrix": "nope"}, "lut": [], "clarity": "x"})
    assert len(problems) == 5, problems
    assert any("hlg->rec709, pq->rec709" in p for p in problems), problems


def _cube_rows(path):
    return np.array([[float(v) for v in line.split()]
                     for line in Path(path).read_text().splitlines()
                     if line.strip() and not line.startswith(("#", "LUT", "DOMAIN", "TITLE"))])


def test_blend_cube_is_identity_lut_midpoint(tmp_path):
    src = tmp_path / "look.cube"
    src.write_text(color.bake_cube(color.BUILTIN_LOOKS["teal-orange"], size=9))
    outs = {}
    for s in (0.0, 0.5, 1.0):
        outs[s] = tmp_path / f"o{s}.cube"
        color.blend_cube(str(src), str(outs[s]), s)
    axis = np.linspace(0.0, 1.0, 9)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=-1)
    full = _cube_rows(src)
    assert np.allclose(_cube_rows(outs[0.0]), grid, atol=1e-5)
    assert np.allclose(_cube_rows(outs[1.0]), full, atol=1e-5)
    assert np.allclose(_cube_rows(outs[0.5]), (grid + full) / 2, atol=1e-5)
    assert "LUT_3D_SIZE 9" in outs[0.5].read_text()


def test_blend_cube_1d_with_domain(tmp_path):
    src = tmp_path / "one.cube"
    src.write_text('TITLE "t"\nLUT_1D_SIZE 3\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\n'
                   "0 0 0\n0.5 0.5 0.5\n1 1 1\n")
    dst = tmp_path / "half.cube"
    color.blend_cube(str(src), str(dst), 0.5)
    text = dst.read_text()
    assert "LUT_1D_SIZE 3" in text and "DOMAIN_MAX 2 2 2" in text and 'TITLE "t"' in text
    # identity over the [0, 2] domain is 0/1/2; mixed with 0/0.5/1 → 0/0.75/1.5
    assert np.allclose(_cube_rows(dst)[:, 0], [0.0, 0.75, 1.5])
    with pytest.raises(ValueError):
        color._parse_cube("LUT_3D_SIZE 2\n0 0 0\n")
