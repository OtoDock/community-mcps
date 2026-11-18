"""Every path at the proxy, writes as writes; a render in the background with
its status and its cancel; never a half file at the output's name; the busy
answer; the filmstrip; the timeline as data; update_clip's flat form."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import composition as comp_mod
import jobs
import renderer
import shared
from conftest import HAVE_FFMPEG, REAL_READ_BESIDE, REAL_WRITE_BESIDE
from fftools import FFMPEG


@pytest.fixture
def proxy(monkeypatch):
    """A stand-in for the proxy's resolve-path: ``answers`` maps a sent path
    to ``(agents_relative, agent)``; every request is recorded. It refuses
    what the platform refuses: a write in the printed form, led by the
    session's own agent (the platform admits that form for a read only).
    Another relative write it places as the answers say, as the platform
    places it under the session's folder."""
    sent: list[tuple[str, bool]] = []
    answers: dict[str, tuple[str, str]] = {"/workspace": ("ag/workspace", "ag")}

    def resolve(path, writing=False):
        sent.append((path, writing))
        if writing and not path.startswith("/") and path.split("/", 1)[0] == "ag":
            return None, "", "proxy resolve-path 403: a write names the session's own form"
        if path in answers:
            rel, agent = answers[path]
            return rel, agent, ""
        return None, "", "proxy resolve-path 403: denied"
    monkeypatch.setattr(shared, "_resolve_via_proxy", resolve)
    monkeypatch.setattr(shared, "_write_beside", REAL_WRITE_BESIDE)
    monkeypatch.setattr(shared, "_read_beside", REAL_READ_BESIDE)
    shared._agent_var.set("")
    return {"sent": sent, "answers": answers}


def test_a_container_path_goes_to_the_proxy_like_every_form(proxy):
    proxy["answers"]["ag/workspace/a.mp4"] = ("ag/workspace/a.mp4", "ag")
    assert shared._resolve_path("/agents/ag/workspace/a.mp4") == "/agents/ag/workspace/a.mp4"
    with pytest.raises(ValueError, match="denied"):
        shared._resolve_path("/agents/other/users/u/private.mp4")
    assert proxy["sent"] == [("ag/workspace/a.mp4", False), ("other/users/u/private.mp4", False)]


def test_an_answer_is_held_to_the_sessions_own_tree(proxy):
    proxy["answers"]["/workspace/x.mp4"] = ("other/workspace/x.mp4", "ag")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        shared._resolve_path("/workspace/x.mp4")
    proxy["answers"]["/workspace/y.mp4"] = ("ag/workspace/y.mp4", "")
    with pytest.raises(ValueError, match="without the session's agent"):
        shared._resolve_path("/workspace/y.mp4")


def test_a_write_is_asked_as_a_write_and_its_printed_form_is_mapped(proxy):
    proxy["answers"]["/workspace/in.mp4"] = ("ag/workspace/in.mp4", "ag")
    proxy["answers"]["/workspace/out.mp4"] = ("ag/workspace/out.mp4", "ag")
    shared._resolve_path("/workspace/in.mp4")
    assert shared._resolve_path("ag/workspace/out.mp4", writing=True) == "/agents/ag/workspace/out.mp4"
    assert proxy["sent"][-1] == ("/workspace/out.mp4", True)


def test_a_write_in_the_printed_form_learns_the_agent_first(proxy):
    """A fresh request whose first path is a write the tool printed
    (``<agent>/workspace/...``): the agent is learned from a read answer,
    then the write goes in the session's own form."""
    proxy["answers"]["/workspace/out.mp4"] = ("ag/workspace/out.mp4", "ag")
    assert shared._resolve_path("ag/workspace/out.mp4", writing=True) == "/agents/ag/workspace/out.mp4"
    assert proxy["sent"] == [("/workspace", False), ("/workspace/out.mp4", True)]


def test_create_print_and_edit_round_trip_in_the_printed_form(proxy, monkeypatch, tmp_path):
    """The path a create prints is the path the next request edits by."""
    import project
    agents = tmp_path / "agents"
    (agents / "ag" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    proxy["answers"]["/workspace/c.vproj.json"] = ("ag/workspace/c.vproj.json", "ag")
    created = asyncio.run(project.handle_create_composition({"path": "/workspace/c.vproj.json"}))
    printed = created.split("\n", 1)[0].removeprefix("Created ")
    assert printed == "ag/workspace/c.vproj.json"
    shared._agent_var.set("")                       # a new request
    edited = asyncio.run(project.handle_edit_composition(
        {"path": printed, "operations": [{"op": "set_project", "fps": 25}]}))
    assert "Saved ag/workspace/c.vproj.json" in edited
    assert json.loads((agents / "ag" / "workspace" / "c.vproj.json").read_text())["project"]["fps"] == 25


def test_a_link_at_a_name_that_differs_only_in_its_normal_form_is_confined(proxy, monkeypatch, tmp_path):
    """The name is matched first, then every link followed and the place
    confined: a decomposed sibling that leads out of the tree is refused."""
    import unicodedata
    agents = tmp_path / "agents"
    (agents / "ag" / "workspace").mkdir(parents=True)
    (agents / "other" / "workspace").mkdir(parents=True)
    (agents / "other" / "workspace" / "secret.mp4").write_text("x")
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    composed = "caf\u00e9.mp4"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    (agents / "ag" / "workspace" / decomposed).symlink_to(agents / "other" / "workspace" / "secret.mp4")
    proxy["answers"][f"/workspace/{composed}"] = (f"ag/workspace/{composed}", "ag")
    with pytest.raises(ValueError, match="names a link the platform did not judge"):
        shared._resolve_path(f"/workspace/{composed}")
    # A plain decomposed name inside the tree still matches.
    (agents / "ag" / "workspace" / decomposed).unlink()
    (agents / "ag" / "workspace" / decomposed).write_text("x")
    assert shared._resolve_path(f"/workspace/{composed}") == str(
        (agents / "ag" / "workspace" / decomposed).resolve())


def test_a_matched_name_that_is_a_link_inside_the_agent_is_refused(proxy, monkeypatch, tmp_path):
    """The platform judged the name it answered, with every link already
    followed. A sibling that differs only in its normal form and is a link
    leads to a place nobody judged, even inside the same agent: refused."""
    import unicodedata
    agents = tmp_path / "agents"
    (agents / "ag" / "workspace").mkdir(parents=True)
    (agents / "ag" / "users" / "olga").mkdir(parents=True)
    (agents / "ag" / "users" / "olga" / "private.mp4").write_text("x")
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    composed = "caf\u00e9.mp4"
    decomposed = unicodedata.normalize("NFD", composed)
    (agents / "ag" / "workspace" / decomposed).symlink_to(agents / "ag" / "users" / "olga" / "private.mp4")
    proxy["answers"][f"/workspace/{composed}"] = (f"ag/workspace/{composed}", "ag")
    with pytest.raises(ValueError, match="names a link the platform did not judge"):
        shared._resolve_path(f"/workspace/{composed}")
    # A link already at a parent folder of a new output is refused the same way.
    (agents / "ag" / "workspace" / "out").symlink_to(agents / "ag" / "users" / "olga")
    proxy["answers"]["/workspace/out/new.mp4"] = ("ag/workspace/out/new.mp4", "ag")
    with pytest.raises(ValueError, match="names a link the platform did not judge"):
        shared._resolve_path("/workspace/out/new.mp4", writing=True)


def test_a_file_beside_a_source_is_written_only_where_the_session_may(proxy):
    proxy["answers"]["/workspace/a.analysis.json"] = ("ag/workspace/a.analysis.json", "ag")
    assert shared._write_beside("/agents/ag/workspace/a.analysis.json") == "/agents/ag/workspace/a.analysis.json"
    assert proxy["sent"][-1] == ("/workspace/a.analysis.json", True)
    assert shared._write_beside("/agents/ag/knowledge/a.analysis.json") is None


def test_a_file_beside_a_source_is_read_only_where_the_session_may(proxy):
    """A stabilization cache is asked as a read, like any file the session
    names. Refused, it is a miss. Beside a computer's file nothing is asked."""
    proxy["answers"]["ag/workspace/a.stab-1.trf"] = ("ag/workspace/a.stab-1.trf", "ag")
    assert shared._read_beside("/agents/ag/workspace/a.stab-1.trf") == "/agents/ag/workspace/a.stab-1.trf"
    assert proxy["sent"][-1] == ("ag/workspace/a.stab-1.trf", False)
    assert shared._read_beside("/agents/ag/users/olga/a.stab-1.trf") is None
    sent = len(proxy["sent"])
    assert shared._read_beside("/agents/.remote-host-cache/s1/a.stab-1.trf") is None
    assert len(proxy["sent"]) == sent


def test_every_write_site_asks_as_a_write(monkeypatch, tmp_path):
    import project
    import quickops
    seen: list[tuple[str, bool]] = []

    def record(path, writing=False):
        seen.append((path, writing))
        return str(tmp_path / Path(path).name)
    monkeypatch.setattr(project, "_resolve_path", record)
    asyncio.run(project.handle_create_composition({"path": "/workspace/c.vproj.json"}))
    assert seen[0] == ("/workspace/c.vproj.json", True)
    seen.clear()
    asyncio.run(project.handle_edit_composition(
        {"path": "/workspace/c.vproj.json", "operations": [{"op": "set_project", "fps": 25}]}))
    assert seen[0] == ("/workspace/c.vproj.json", True)
    assert callable(quickops.handle_edit_video)


# ── The page a still or a motion clip renders ────────────────────────────


class _Route:
    def __init__(self, url):
        self.request = type("R", (), {"url": url})()
        self.verdict = ""

    async def continue_(self):
        self.verdict = "load"

    async def abort(self):
        self.verdict = "refused"


def test_a_pages_files_load_only_from_the_call_or_where_the_session_reads(monkeypatch, tmp_path):
    import motion
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "still.html").write_text("x")
    readable = tmp_path / "agents" / "ag" / "workspace" / "a.png"
    readable.parent.mkdir(parents=True)
    readable.write_text("x")
    asked: list[str] = []

    def resolve(path, writing=False):
        asked.append(path)
        if path == str(readable):
            return path
        raise ValueError("denied")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    guard = motion._page_guard(scratch)

    def verdict(url):
        route = _Route(url)
        asyncio.run(guard(route))
        return route.verdict
    assert verdict(f"file://{scratch}/still.html") == "load"
    assert verdict(f"file://{readable}") == "load"
    assert verdict(f"file://{readable}") == "load"
    assert asked.count(str(readable)) == 1          # once per file per call
    assert verdict(f"file://{tmp_path}/agents/other/secret.png") == "refused"
    assert verdict(f"file://{scratch}/../agents/other/secret.png") == "refused"
    assert verdict("https://example.com/x.png") == "refused"
    assert verdict("data:image/png;base64,AAAA") == "load"


# ── update_clip's flat form, the timeline as data ────────────────────────


def test_update_clip_takes_a_clips_fields_flat():
    comp = comp_mod.new_composition()
    comp["tracks"][0]["clips"] = [{"fill": "black", "duration": 2}, {"fill": "white", "duration": 2}]
    comp, results = comp_mod.apply_operations(comp, [
        {"op": "update_clip", "track": "video", "index": 1, "volume_db": -6},
        {"op": "update_clip", "index": 0, "nonsense": 1}])
    assert comp["tracks"][0]["clips"][1]["volume_db"] == -6
    assert results[0].startswith("ok") and results[1].startswith("error")
    assert "nonsense" not in comp["tracks"][0]["clips"][0]


def test_the_timeline_as_data():
    import project
    comp = comp_mod.new_composition()
    comp["tracks"][0]["clips"] = [{"fill": "black", "duration": 2, "label": "intro"},
                                  {"fill": "white", "duration": 3,
                                   "transition_in": {"type": "fade", "duration": 0.5}}]
    data = project._timeline_json(comp, comp, {}, [])
    assert data["ok"] is True and data["duration"] == pytest.approx(4.5)
    assert [c["label"] for c in data["base"]] == ["intro", "white"]
    assert data["base"][1]["start"] == pytest.approx(1.5)
    json.dumps(data)


# ── Jobs ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_jobs():
    jobs.reset_for_tests()
    yield
    jobs.reset_for_tests()


def _plan(tmp_path, out="ag/workspace/o.mp4") -> renderer.RenderPlan:
    scratch = tmp_path / "plan"
    scratch.mkdir(exist_ok=True)
    return renderer.RenderPlan(
        mode="preview", out=f"/agents/{out}", resolved={}, media_info={}, issues=[],
        tmp=scratch, scale=1.0, time_range=None, crf=None, captions_ass=None,
        captions_fontsdir=None, luts={}, duration=10.0)


def test_a_job_runs_reports_and_answers_only_its_own_place(monkeypatch, tmp_path, fresh_jobs):
    release = asyncio.Event()

    async def fake_run(plan, *, token="", progress=None, cancelled=None):
        progress(42.0)
        await release.wait()
        return {"output": plan.out, "mode": "preview"}
    monkeypatch.setattr(renderer, "run_render", fake_run)
    place = {"/workspace/o.mp4": "/agents/ag/workspace/o.mp4"}
    monkeypatch.setattr(jobs, "_resolve_path",
                        lambda p, writing=False: place[p] if p in place else (_ for _ in ()).throw(ValueError("x")))

    async def main():
        job = await jobs.start(_plan(tmp_path), lambda r: "Rendered (preview)")
        await asyncio.sleep(0.01)
        assert jobs.busy()
        mid = jobs.status(job.id)
        assert mid["state"] == "running" and mid["percent"] == 42.0 and mid["output"] == "ag/workspace/o.mp4"
        release.set()
        await job.task
        done = jobs.status(job.id)
        assert done["state"] == "done" and done["percent"] == 100.0 and done["report"] == "Rendered (preview)"
        assert not jobs.busy()
        # Another session's place, and an unknown id, answer alike.
        place["/workspace/o.mp4"] = "/agents/other/workspace/o.mp4"
        assert jobs.status(job.id)["state"] == "lost"
        assert jobs.status("nope") == {"job_id": "nope", "state": "lost", "message": jobs.LOST}
    asyncio.run(main())


def test_a_job_is_cancelled_its_plan_discarded(monkeypatch, tmp_path, fresh_jobs):
    async def slow_run(plan, *, token="", progress=None, cancelled=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(renderer, "run_render", slow_run)
    monkeypatch.setattr(jobs, "_resolve_path", lambda p, writing=False: "/agents/ag/workspace/o.mp4")

    async def main():
        plan = _plan(tmp_path)
        job = await jobs.start(plan, str)
        await asyncio.sleep(0.01)
        assert jobs.cancel(job.id)["state"] == "cancelled"
        await asyncio.gather(job.task, return_exceptions=True)
        assert jobs.status(job.id)["state"] == "cancelled"
        assert not plan.tmp.exists()
    asyncio.run(main())


def test_a_fifth_waiting_job_and_an_output_on_a_machine_are_refused(monkeypatch, tmp_path, fresh_jobs):
    async def slow_run(plan, *, token="", progress=None, cancelled=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(renderer, "run_render", slow_run)

    async def main():
        started = [await jobs.start(_plan(tmp_path), str)]
        await asyncio.sleep(0.01)            # the first takes its turn and runs
        started += [await jobs.start(_plan(tmp_path), str) for _ in range(jobs.MAX_WAITING)]
        assert [j.state for j in started] == ["running"] + ["waiting"] * jobs.MAX_WAITING
        with pytest.raises(ValueError, match="already wait"):
            await jobs.start(_plan(tmp_path), str)
        with pytest.raises(ValueError, match="on a computer"):
            await jobs.start(_plan(tmp_path, ".remote-host-cache/s1/o.mp4"), str)
        for j in started:
            j.task.cancel()
        await asyncio.gather(*(j.task for j in started), return_exceptions=True)
    asyncio.run(main())


def test_the_busy_route_answers_whether_a_job_waits_or_runs(fresh_jobs):
    from starlette.testclient import TestClient

    import server
    with TestClient(server.starlette_app) as client:
        assert client.get("/busy").json() == {"busy": False}
        jobs._jobs["j"] = jobs.Job(id="j", output="/agents/a/o.mp4", place="a/o.mp4", state="running")
        assert client.get("/busy").json() == {"busy": True}


def test_the_manifest_declares_it_and_names_the_platform_its_paths_need():
    """1.7.0 is the first platform whose resolve-path names the session's
    agent, which every path check here needs; 1.9.1 installs keep it."""
    root = Path(__file__).parent.parent
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["server"]["busy_endpoint"] == "/busy"
    assert manifest["platform_min_version"] == "1.7.0"
    registry = json.loads((root.parent / "registry.json").read_text())
    entry = next(m for m in registry["mcps"] if m["name"] == "video-tools")
    assert entry["version"] == manifest["version"]
    assert entry["platform_min_version"] == "1.7.0"


# ── With ffmpeg: the partial file, progress, the filmstrip ───────────────


needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")


def _clip(path: Path, seconds: int = 3):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)


@needs_ffmpeg
def test_a_render_takes_its_name_only_when_it_succeeded(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    _clip(clip)
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 25})
    comp["tracks"][0]["clips"] = [{"src": str(clip)}]
    comp_path = tmp_path / "edit.vproj.json"
    comp_mod.save_composition(str(comp_path), comp)
    out = tmp_path / "out.mp4"
    seen: list[float] = []

    async def main():
        plan = await renderer.plan_render(str(comp_path), lambda p, writing=False: p,
                                          output_path=str(out))
        return await renderer.run_render(plan, token="t1", progress=seen.append)
    result = asyncio.run(main())
    assert result["output"] == str(out) and out.exists()
    assert seen and max(seen) <= 99.0
    assert not list(tmp_path.glob(".out.partial-*"))

    # A failed encode leaves neither a partial file nor the output.
    out.unlink()
    import fftools

    async def broken(args, **kw):
        Path(args[-1]).write_bytes(b"half")
        raise fftools.FFmpegError("broken")
    monkeypatch.setattr(renderer, "run_ffmpeg", broken)

    async def again():
        plan = await renderer.plan_render(str(comp_path), lambda p, writing=False: p,
                                          output_path=str(out))
        await renderer.run_render(plan, token="t2")
    with pytest.raises(fftools.FFmpegError):
        asyncio.run(again())
    assert not out.exists() and not list(tmp_path.glob(".out.partial-*"))


@needs_ffmpeg
def test_a_stale_partial_file_of_the_output_is_removed(tmp_path):
    stale = tmp_path / ".out.partial-old.mp4"
    stale.write_bytes(b"x")
    renderer._remove_stale_partials(str(tmp_path / "out.mp4"))
    assert not stale.exists()


@needs_ffmpeg
def test_the_filmstrip_is_one_small_picture(tmp_path, monkeypatch):
    import analysis
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=4)
    monkeypatch.setattr(analysis, "_resolve_path", lambda p, writing=False: p)
    answer = json.loads(asyncio.run(analysis.handle_render_filmstrip(
        {"path": str(clip), "count": 4, "height": 45})))
    strip = Path(tmp_path / "clip.filmstrip-4x45.jpg")
    assert strip.exists() and answer["count"] == 4 and answer["frame_width"] == 80
    assert answer["times"] == [0.5, 1.5, 2.5, 3.5]
    from PIL import Image
    assert Image.open(strip).size == (4 * 80, 45)
    assert "Error" in asyncio.run(analysis.handle_render_filmstrip({"path": str(clip), "count": 99}))
    assert answer["start"] == 0 and answer["end"] == 4.0

    part = json.loads(asyncio.run(analysis.handle_render_filmstrip(
        {"path": str(clip), "count": 2, "height": 45, "start": 1, "end": 3})))
    assert part["times"] == [1.5, 2.5] and (part["start"], part["end"]) == (1, 3)
    assert part["output"].endswith("clip.filmstrip-2x45-1-3.jpg")
    assert Image.open(tmp_path / "clip.filmstrip-2x45-1-3.jpg").size == (2 * 80, 45)


def _strip_answer(monkeypatch, tmp_path, args, duration="60"):
    """The filmstrip's answer for a 1080p file of ``duration`` seconds, with
    ffmpeg faked."""
    import analysis
    ran: list = []

    async def probe(path):
        return {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                "format": {"duration": duration}}

    async def run(a, **kw):
        ran.append(a)
    monkeypatch.setattr(analysis, "_resolve_path", lambda path, writing=False: str(tmp_path / Path(path).name))
    monkeypatch.setattr(analysis, "probe", probe)
    monkeypatch.setattr(analysis, "run_ffmpeg", run)
    answer = asyncio.run(analysis.handle_render_filmstrip({"path": "/workspace/v.mp4", **args}))
    return answer, ran


def test_a_filmstrip_of_one_span_takes_its_frames_inside_it(monkeypatch, tmp_path):
    answer, ran = _strip_answer(monkeypatch, tmp_path, {"count": 4, "start": 10, "end": 18})
    answer = json.loads(answer)
    assert answer["times"] == [11.0, 13.0, 15.0, 17.0]
    seeks = [float(ran[0][i + 1]) for i, a in enumerate(ran[0]) if a == "-ss"]
    assert seeks == answer["times"]
    assert answer["output"].endswith("v.filmstrip-4x90-10-18.jpg")


def test_two_spans_a_few_milliseconds_apart_never_share_a_file(monkeypatch, tmp_path):
    names = {json.loads(_strip_answer(monkeypatch, tmp_path, span, duration="3600")[0])["output"]
             for span in ({"start": 1234.567, "end": 1300}, {"start": 1234.571, "end": 1300})}
    assert len(names) == 2
    assert any(n.endswith("v.filmstrip-10x90-1234.567-1300.jpg") for n in names)
    head = json.loads(_strip_answer(monkeypatch, tmp_path, {"end": 2.5})[0])["output"]
    assert head.endswith("v.filmstrip-10x90-0-2.5.jpg")


def test_a_span_past_the_end_stops_at_the_end(monkeypatch, tmp_path):
    answer, _ = _strip_answer(monkeypatch, tmp_path, {"count": 2, "start": 50, "end": 75})
    answer = json.loads(answer)
    assert answer["end"] == 60 and answer["times"] == [52.5, 57.5]


@pytest.mark.parametrize("span", [
    {"start": 60}, {"start": 70, "end": 80}, {"start": 5, "end": 5}, {"start": 8, "end": 2},
    {"start": -1}, {"start": "soon"}, {"end": 0},
])
def test_a_span_that_holds_no_part_of_the_file_is_refused(monkeypatch, tmp_path, span):
    answer, ran = _strip_answer(monkeypatch, tmp_path, span)
    assert answer.startswith("Error: ") and not ran


def test_the_request_to_the_proxy_names_a_write(monkeypatch):
    """The judge sees every write as a write: the request carries it."""
    sent: list[dict] = []

    class _Answer:
        status_code = 200

        @staticmethod
        def json():
            return {"agents_relative": "ag/workspace/o.mp4", "agent": "ag"}

    def post(url, json, headers, timeout):
        sent.append(json)
        return _Answer()
    monkeypatch.setattr(shared.httpx, "post", post)
    monkeypatch.setattr(shared, "PROXY_URL", "http://proxy")
    shared.set_request_context("s1", "Bearer t")
    assert shared._resolve_via_proxy("/workspace/o.mp4", writing=True)[:2] == ("ag/workspace/o.mp4", "ag")
    assert shared._resolve_via_proxy("/workspace/o.mp4")[:2] == ("ag/workspace/o.mp4", "ag")
    assert sent == [{"session_id": "s1", "path": "/workspace/o.mp4", "writing": True},
                    {"session_id": "s1", "path": "/workspace/o.mp4", "writing": False}]


# ── Every output is asked as a write ─────────────────────────────────────


class _Stop(Exception):
    pass


def _recorder(seen, tmp_path):
    """Records ``(path, writing)``; a read answers a file in ``tmp_path``,
    a write stops the call once recorded."""
    def resolve(path, writing=False):
        seen.append((path, writing))
        if writing:
            raise _Stop(path)
        return str(tmp_path / Path(path).name)
    return resolve


@pytest.mark.parametrize(("handler", "args"), [
    ("render_still", {"html": "<p>x</p>", "output_path": "/workspace/s.png"}),
    ("render_motion_clip", {"html": "<p>x</p>", "duration": 1, "output_path": "/workspace/m.mp4"}),
])
def test_a_pages_output_is_asked_as_a_write(monkeypatch, tmp_path, handler, args):
    import motion
    seen: list = []
    monkeypatch.setattr(motion, "_resolve_path", _recorder(seen, tmp_path))
    with pytest.raises(_Stop):
        asyncio.run(getattr(motion, f"handle_{handler}")(args))
    assert (args["output_path"], True) in seen


def test_a_renders_and_a_filmstrips_output_are_asked_as_writes(monkeypatch, tmp_path):
    import analysis
    seen: list = []
    comp = tmp_path / "c.vproj.json"
    edit = comp_mod.new_composition()
    edit["tracks"][0]["clips"] = [{"src": "/workspace/v.mp4"}]
    comp_mod.save_composition(str(comp), edit)

    async def probe_paths(paths):
        return {p: {"duration": 10.0, "has_video": True, "has_audio": False,
                    "width": 1920, "height": 1080, "fps": 30.0, "color": {}} for p in paths}
    monkeypatch.setattr(renderer, "probe_media_paths", probe_paths)
    (tmp_path / "v.mp4").write_text("x")
    with pytest.raises(_Stop):
        asyncio.run(renderer.plan_render("/workspace/c.vproj.json", _recorder(seen, tmp_path),
                                         output_path="/workspace/r.mp4"))
    assert ("/workspace/r.mp4", True) in seen

    seen.clear()
    monkeypatch.setattr(analysis, "_resolve_path", _recorder(seen, tmp_path))

    async def probe(path):
        return {"streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
                "format": {"duration": "10"}}
    monkeypatch.setattr(analysis, "probe", probe)
    with pytest.raises(_Stop):
        asyncio.run(analysis.handle_render_filmstrip({"path": "/workspace/v.mp4", "output_path": "/workspace/f.jpg"}))
    assert ("/workspace/f.jpg", True) in seen


def test_an_output_whose_extension_changes_is_judged_again(monkeypatch, tmp_path):
    import motion
    seen: list = []

    def resolve(path, writing=False):
        seen.append((path, writing))
        if writing and path.endswith(".png"):
            raise _Stop(path)
        return str(tmp_path / Path(path).name)
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    with pytest.raises(_Stop):
        asyncio.run(motion.handle_render_still({"html": "<p>x</p>", "output_path": "/workspace/s.txt"}))
    assert seen[-1] == (str(tmp_path / "s.png"), True)


# ── Places the platform refused, and a computer's files ─────────────────


def test_a_refused_path_tells_nothing_about_whether_it_exists(tmp_path):
    """A refused path keeps its raw text, and the container sees every
    agent's folder: a refused file that exists and one that does not answer
    alike."""
    there = tmp_path / "there.mp4"
    there.write_text("x")
    gone = tmp_path / "gone.mp4"

    def deny(path, writing=False):
        raise ValueError("Cannot open: denied")

    def issues_for(src):
        comp = comp_mod.new_composition()
        comp["tracks"][0]["clips"] = [{"src": src, "in": 0, "out": 1}]
        _resolved, _info, issues = asyncio.run(renderer.prepare(comp, deny))
        return sorted(i["message"].replace(src, "<p>") for i in issues)
    assert issues_for(str(there)) == issues_for(str(gone))


def test_nothing_is_written_beside_a_computers_file(proxy, monkeypatch, tmp_path):
    import quickops
    # The platform would place this relative write in the session's folder:
    # the tool must not ask at all.
    proxy["answers"][".remote-host-cache/s1/clip.analysis.json"] = (
        "ag/workspace/.remote-host-cache/s1/clip.analysis.json", "ag")
    assert shared._write_beside("/agents/.remote-host-cache/s1/clip.analysis.json") is None
    assert not [s for s in proxy["sent"] if s[0].startswith(".remote-host-cache")]
    src = tmp_path / "clip.mp4"
    src.write_text("x")
    monkeypatch.setattr(quickops, "_resolve_path", lambda path, writing=False: str(src))
    monkeypatch.setattr(quickops, "_on_a_computer", lambda path: True)
    answer = asyncio.run(quickops.handle_edit_video({"path": "/home/u/clip.mp4", "operations": [{"op": "trim"}]}))
    assert answer.startswith("Error: this video is on a computer") and "output_path" in answer


# ── Bounds: the filmstrip, the budget, a stuck read, a stuck job ─────────


def test_a_filmstrip_keeps_one_thread_per_frame_and_fewer_frames_above_1080p(monkeypatch, tmp_path):
    import analysis
    ran: list = []

    async def probe(path):
        return {"streams": [{"codec_type": "video", "width": 3840, "height": 2160}],
                "format": {"duration": "60"}}

    async def run(args, **kw):
        ran.append((args, kw))
    monkeypatch.setattr(analysis, "_resolve_path", lambda path, writing=False: str(tmp_path / Path(path).name))
    monkeypatch.setattr(analysis, "probe", probe)
    monkeypatch.setattr(analysis, "run_ffmpeg", run)
    answer = json.loads(asyncio.run(analysis.handle_render_filmstrip(
        {"path": "/workspace/v.mp4", "count": 60, "output_path": "/workspace/f.jpg"})))
    args, kw = ran[0]
    assert answer["count"] == analysis.FILMSTRIP_MAX_FRAMES_BIG
    assert args.count("-i") == analysis.FILMSTRIP_MAX_FRAMES_BIG
    assert args.count("-threads") == args.count("-i")
    assert kw["heavy"] is True


# ── Heavy runs share the memory ─────────────────────────────────────────


@pytest.fixture
def gate(monkeypatch):
    """A fresh gate over a 1,000 MB budget and two runs at once."""
    import fftools
    monkeypatch.setenv("VIDEO_TOOLS_RENDER_BUDGET_MB", "1000")
    monkeypatch.setattr(fftools, "MAX_RENDERS", 2)
    monkeypatch.setattr(fftools, "_gate", fftools._Gate())
    yield fftools
    assert fftools._gate.held == 0 and fftools._gate.claimed == 0 and not fftools._gate.line


def _runs(fftools, claims, *, hold=0.05):
    """Run one heavy step per claim, each started a moment after the one
    before; answers each step's (start, end) and the order they started in."""
    loop_t = []
    order = []

    async def step(i, claim):
        await asyncio.sleep(0.005 * i)
        async with fftools.heavy_run(claim):
            order.append(i)
            t0 = asyncio.get_running_loop().time()
            await asyncio.sleep(hold)
            loop_t.append((i, t0, asyncio.get_running_loop().time()))

    async def main():
        await asyncio.gather(*(step(i, c) for i, c in enumerate(claims)))
    asyncio.run(main())
    spans = {i: (a, b) for i, a, b in loop_t}
    return [spans[i] for i in range(len(claims))], order


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def test_two_claims_over_the_budget_never_run_together(gate):
    (a, b), _ = _runs(gate, [800e6, 800e6])
    assert not _overlap(a, b)


def test_two_claims_within_the_budget_run_together(gate):
    (a, b), _ = _runs(gate, [400e6, 500e6])
    assert _overlap(a, b)


def test_a_claim_over_the_budget_runs_alone(gate):
    (big, small), _ = _runs(gate, [5000e6, 1e6])
    assert not _overlap(big, small)


def test_a_waiter_is_never_passed_by_a_later_one(gate):
    """A small claim that would fit beside the running one still waits behind
    the big one that asked first: a big render is never starved."""
    _, order = _runs(gate, [800e6, 800e6, 100e6])
    assert order == [0, 1, 2]


def test_plain_runs_keep_two_at_once(gate):
    (a, b, c), _ = _runs(gate, [None, None, None])
    assert _overlap(a, b)
    assert c[0] >= min(a[1], b[1])


@pytest.mark.parametrize("budget", [1000e6, 8047452611.379834])
def test_three_plain_runs_go_together_when_three_are_allowed(monkeypatch, budget):
    """Three equal shares sum a few millionths of a byte past some budgets in
    floating point (the second one here): the third share still fits."""
    import fftools
    monkeypatch.setattr(fftools, "MAX_RENDERS", 3)
    monkeypatch.setattr(fftools, "render_budget_bytes", lambda: budget)
    monkeypatch.setattr(fftools, "_gate", fftools._Gate())
    (a, b, c), _ = _runs(fftools, [None, None, None])
    assert _overlap(a, c) and _overlap(b, c)


def test_a_cancelled_waiter_and_a_failed_run_leave_the_gate_empty(gate):
    async def main():
        hold = asyncio.Event()

        async def first():
            async with gate.heavy_run(900e6):
                await hold.wait()

        async def failing():
            async with gate.heavy_run(900e6):
                raise RuntimeError("ffmpeg failed")

        a = asyncio.create_task(first())
        await asyncio.sleep(0.01)
        waiter = asyncio.create_task(gate.heavy_run(900e6).__aenter__())
        await asyncio.sleep(0.01)
        assert len(gate._gate.line) == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not gate._gate.line
        hold.set()
        await a
        with pytest.raises(RuntimeError):
            await failing()
    asyncio.run(main())


def test_a_run_the_system_ends_for_memory_says_what_to_do(monkeypatch, tmp_path):
    import fftools
    killed = tmp_path / "ffmpeg"
    killed.write_text("#!/bin/sh\nkill -9 $$\n")
    killed.chmod(0o755)
    monkeypatch.setattr(fftools, "FFMPEG", str(killed))
    with pytest.raises(fftools.FFmpegError) as err:
        asyncio.run(fftools.run_ffmpeg(["-version"]))
    assert str(err.value) == fftools.OUT_OF_MEMORY


def test_a_stuck_read_in_colour_matching_ends(monkeypatch):
    import colormatch

    def stuck(*a, **kw):
        assert kw.get("timeout")
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])
    monkeypatch.setattr(subprocess, "run", stuck)
    with pytest.raises(ValueError, match="frame size"):
        colormatch._frame_size("/x.mp4")


def test_a_job_past_its_ceiling_fails_and_frees_the_tool(monkeypatch, tmp_path):
    jobs.reset_for_tests()
    monkeypatch.setattr(jobs, "JOB_MAX_S", 0.2)
    monkeypatch.setattr(jobs, "_resolve_path", lambda path, writing=False: str(tmp_path / "out.mp4"))

    async def endless(plan, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(renderer, "run_render", endless)
    monkeypatch.setattr(renderer, "discard", lambda plan: None)
    plan = type("P", (), {"out": str(tmp_path / "out.mp4")})()

    async def main():
        job = await jobs.start(plan, lambda result: "")
        await asyncio.sleep(0.6)
        return job
    job = asyncio.run(main())
    assert job.state == "failed" and job.error == jobs.TOO_LONG
    assert not jobs.busy()


def test_a_finished_file_not_sent_on_says_so(monkeypatch, tmp_path):
    jobs.reset_for_tests()
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(tmp_path))
    out = str(tmp_path / "ag" / "workspace" / "out.mp4")
    monkeypatch.setattr(jobs, "_resolve_path", lambda path, writing=False: out)

    async def done(plan, **kw):
        return {"output": out}

    async def not_sent(path):
        return False
    monkeypatch.setattr(renderer, "run_render", done)
    monkeypatch.setattr(jobs, "_notify_file_written", not_sent)
    plan = type("P", (), {"out": out})()

    async def main():
        job = await jobs.start(plan, lambda result: "Rendered")
        await job.task
        return job
    job = asyncio.run(main())
    answer = jobs.status(job.id)
    assert answer["state"] == "done" and answer["note"] == jobs.NOT_SENT


@needs_ffmpeg
def test_a_cancel_ends_ffmpeg(tmp_path):
    """A cancelled encode is killed, not left to run: the process is gone
    right after the cancel."""
    import fftools
    started: list = []
    real = asyncio.create_subprocess_exec

    async def spawn(*cmd, **kw):
        proc = await real(*cmd, **kw)
        started.append(proc)
        return proc

    async def main():
        fftools.asyncio.create_subprocess_exec = spawn
        try:
            task = asyncio.create_task(fftools.run_ffmpeg(
                ["-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=60",
                 "-f", "null", "-"], heavy=False))
            while not started:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            assert started[0].returncode is None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert started[0].returncode is not None
        finally:
            fftools.asyncio.create_subprocess_exec = real
    asyncio.run(main())


def test_the_browser_sends_a_pages_local_files_through_the_guard(monkeypatch, tmp_path):
    """A real page: its local image is asked of the session before the
    browser may load it, and one the session may not read is refused."""
    from test_motion import HAVE_CHROMIUM
    if not (HAVE_FFMPEG and HAVE_CHROMIUM):
        pytest.skip("ffmpeg or playwright chromium not available")
    import motion
    secret = tmp_path / "elsewhere" / "secret.png"
    secret.parent.mkdir()
    secret.write_bytes(b"\x89PNG\r\n\x1a\n")
    asked: list = []

    def resolve(path, writing=False):
        asked.append((path, writing))
        if writing:
            return str(tmp_path / "s.png")
        raise ValueError("denied")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    html = f'<body style="margin:0"><img src="file://{secret}" width="32" height="32"></body>'
    asyncio.run(motion.handle_render_still({"html": html, "width": 64, "height": 64,
                                            "output_path": "/workspace/s.png"}))
    assert (str(secret), False) in asked


# ── A composition carries no renderer key ────────────────────────────────


def test_the_renderers_own_keys_never_come_from_a_composition(tmp_path):
    """``_slomo``, ``_stab`` and ``_match`` name files the renderer staged
    itself: a stored composition that carries them is refused, and the
    resolved copy holds none of them, so nothing unjudged reaches ffmpeg."""
    source = tmp_path / "clip.mp4"
    source.write_text("x")
    elsewhere = str(tmp_path / "elsewhere.mp4")
    comp = comp_mod.new_composition()
    comp["tracks"][0]["clips"] = [{"src": "/workspace/clip.mp4",
                                   "_slomo": {"src": elsewhere},
                                   "_stab": {"trf": elsewhere},
                                   "_match": {"cube": elsewhere}}]
    judged: list = []

    def resolve(path, writing=False):
        judged.append(path)
        return str(source)
    resolved, _info, issues = asyncio.run(renderer.prepare(comp, resolve))
    clip = resolved["tracks"][0]["clips"][0]
    assert not [k for k in clip if k.startswith("_")]
    assert clip["src"] == str(source) and judged == ["/workspace/clip.mp4"]
    errors = [i for i in issues if i["level"] == "error"]
    assert any("_match" in i["message"] and "_slomo" in i["message"] and "_stab" in i["message"]
               for i in errors)


def test_the_validator_refuses_a_key_with_an_underscore():
    comp = comp_mod.new_composition()
    comp["tracks"][0]["clips"] = [{"fill": "#000000", "duration": 1, "_stab": {}}]
    issues = comp_mod.validate(comp)
    assert any(i["level"] == "error" and "_stab" in i["message"] for i in issues)
