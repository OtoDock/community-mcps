"""Unit tests for colormatch.py — ref parsing, LUT math on synthetic
frames, cube output. The end-to-end cut-matching A/B lives in
test_render_smoke.py."""

import numpy as np
import pytest

import colormatch


def test_parse_ref():
    assert colormatch.parse_ref("workspace/a.mp4@11.8") == ("workspace/a.mp4", 11.8)
    assert colormatch.parse_ref("dir@2/b.mp4@0") == ("dir@2/b.mp4", 0.0)
    for bad in ("a.mp4", "a.mp4@x", "@1.0", "a.mp4@-2", 3):
        with pytest.raises(ValueError):
            colormatch.parse_ref(bad)


def _flat_frames(bgr, n=2, noise=6):
    """Synthetic frames around a BGR color with a little spread (flat
    frames have ~zero std — the scale clamp path)."""
    rng = np.random.default_rng(7)
    base = np.array(bgr, dtype=np.int16)
    return [np.clip(base + rng.integers(-noise, noise, (60, 80, 3)),
                    0, 255).astype(np.uint8) for _ in range(n)]


def _lut_lookup(lut, size, rgb):
    """Nearest-index lookup of an (size³, 3) red-fastest LUT grid."""
    idx = [int(round(c * (size - 1))) for c in rgb]   # r, g, b
    flat = idx[2] * size * size + idx[1] * size + idx[0]
    return lut[flat]


def test_match_moves_target_color_toward_reference():
    size = 33
    cold = _flat_frames((140, 90, 60))    # BGR: blue-ish
    warm = _flat_frames((60, 100, 170))   # BGR: warm orange
    tgt = colormatch._rgb_quantiles(cold)
    ref = colormatch._rgb_quantiles(warm)
    lut = colormatch._build_lut(tgt, ref, 1.0, size)

    mapped = _lut_lookup(lut, size, (60 / 255, 90 / 255, 140 / 255))  # RGB
    want = np.array([170, 100, 60]) / 255.0                            # RGB
    before = np.linalg.norm(np.array([60, 90, 140]) / 255.0 - want)
    after = np.linalg.norm(mapped - want)
    assert after < before * 0.2, (before, after, mapped)


def test_gamma_difference_is_recovered():
    """Quantile curves capture tone differences an affine cannot: a
    gamma-darkened copy must map back onto the original."""
    rng = np.random.default_rng(3)
    base = rng.integers(20, 235, (80, 100, 3)).astype(np.uint8)
    dark = (255.0 * (base / 255.0) ** 1.8).astype(np.uint8)
    tgt = colormatch._rgb_quantiles([dark])
    ref = colormatch._rgb_quantiles([base])
    size = 33
    lut = colormatch._build_lut(tgt, ref, 1.0, size)
    for v in (0.2, 0.5, 0.8):
        mapped = _lut_lookup(lut, size, (v ** 1.8,) * 3)
        assert abs(float(mapped[0]) - v) < 0.06, (v, mapped)


def test_strength_zero_is_identity_and_half_is_between():
    size = 17
    a = colormatch._rgb_quantiles(_flat_frames((200, 60, 40)))
    b = colormatch._rgb_quantiles(_flat_frames((40, 60, 200)))
    ident = colormatch._build_lut(a, b, 0.0, size)
    axis = np.linspace(0, 1, size)
    bb, gg, rr = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([rr.ravel(), gg.ravel(), bb.ravel()], axis=-1)
    assert np.allclose(ident, grid, atol=1e-9)

    full = colormatch._build_lut(a, b, 1.0, size)
    half = colormatch._build_lut(a, b, 0.5, size)
    assert np.allclose(half, (grid + full) / 2, atol=1e-9)


def test_write_cube_format(tmp_path):
    size = 5
    lut = np.tile(np.linspace(0, 1, size * size * size)[:, None], (1, 3))
    path = tmp_path / "m.cube"
    colormatch.write_cube(lut, size, str(path))
    lines = path.read_text().splitlines()
    assert f"LUT_3D_SIZE {size}" in lines[1]
    assert len([l for l in lines if l and not l.startswith(("#", "LUT", "DOMAIN"))]) == size ** 3


def test_sample_window_clamps():
    assert colormatch.sample_window(1.0, 0.0, 5.0) == [0.85, 1.0, 1.15]
    assert colormatch.sample_window(0.05, 0.0, 5.0) == [0.0, 0.05, 0.2]
    assert colormatch.sample_window(4.99, 0.0, 5.0) == [4.84, 4.99, 5.0]
