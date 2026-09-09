"""Unit tests for renderer helpers that don't need a real ffmpeg.

The loudnorm pass-1 parse is exercised with a monkeypatched ``run_ffmpeg``:
a silent mix measures ``input_i = -inf``, which pass 2 rejects outright
("Value -inf for parameter 'measured_I' out of range" — ffmpeg exit 222,
killing the whole render; hit live on silent drone footage), so
``_measure_loudnorm`` must return ``None`` (= skip normalization) instead of
interpolating -inf into the filter string.
"""

import asyncio
from pathlib import Path

import composition as comp_mod
import renderer


def _silent_comp():
    comp = comp_mod.new_composition({"width": 320, "height": 180})
    comp["tracks"][0]["clips"] = [{"fill": "#204060", "duration": 1.0}]
    return comp


_CFG = {"i": -14.0, "tp": -1.5, "lra": 11.0}


def _measure_with_stderr(monkeypatch, tmp_path, stderr: str, cfg=None):
    async def fake_run(args, timeout=0, heavy=True):
        return 0, stderr

    monkeypatch.setattr(renderer, "run_ffmpeg", fake_run)
    return asyncio.run(renderer._measure_loudnorm(
        _silent_comp(), {}, dict(cfg or _CFG), Path(tmp_path)))


# +6 dB of gain lands the −9 dBTP peak at −3: ffmpeg stays linear.
_NORMAL = (
    'progress {"frame": 1}\n{\n"input_i" : "-20.00",\n'
    '"input_tp" : "-9.00",\n"input_lra" : "6.20",\n'
    '"input_thresh" : "-30.50",\n"target_offset" : "0.40"\n}\n'
)
_WIDE = (
    '{\n"input_i" : "-14.77",\n"input_tp" : "-1.45",\n"input_lra" : "6.30",\n'
    '"input_thresh" : "-25.00",\n"target_offset" : "0.60"\n}\n'
)


def test_silent_measurement_returns_none(monkeypatch, tmp_path):
    stderr = (
        '[Parsed_loudnorm] {\n"input_i" : "-inf",\n"input_tp" : "-inf",\n'
        '"input_lra" : "0.00",\n"input_thresh" : "-70.00",\n'
        '"target_offset" : "0.00"\n}\n'
    )
    assert _measure_with_stderr(monkeypatch, tmp_path, stderr) == (None, None)


def test_near_silent_measurement_returns_none(monkeypatch, tmp_path):
    # Below the -70 LUFS floor: "normalizing" would amplify the noise floor
    # by ~+56 dB — treat as silent.
    stderr = (
        '{\n"input_i" : "-84.30",\n"input_tp" : "-60.00",\n'
        '"input_lra" : "0.00",\n"input_thresh" : "-94.30",\n'
        '"target_offset" : "0.00"\n}\n'
    )
    assert _measure_with_stderr(monkeypatch, tmp_path, stderr) == (None, None)


def test_normal_measurement_returns_linear_two_pass(monkeypatch, tmp_path):
    flt, report = _measure_with_stderr(monkeypatch, tmp_path, _NORMAL)
    assert flt is not None
    assert "measured_I=-20.00" in flt
    assert "linear=true" in flt and flt.endswith(",aresample=48000")
    assert report["normalization_type"] == "linear"


def test_auto_mode_predicts_and_warns_on_dynamic_fallback(monkeypatch, tmp_path):
    flt, report = _measure_with_stderr(monkeypatch, tmp_path, _WIDE)
    assert "linear=true" in flt          # ffmpeg decides — and will go dynamic
    assert report["normalization_type"] == "dynamic"
    issues = []
    graph = renderer._apply_loudnorm("[a]__LOUDNORM__[out]", flt, report, issues)
    assert graph == f"[a]{flt}[out]"
    assert len(issues) == 1 and "DYNAMIC" in issues[0]["message"]
    assert 'mode to "linear"' in issues[0]["message"]


def test_linear_mode_never_enters_loudnorm(monkeypatch, tmp_path):
    flt, report = _measure_with_stderr(monkeypatch, tmp_path, _WIDE,
                                       cfg=dict(_CFG, mode="linear"))
    assert flt.startswith("volume=0.77dB,aresample=192000,alimiter=")
    assert "loudnorm" not in flt
    assert report["normalization_type"] == "linear"
    issues = []
    renderer._apply_loudnorm("__LOUDNORM__", flt, report, issues)
    assert issues == []


def test_silent_mix_substitutes_anull_with_a_warning():
    issues = []
    graph = renderer._apply_loudnorm("[a]__LOUDNORM__[out]", None, None, issues)
    assert graph == "[a]anull[out]"
    assert issues[0]["message"].startswith("mix is silent")


def test_unparseable_measurement_falls_back_to_single_pass(monkeypatch, tmp_path):
    flt, report = _measure_with_stderr(monkeypatch, tmp_path, "no json here")
    assert flt == "loudnorm=I=-14.0:TP=-1.5:LRA=11.0,aresample=48000"
    assert report is None


def test_render_budget_env_override(monkeypatch):
    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "2000")
    assert renderer._render_budget_bytes() == 2000 * 1e6
    monkeypatch.setattr(renderer, "_cgroup_memory_limit_bytes", lambda: None)
    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "not-a-number")
    assert renderer._render_budget_bytes() >= 1.5e9
    monkeypatch.delenv("VIDEO_TOOLS_RENDER_BUDGET_MB")
    assert renderer._render_budget_bytes() >= 1.5e9


def test_render_budget_honors_cgroup_limit(monkeypatch):
    monkeypatch.delenv("VIDEO_TOOLS_RENDER_BUDGET_MB", raising=False)
    monkeypatch.setattr(renderer, "_cgroup_memory_limit_bytes", lambda: 4e9)
    assert renderer._render_budget_bytes() == 4e9 * 0.4
    monkeypatch.setattr(renderer, "_cgroup_memory_limit_bytes", lambda: 2e9)
    assert renderer._render_budget_bytes() == 2e9 * 0.6


def test_video_encode_args_strip_audio():
    args = ["-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-profile:v", "high", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart"]
    assert renderer._video_encode_args(args) == [
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-profile:v", "high", "-movflags", "+faststart"]
