"""Grade validation, filter mapping, and LUT baking."""

import os
from pathlib import Path

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
