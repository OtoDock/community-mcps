"""Every path at the proxy, writes as writes; a render in the background with
its status and its cancel; never a half file at the output's name; the busy
answer; the filmstrip; the timeline as data; update_clip's flat form."""

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

import composition as comp_mod
import jobs
import renderer
import shared
from conftest import HAVE_FFMPEG, REAL_READ_BESIDE, REAL_WRITE_BESIDE, same_path
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

    async def resolve(path, writing=False):
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


def _resolve(path, writing=False):
    """One request's resolve, from a sync test."""
    return asyncio.run(shared._resolve_path(path, writing=writing))


def test_a_container_path_goes_to_the_proxy_like_every_form(proxy):
    proxy["answers"]["ag/workspace/a.mp4"] = ("ag/workspace/a.mp4", "ag")
    assert _resolve("/agents/ag/workspace/a.mp4") == "/agents/ag/workspace/a.mp4"
    with pytest.raises(ValueError, match="denied"):
        _resolve("/agents/other/users/u/private.mp4")
    assert proxy["sent"] == [("ag/workspace/a.mp4", False), ("other/users/u/private.mp4", False)]


def test_an_answer_is_held_to_the_sessions_own_tree(proxy):
    proxy["answers"]["/workspace/x.mp4"] = ("other/workspace/x.mp4", "ag")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        _resolve("/workspace/x.mp4")
    # A sibling agent whose name starts with the session's agent's name.
    proxy["answers"]["/workspace/z.mp4"] = ("ag-other/workspace/z.mp4", "ag")
    with pytest.raises(ValueError, match="leaves this session's agent tree"):
        _resolve("/workspace/z.mp4")
    proxy["answers"]["/workspace/y.mp4"] = ("ag/workspace/y.mp4", "")
    with pytest.raises(ValueError, match="without the session's agent"):
        _resolve("/workspace/y.mp4")


def test_a_write_is_asked_as_a_write_and_its_printed_form_is_mapped(proxy):
    proxy["answers"]["/workspace/in.mp4"] = ("ag/workspace/in.mp4", "ag")
    proxy["answers"]["/workspace/out.mp4"] = ("ag/workspace/out.mp4", "ag")

    async def one_request():
        await shared._resolve_path("/workspace/in.mp4")
        return await shared._resolve_path("ag/workspace/out.mp4", writing=True)
    assert asyncio.run(one_request()) == "/agents/ag/workspace/out.mp4"
    # The agent came from the request's earlier answer: no extra question.
    assert proxy["sent"] == [("/workspace/in.mp4", False), ("/workspace/out.mp4", True)]


def test_a_write_in_the_printed_form_learns_the_agent_first(proxy):
    """A fresh request whose first path is a write the tool printed
    (``<agent>/workspace/...``): the agent is learned from a read answer,
    then the write goes in the session's own form."""
    proxy["answers"]["/workspace/out.mp4"] = ("ag/workspace/out.mp4", "ag")
    assert _resolve("ag/workspace/out.mp4", writing=True) == "/agents/ag/workspace/out.mp4"
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


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_every_path_a_tool_echoes_for_a_file_it_wrote_is_taken_back_as_a_write(proxy, monkeypatch, tmp_path):
    """The path a tool prints for the file it wrote (a composition, a
    render, an edit, a filmstrip, an analysis sidecar and its contact sheet)
    is a path the tools take as an output in a later request: it is judged
    as a write and lands on the very same file."""
    import analysis
    import project
    import quickops
    agents = tmp_path / "agents"
    workspace = agents / "ag" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    for name in ("c.vproj.json", "clip.mp4", "c.preview.mp4", "clip_edited.mp4",
                 "clip.filmstrip-2x45.jpg", "clip.analysis.json", "clip.shots.png"):
        proxy["answers"][f"/workspace/{name}"] = (f"ag/workspace/{name}", "ag")
        proxy["answers"][f"ag/workspace/{name}"] = (f"ag/workspace/{name}", "ag")
    _clip(workspace / "clip.mp4", seconds=1)

    def run(handler, args):
        answer = asyncio.run(handler(args))               # one request each
        if isinstance(answer, list):
            answer = "\n".join(c.text for c in answer if getattr(c, "type", "") == "text")
        assert not answer.startswith("Error"), answer
        return answer

    edit = {"project": {"width": 320, "height": 180, "fps": 25},
            "tracks": [{"kind": "video", "clips": [{"src": "/workspace/clip.mp4"}]}]}
    created = run(project.handle_create_composition,
                  {"path": "/workspace/c.vproj.json", "composition": edit})
    rendered = run(project.handle_render_composition, {"path": "/workspace/c.vproj.json"})
    edited = run(quickops.handle_edit_video, {"path": "/workspace/clip.mp4",
                                              "operations": [{"op": "trim", "start": 0, "end": 0.5}]})
    strip = run(analysis.handle_render_filmstrip, {"path": "/workspace/clip.mp4", "count": 2, "height": 45})
    analysed = run(analysis.handle_analyze_video, {"path": "/workspace/clip.mp4"})

    echoes = {
        "created": created.split("\n", 1)[0].removeprefix("Created "),
        "rendered": rendered.split("\n", 1)[0].removeprefix("Rendered (preview): "),
        "edited": next(line for line in edited.splitlines()
                       if line.startswith("saved: "))[len("saved: "):].split(" (", 1)[0],
        "strip": json.loads(strip)["output"],
        "sidecar": analysed.split("sidecar: ", 1)[1].split(" · ", 1)[0],
        "sheet": analysed.split("contact sheet: ", 1)[1].split("\n", 1)[0],
    }
    assert echoes == {
        "created": "ag/workspace/c.vproj.json", "rendered": "ag/workspace/c.preview.mp4",
        "edited": "ag/workspace/clip_edited.mp4", "strip": "ag/workspace/clip.filmstrip-2x45.jpg",
        "sidecar": "ag/workspace/clip.analysis.json", "sheet": "ag/workspace/clip.shots.png"}
    for echo in echoes.values():
        written = agents / echo
        assert written.is_file(), echo
        # A later request names it as its output: judged as a write, in the
        # session's own form, and answered with the same file.
        assert _resolve(echo, writing=True) == str(written)

    # And the tools take them as their outputs, and write the same files.
    assert run(project.handle_create_composition,
               {"path": echoes["created"], "composition": edit, "create_new": True}
               ).startswith(f"Created {echoes['created']}")
    assert run(project.handle_render_composition,
               {"path": echoes["created"], "output_path": echoes["rendered"]}
               ).startswith(f"Rendered (preview): {echoes['rendered']}")
    assert f"saved: {echoes['edited']} (" in run(
        quickops.handle_edit_video, {"path": "/workspace/clip.mp4", "output_path": echoes["edited"],
                                     "operations": [{"op": "trim", "start": 0, "end": 0.5}]})
    assert json.loads(run(analysis.handle_render_filmstrip,
                          {"path": "/workspace/clip.mp4", "count": 2, "height": 45,
                           "output_path": echoes["strip"]}))["output"] == echoes["strip"]
    assert sorted(p.name for p in workspace.iterdir()) == [
        "c.preview.mp4", "c.vproj.json", "clip.analysis.json", "clip.filmstrip-2x45.jpg",
        "clip.mp4", "clip.shots.png", "clip_edited.mp4"]


def test_a_link_at_a_name_that_differs_only_in_its_normal_form_is_confined(proxy, monkeypatch, tmp_path):
    """The name is matched first, and the answer is opened beneath the mount
    with no link followed: a decomposed sibling that leads out of the tree
    is refused when it is opened, never followed."""
    import unicodedata
    agents = tmp_path / "agents"
    (agents / "ag" / "workspace").mkdir(parents=True)
    (agents / "other" / "workspace").mkdir(parents=True)
    (agents / "other" / "workspace" / "secret.mp4").write_text("secret")
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    composed = "caf\u00e9.mp4"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    (agents / "ag" / "workspace" / decomposed).symlink_to(agents / "other" / "workspace" / "secret.mp4")
    proxy["answers"][f"/workspace/{composed}"] = (f"ag/workspace/{composed}", "ag")
    linked = _resolve(f"/workspace/{composed}")
    assert linked == str(agents / "ag" / "workspace" / decomposed)
    assert shared.safe_kind(linked).kind == "other" and not shared.safe_is_file(linked)
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(linked)
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_read_fd(linked)
    # A plain decomposed name inside the tree still matches, and reads.
    (agents / "ag" / "workspace" / decomposed).unlink()
    (agents / "ag" / "workspace" / decomposed).write_text("x")
    plain = _resolve(f"/workspace/{composed}")
    assert plain == str(agents / "ag" / "workspace" / decomposed)
    assert shared.safe_read_bytes(plain, 16) == b"x"


def test_a_matched_name_that_is_a_link_inside_the_agent_is_refused(proxy, monkeypatch, tmp_path):
    """The platform judged the name it answered, with every link already
    followed. A sibling that differs only in its normal form and is a link
    leads to a place nobody judged, even inside the same agent: its open
    is refused."""
    import unicodedata
    agents = tmp_path / "agents"
    (agents / "ag" / "workspace").mkdir(parents=True)
    (agents / "ag" / "users" / "olga").mkdir(parents=True)
    (agents / "ag" / "users" / "olga" / "private.mp4").write_text("x")
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    composed = "caf\u00e9.mp4"
    decomposed = unicodedata.normalize("NFD", composed)
    (agents / "ag" / "workspace" / decomposed).symlink_to(agents / "ag" / "users" / "olga" / "private.mp4")
    proxy["answers"][f"/workspace/{composed}"] = (f"ag/workspace/{composed}", "ag")
    with pytest.raises(PermissionError, match="Cannot read"):
        shared.safe_open_read(_resolve(f"/workspace/{composed}"))
    # A link already at a parent folder of a new output is refused the same
    # way: nothing is written through it.
    (agents / "ag" / "workspace" / "out").symlink_to(agents / "ag" / "users" / "olga")
    proxy["answers"]["/workspace/out/new.mp4"] = ("ag/workspace/out/new.mp4", "ag")
    out = _resolve("/workspace/out/new.mp4", writing=True)
    with pytest.raises(PermissionError, match="Cannot write"):
        shared.safe_write_bytes(out, b"x")
    assert sorted(p.name for p in (agents / "ag" / "users" / "olga").iterdir()) == ["private.mp4"]


# ── Every file opened beneath the mount, no link followed ───────────────


def test_a_link_is_refused_where_the_file_is_opened(monkeypatch, tmp_path):
    """A name that is a link, and a name under a folder that is a link, are
    refused when they are read, by the helpers and by ffmpeg's door; an
    output under a linked folder is refused, and nothing lands where the
    link leads."""
    import fftools
    mount = tmp_path / "mount"
    (mount / "ws").mkdir(parents=True)
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.mp4").write_bytes(b"secret")
    linked_name = mount / "ws" / "a.mp4"
    linked_name.symlink_to(elsewhere / "secret.mp4")
    (mount / "ws" / "out").symlink_to(elsewhere)
    under_linked_folder = mount / "ws" / "out" / "secret.mp4"

    for name in (linked_name, under_linked_folder):
        with pytest.raises(PermissionError, match="Cannot read"):
            shared.safe_open_read(str(name))
        with pytest.raises(PermissionError, match="Cannot read"):
            shared.safe_read_fd(str(name))
        with pytest.raises(PermissionError, match="Cannot read"):
            with fftools.bound_files(["ffmpeg", "-i", str(name), "-f", "null", "-"]):
                pass
    with pytest.raises(PermissionError, match="Cannot read"):
        with fftools.bound_files(["ffprobe", str(linked_name)], last_is_input=True):
            pass

    with pytest.raises(PermissionError, match="Cannot write"):
        shared.safe_write_bytes(str(mount / "ws" / "out" / "new.json"), b"{}")
    with pytest.raises(PermissionError, match="Cannot write"):
        with fftools.bound_files(["ffmpeg", "-i", "-",
                                  str(mount / "ws" / "out" / "new.mp4")]):
            pass
    assert sorted(p.name for p in elsewhere.iterdir()) == ["secret.mp4"]


def test_the_door_refuses_a_mount_file_that_is_neither_an_input_nor_the_output(monkeypatch, tmp_path):
    """Only an ``-i`` value and the last argument may name the mount: any
    other argument that does is refused before a child runs. An ``-i``
    value outside the mount stays as written."""
    import re

    import fftools
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    (mount / "graph.txt").write_text("[0:v]null[v]")
    spawned: list = []

    async def spawn(*cmd, **kw):
        spawned.append(cmd)
        raise AssertionError("a child ran")
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", spawn)
    refused = re.escape(fftools.MOUNT_REFUSED)
    with pytest.raises(fftools.FFmpegError, match=refused):
        asyncio.run(fftools.run_ffmpeg(
            ["-i", "-",
             "-filter_complex_script", str(mount / "graph.txt"),
             "-map", "[v]", "-f", "null", "-"], heavy=False))
    with pytest.raises(fftools.FFmpegError, match=refused):
        asyncio.run(fftools.run_ffmpeg(
            ["-i", "-",
             "-vf", f"subtitles={mount}/s.ass", "-f", "null", "-"], heavy=False))
    assert spawned == []


def test_outside_the_mount_only_the_tools_own_folders_open(monkeypatch, tmp_path):
    """Outside the mount a file opens only in the tool's own folders: a temp
    folder of its own and a folder a module names (the looks). Any other
    path is refused with a sentence, by every reader and by ffmpeg's door,
    and so is a link in an own folder."""
    import tempfile

    import color
    import fftools
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    elsewhere = tmp_path / "elsewhere.mp4"           # neither the mount nor the tool's
    elsewhere.write_bytes(b"elsewhere")
    own = Path(tempfile.mkdtemp(prefix="vt-test-"))
    try:
        (own / "frame.png").write_bytes(b"own")
        (own / "link.png").symlink_to(elsewhere)
        refused = "outside the agents tree and the tool's own folders"
        copy = lambda p: shared.private_copy(p, str(own), 1024)   # noqa: E731
        for reader in (shared.safe_open_read, shared.safe_read_fd, shared.safe_kind, copy):
            with pytest.raises(PermissionError, match=refused):
                reader(str(elsewhere))
        for reader in (shared.safe_open_read, shared.safe_read_fd, copy):
            with pytest.raises(PermissionError, match="Cannot read"):
                reader(str(own / "link.png"))
        assert shared.safe_kind(str(own / "link.png")).kind == "other"
        assert not shared.safe_is_file(str(elsewhere))
        with pytest.raises(PermissionError, match=refused):
            with fftools.bound_files(["ffmpeg", "-i", str(elsewhere), "-f", "null", "-"]):
                pass
        with pytest.raises(PermissionError, match=refused):
            with fftools.bound_files(["ffprobe", str(elsewhere)], last_is_input=True):
                pass
        with pytest.raises(PermissionError, match=refused):
            with fftools.bound_files(["ffmpeg", "-i", f"{own}/../{elsewhere.name}", "-f", "null", "-"]):
                pass
        assert sorted(p.name for p in own.iterdir()) == ["frame.png", "link.png"]

        # The tool's own: its temp folder, a look of the image's, its pipe.
        with shared.safe_open_read(str(own / "frame.png")) as fh:
            assert fh.read() == b"own"
        look = Path(color.LOOKS_DIR) / f"{next(iter(color.BUILTIN_LOOKS))}.cube"
        assert shared.safe_kind(str(look)).kind == "file"
        for argv in (["ffmpeg", "-i", str(own / "f%05d.png"), "-f", "null", "-"],
                     ["ffmpeg", "-i", "-", "-f", "null", "-"]):
            with fftools.bound_files(argv) as (bound, fds):
                assert bound == argv and fds == []
        # A generated source can open files of its own: the door refuses it.
        for source in ("testsrc2=duration=1", f"movie={elsewhere}"):
            with pytest.raises(PermissionError, match=refused):
                with fftools.bound_files(["ffmpeg", "-f", "lavfi", "-i", source, "-f", "null", "-"]):
                    pass
    finally:
        shutil.rmtree(own, ignore_errors=True)


def test_outside_the_mount_only_the_tools_own_folders_are_written(monkeypatch, tmp_path):
    """A finished file lands beneath the mount only, and ffmpeg's door holds
    an output outside the mount to the tool's own folders and its pipe, as
    it holds an input. Anything else, a mount path with a dot part
    included, is refused before anything is written."""
    import tempfile

    import fftools
    mount = tmp_path / "mount"
    (mount / "ws").mkdir(parents=True)
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    own = Path(tempfile.mkdtemp(prefix="vt-test-"))
    try:
        made = own / "made.mp4"
        made.write_bytes(b"made")
        refused = "outside the agents tree and the tool's own folders"
        with pytest.raises(PermissionError, match="Cannot write"):
            shared.place_copy(str(made), str(elsewhere / "out.mp4"))
        for target in (str(elsewhere / "out.mp4"), f"{mount}/ws/../ws/out.mp4"):
            with pytest.raises(PermissionError, match=refused):
                with fftools.bound_files(["ffmpeg", "-i", "-", target]):
                    pass
        assert list(elsewhere.iterdir()) == [] and list((mount / "ws").iterdir()) == []
        for target in (str(own / "seg.mp4"), "-", "pipe:1"):
            argv = ["ffmpeg", "-i", "-", target]
            with fftools.bound_files(argv) as (bound, fds):
                assert bound == argv and fds == []
        shared.place_copy(str(made), str(mount / "ws" / "out.mp4"))
        assert (mount / "ws" / "out.mp4").read_bytes() == b"made"
    finally:
        shutil.rmtree(own, ignore_errors=True)

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_file_changed_between_its_check_and_its_use_is_refused(tmp_path):
    """Within one call the encode opens the file the probe checked: another
    file put at that name in between is refused with one sentence, and so is
    a source changed after the slow-motion key read it. A file written again
    in place, to another length or to the same one, is refused too. A file
    the tool wrote itself is read as it now is."""
    import contextvars

    import fftools
    import slowmo
    clip, other = tmp_path / "clip.mp4", tmp_path / "other.mp4"
    _clip(clip, seconds=1)
    _clip(other, seconds=2)

    def one_call():
        shared.begin_call()
        asyncio.run(fftools.probe(str(clip)))
        slowmo.cache_key(str(other), (0.0, 1.0), 0.5, 30.0, None)
        for name in (clip, other):
            replacement = tmp_path / f"new-{name.name}"
            shutil.copyfile(clip if name == other else other, replacement)
            os.replace(replacement, name)
            with pytest.raises(PermissionError, match="changed after this call checked it"):
                with fftools.bound_files(["ffmpeg", "-i", str(name), "-f", "null", "-"]):
                    pass
        with shared.safe_open_write(str(clip)) as fh:
            fh.write(other.read_bytes())
        with fftools.bound_files(["ffmpeg", "-i", str(clip), "-f", "null", "-"]) as (_, fds):
            assert len(fds) == 1
    contextvars.copy_context().run(one_call)

    longer, same = tmp_path / "longer.mp4", tmp_path / "same.mp4"
    _clip(longer, seconds=1)
    _clip(same, seconds=1)

    def in_place():
        shared.begin_call()
        for name in (longer, same):
            asyncio.run(fftools.probe(str(name)))
        with open(longer, "ab") as fh:
            fh.write(b"\0" * 16)
        checked = os.stat(same)
        with open(same, "r+b") as fh:
            fh.write(b"\0" * 8)
        # The kernel stamps a change from a coarse clock: write until it moves.
        while os.stat(same).st_ctime_ns == checked.st_ctime_ns:
            time.sleep(0.005)
            with open(same, "r+b") as fh:
                fh.write(b"\0" * 8)
        assert os.stat(same)[1:2] == checked[1:2] and os.stat(same).st_size == checked.st_size
        for name in (longer, same):
            with pytest.raises(PermissionError, match="changed after this call checked it"):
                with fftools.bound_files(["ffmpeg", "-i", str(name), "-f", "null", "-"]):
                    pass
    contextvars.copy_context().run(in_place)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_tool_call_starts_the_record_that_refuses_a_changed_file(proxy, monkeypatch, tmp_path):
    """The server's own dispatch starts the call's record: an edit whose
    source is replaced after its probe is refused with one sentence, and
    writes nothing."""
    import quickops
    import server
    agents = tmp_path / "agents"
    workspace = agents / "ag" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    for name in ("clip.mp4", "clip_edited.mp4"):
        proxy["answers"][f"/workspace/{name}"] = (f"ag/workspace/{name}", "ag")
    _clip(workspace / "clip.mp4", seconds=1)
    real_probe = quickops.probe
    swapped: list[str] = []

    async def probe_then_swap(path):
        info = await real_probe(path)
        if not swapped:
            swapped.append(path)
            _clip(workspace / "new.mp4", seconds=2)
            os.replace(workspace / "new.mp4", workspace / "clip.mp4")
        return info
    monkeypatch.setattr(quickops, "probe", probe_then_swap)

    answer = asyncio.run(server.call_tool("edit_video", {
        "path": "/workspace/clip.mp4", "operations": [{"op": "trim", "start": 0, "end": 0.5}]}))
    text = "\n".join(c.text for c in answer)
    assert swapped and "changed after this call checked it" in text, text
    assert sorted(p.name for p in workspace.iterdir()) == ["clip.mp4"]

def test_a_running_tool_reads_no_switch_for_its_mount(monkeypatch, tmp_path):
    """The suite's switch points the mount at a temporary tree, and the
    server's start pins the mount: after it the switch changes nothing."""
    import ast
    monkeypatch.setattr(shared, "_root_fixed", False)
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(tmp_path))
    assert shared._write_root() == str(tmp_path)
    shared.fix_write_root()
    assert shared._write_root() == shared.MOUNT_AGENTS_DIR
    tree = ast.parse((Path(__file__).parent.parent / "server.py").read_text())
    main = next(node for node in tree.body if isinstance(node, ast.If)
                and "__main__" in ast.unparse(node.test))
    first = main.body[0]
    assert isinstance(first, ast.Expr) and ast.unparse(first.value) == "fix_write_root()"

def test_opencv_reads_through_its_ffmpeg_reader_alone():
    """Every module that captures leaves OpenCV its FFmpeg reader alone
    before the first capture, the scene detector's own opens included: the
    registry of a fresh process that imported them lists no other."""
    import sys
    code = ("import analysis, quickops, reframe, track\n"
            "from cv2 import videoio_registry as r\n"
            "print(','.join(r.getBackendName(b) for b in r.getStreamBackends()))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent.parent),
                         capture_output=True, text=True, timeout=120, env=dict(os.environ))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "FFMPEG"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_capture_opens_a_clip_and_not_a_picture_kind_the_list_leaves_out(tmp_path):
    """A capture opens a clip, and a picture of a kind the format list
    leaves out is not opened."""
    import cv2
    import numpy as np
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=1)
    raster = tmp_path / "frame.ras"
    assert cv2.imwrite(str(raster), np.zeros((16, 16, 3), np.uint8))
    for name, opens in ((clip, True), (raster, False)):
        cap = shared.open_capture(str(name))
        try:
            assert cap.isOpened() is opens, name.name
        finally:
            cap.release()

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_capture_reads_under_the_lists_and_opens_no_playlist(tmp_path):
    """OpenCV reads a file under the door's two lists: a playlist beneath
    the mount that names a clip is not opened."""
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=1)
    playlist = tmp_path / "list.mp4"
    playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                        f"#EXTINF:1.0,\n{clip}\n#EXT-X-ENDLIST\n")
    cap = shared.open_capture(str(playlist))
    try:
        assert not cap.isOpened()
    finally:
        cap.release()


def test_the_door_refuses_a_link_where_the_safe_open_is_missing(monkeypatch, tmp_path):
    """On a kernel with no safe open the door walks each part of the name
    with no link followed: a link, in the name or on the way, is refused,
    and a plain file opens."""
    import fftools
    import safe_fs
    monkeypatch.setattr(safe_fs, "_openat2_ok", False)
    assert not safe_fs.openat2_available()
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(b"plain")
    (tmp_path / "link.mp4").symlink_to(plain)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inside.mp4").write_bytes(b"inside")
    (tmp_path / "way").symlink_to(tmp_path / "real")
    for refused in (tmp_path / "link.mp4", tmp_path / "way" / "inside.mp4"):
        with pytest.raises(PermissionError, match="Cannot read"):
            with fftools.bound_files(["ffmpeg", "-i", str(refused), "-f", "null", "-"]):
                pass
    with fftools.bound_files(["ffmpeg", "-i", str(plain), "-f", "null", "-"]) as (_, fds):
        assert len(fds) == 1

def test_safe_fs_is_the_platforms_own_copy_byte_for_byte():
    """``safe_fs.py`` is the platform's ``proxy/services/infra/safe_fs.py``
    copied byte for byte: pinned by its hash, and compared with a checkout
    of the platform beside this repository when there is one."""
    import hashlib
    here = Path(__file__).resolve().parents[1] / "safe_fs.py"
    assert hashlib.sha256(here.read_bytes()).hexdigest() == \
        "c0ede48b6c28a23c9cae4aade72c068c6c83981b5d716a5be71c8beb30e99c53"
    platform = Path(__file__).resolve().parents[2].parent / "oto-dock" / "proxy" / "services" / "infra" / "safe_fs.py"
    if not platform.is_file():
        pytest.skip("no checkout of the platform beside this repository to compare with")
    assert here.read_bytes() == platform.read_bytes()


def test_a_file_beside_a_source_is_written_only_where_the_session_may(proxy):
    proxy["answers"]["/workspace/a.analysis.json"] = ("ag/workspace/a.analysis.json", "ag")
    assert asyncio.run(shared._write_beside("/agents/ag/workspace/a.analysis.json")) \
        == "/agents/ag/workspace/a.analysis.json"
    assert proxy["sent"][-1] == ("/workspace/a.analysis.json", True)
    assert asyncio.run(shared._write_beside("/agents/ag/knowledge/a.analysis.json")) is None


def test_a_file_beside_a_source_is_read_only_where_the_session_may(proxy):
    """A stabilization cache is asked as a read, like any file the session
    names. Refused, it is a miss. Beside a computer's file nothing is asked."""
    proxy["answers"]["ag/workspace/a.stab-1.trf"] = ("ag/workspace/a.stab-1.trf", "ag")
    assert asyncio.run(shared._read_beside("/agents/ag/workspace/a.stab-1.trf")) \
        == "/agents/ag/workspace/a.stab-1.trf"
    assert proxy["sent"][-1] == ("ag/workspace/a.stab-1.trf", False)
    assert asyncio.run(shared._read_beside("/agents/ag/users/olga/a.stab-1.trf")) is None
    sent = len(proxy["sent"])
    assert asyncio.run(shared._read_beside("/agents/.remote-host-cache/s1/a.stab-1.trf")) is None
    assert len(proxy["sent"]) == sent


def test_every_write_site_asks_as_a_write(monkeypatch, tmp_path):
    import project
    import quickops
    seen: list[tuple[str, bool]] = []

    async def record(path, writing=False):
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
        self.body = None

    async def continue_(self):
        self.verdict = "load"

    async def abort(self):
        self.verdict = "refused"

    async def fulfill(self, *, status=200, body=b"", content_type=""):
        self.verdict, self.body = "served", body


def test_a_pages_files_load_only_from_the_call_or_where_the_session_reads(monkeypatch, tmp_path):
    """The page is the call's own document; a name under the page's origin
    is served only from a file the session may read, read beneath the mount
    with no link followed; the browser itself opens nothing."""
    import motion
    workspace = tmp_path / "agents" / "ag" / "workspace"
    (workspace / "site").mkdir(parents=True)
    beside = workspace / "site" / "a.png"           # relative to the page
    beside.write_bytes(b"beside")
    rooted = workspace / "b.png"                    # root-absolute
    rooted.write_bytes(b"rooted")
    secret = tmp_path / "agents" / "other" / "secret.png"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"secret")
    place = {"/workspace/site/a.png": str(beside), "/workspace/b.png": str(rooted)}
    asked: list[str] = []

    async def resolve(path, writing=False):
        asked.append(path)
        if path in place:
            return place[path]
        raise ValueError("denied")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    doc = motion.PageDoc(b"<p>page</p>", "/workspace/site")
    route_of = motion._page_route(doc)

    def verdict(url, route_of=route_of):
        route = _Route(url)
        asyncio.run(route_of(route))
        return route.verdict, route.body
    assert verdict(motion.PAGE_URL) == ("served", b"<p>page</p>")
    assert verdict(f"{motion.ORIGIN}/__page__/a.png") == ("served", b"beside")
    assert verdict(f"{motion.ORIGIN}/__page__/a.png") == ("served", b"beside")
    assert asked.count("/workspace/site/a.png") == 1    # once per file per call
    assert verdict(f"{motion.ORIGIN}/workspace/b.png") == ("served", b"rooted")
    assert verdict(f"{motion.ORIGIN}{secret}")[0] == "refused"
    assert verdict(f"{motion.ORIGIN}/__page__/../../../other/secret.png")[0] == "refused"
    assert verdict(f"file://{beside}")[0] == "refused"   # the browser opens no name
    assert verdict("https://example.com/x.png")[0] == "refused"
    assert verdict("data:image/png;base64,AAAA")[0] == "load"
    # A name judged once and then swapped for a link is never followed.
    rooted.unlink()
    rooted.symlink_to(secret)
    assert verdict(f"{motion.ORIGIN}/workspace/b.png")[0] == "refused"
    # Inline html names nothing relative to it.
    inline = motion._page_route(motion.PageDoc(b"<p>inline</p>", None))
    asked.clear()
    assert verdict(f"{motion.ORIGIN}/__page__/a.png", inline)[0] == "refused"
    assert asked == []


def _page_files(tmp_path, count):
    folder = tmp_path / "site"
    folder.mkdir()
    for i in range(count):
        (folder / f"f{i}.png").write_bytes(b"123456")
    return folder


def _gathered(route_of, names):
    """The page's requests of ``names``, all in flight together, as
    Playwright runs them: one task each."""
    import motion
    routes = [_Route(f"{motion.ORIGIN}/__page__/{name}") for name in names]

    async def main():
        await asyncio.gather(*(route_of(route) for route in routes))
    asyncio.run(main())
    return [route.verdict for route in routes]


def test_a_pages_names_are_counted_before_the_platform_is_asked(monkeypatch, tmp_path):
    """Requests of one page in flight together: the platform is asked for
    the page's share of names and no more, a name asked twice is asked
    once, the rest are refused, and the answer gets the sentence once."""
    import motion
    many = 2 * motion._PAGE_MAX_NAMES
    folder = _page_files(tmp_path, many)
    asked: list[str] = []

    async def resolve(path, writing=False):
        asked.append(path)
        await asyncio.sleep(0)                  # the platform answers later
        return str(folder / Path(path).name)
    monkeypatch.setattr(motion, "_resolve_path", resolve)

    doc = motion.PageDoc(b"<p>page</p>", "/workspace/site")
    names = ["f0.png"] + [f"f{i}.png" for i in range(many)]
    verdicts = _gathered(motion._page_route(doc), names)
    assert verdicts == ["served"] * (motion._PAGE_MAX_NAMES + 1) + ["refused"] * motion._PAGE_MAX_NAMES
    assert len(asked) == motion._PAGE_MAX_NAMES and doc.refused == [motion.NAMES_REFUSED]


def test_a_pages_bytes_are_counted_before_the_next_file_is_read(monkeypatch, tmp_path):
    """Requests of one page in flight together are served the page's share
    of bytes and no more, and the answer gets the sentence once. A file
    that is only missing is refused with no word of the limit."""
    import motion
    folder = _page_files(tmp_path, 4)

    async def resolve(path, writing=False):
        return str(folder / Path(path).name)
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    monkeypatch.setattr(motion, "_PAGE_MAX_BYTES", 10)

    doc = motion.PageDoc(b"<p>page</p>", "/workspace/site")
    verdicts = _gathered(motion._page_route(doc), [f"f{i}.png" for i in range(4)])
    assert verdicts == ["served", "refused", "refused", "refused"]
    assert doc.served == 6 and doc.refused == [motion.BYTES_REFUSED]

    missing = motion.PageDoc(b"<p>page</p>", "/workspace/site")
    assert _gathered(motion._page_route(missing), ["f0.png", "gone.png"]) == ["served", "refused"]
    assert missing.served == 6 and missing.refused == []


@pytest.mark.parametrize("kind", ["still", "frames"])
def test_a_third_capture_waits_for_one_of_the_two_turns(monkeypatch, tmp_path, kind):
    """While two captures hold their turns a third starts no browser, and
    once one of them ends it starts its own."""
    import time

    import playwright.async_api

    import motion

    class Driver:
        async def __aenter__(self):
            return "driver"

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(playwright.async_api, "async_playwright", Driver)
    launched: list[str] = []

    async def launch(p):
        launched.append(p)
        raise RuntimeError("no browser in this test")
    monkeypatch.setattr(motion, "_launch", launch)
    doc = motion.PageDoc(b"<p>page</p>", None)

    def capture(deadline):
        if kind == "still":
            return motion._capture_still(doc, 64, 64, 0.0, False, 1.0, deadline=deadline)
        return motion._capture_frames(doc, tmp_path, 64, 64, 10.0, 0.3, False, deadline=deadline)

    async def main():
        slot = motion._capture_slot()
        for _ in range(motion._CAPTURES_AT_ONCE):
            await slot.acquire()
        third = asyncio.create_task(capture(time.monotonic() + 30.0))
        await asyncio.sleep(0.2)
        assert not third.done() and launched == []
        slot.release()
        with pytest.raises(RuntimeError, match="no browser in this test"):
            await asyncio.wait_for(third, 5.0)
        assert launched == ["driver"]
    asyncio.run(main())


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_render_or_an_edit_whose_file_is_gone_says_so_in_one_sentence(proxy, monkeypatch, tmp_path):
    """A render's or an edit's file removed between its landing and the
    answer: the call says so in one sentence, with no raw error."""
    import project
    import quickops
    agents = tmp_path / "agents"
    workspace = agents / "ag" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    for name in ("c.vproj.json", "c.preview.mp4", "clip.mp4", "clip_edited.mp4"):
        proxy["answers"][f"/workspace/{name}"] = (f"ag/workspace/{name}", "ag")
    _clip(workspace / "clip.mp4", seconds=1)
    real_size = shared.landed_size_mb

    def gone_first(path):
        os.unlink(path)
        return real_size(path)
    monkeypatch.setattr(renderer, "landed_size_mb", gone_first)
    monkeypatch.setattr(quickops, "landed_size_mb", gone_first)

    comp = {"project": {"width": 320, "height": 180, "fps": 25},
            "tracks": [{"kind": "video", "clips": [{"fill": "black", "duration": 1}]}]}
    created = asyncio.run(project.handle_create_composition({"path": "/workspace/c.vproj.json",
                                                             "composition": comp}))
    assert not created.startswith("Error"), created
    rendered = asyncio.run(project.handle_render_composition({"path": "/workspace/c.vproj.json"}))
    assert rendered == "Error: " + shared.LANDED_GONE.format(name="c.preview.mp4")
    edited = asyncio.run(quickops.handle_edit_video({
        "path": "/workspace/clip.mp4", "operations": [{"op": "trim", "start": 0, "end": 0.5}]}))
    assert edited == "Error: " + shared.LANDED_GONE.format(name="clip_edited.mp4")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_the_blurs_encode_holds_the_gate_and_ends_on_a_cancel(monkeypatch, tmp_path):
    """The blur's encode passes the gate as every heavy run does, and a
    cancel ends its child: nothing is written and the gate is free."""
    import fftools
    import track
    src = tmp_path / "src.mp4"
    _clip(src, seconds=3)
    out = tmp_path / "blurred.mp4"
    children: list = []
    real_popen = subprocess.Popen

    def popen(*a, **kw):
        proc = real_popen(*a, **kw)
        children.append(proc)
        return proc
    monkeypatch.setattr(subprocess, "Popen", popen)
    held: list[int] = []
    started = threading.Event()

    def boxes_at(n, t):
        held.append(fftools._gate.held)
        started.set()
        threading.Event().wait(0.05)
        return [(0, 0, 8, 8)]

    async def main():
        task = asyncio.create_task(track.render_blurred(str(src), str(out), boxes_at, True))
        while not started.is_set():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fftools._gate.held == 0
    asyncio.run(main())
    assert held and set(held) == {1}
    assert children and all(p.poll() is not None for p in children)
    assert not out.exists() and not list(tmp_path.glob(".blurred.mp4.*"))

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

    async def fake_run(plan, *, progress=None, cancelled=None, landing=None):
        progress(42.0)
        await release.wait()
        return {"output": plan.out, "mode": "preview"}
    monkeypatch.setattr(renderer, "run_render", fake_run)
    place = {"/workspace/o.mp4": "/agents/ag/workspace/o.mp4"}

    async def resolve(p, writing=False):
        if p not in place:
            raise ValueError("x")
        return place[p]
    monkeypatch.setattr(jobs, "_resolve_path", resolve)

    async def main():
        job = await jobs.start(_plan(tmp_path), lambda r: "Rendered (preview)")
        await asyncio.sleep(0.01)
        assert jobs.busy()
        mid = await jobs.status(job.id)
        assert mid["state"] == "running" and mid["percent"] == 42.0 and mid["output"] == "ag/workspace/o.mp4"
        release.set()
        await job.task
        done = await jobs.status(job.id)
        assert done["state"] == "done" and done["percent"] == 100.0 and done["report"] == "Rendered (preview)"
        assert not jobs.busy()
        # Another session's place, and an unknown id, answer alike.
        place["/workspace/o.mp4"] = "/agents/other/workspace/o.mp4"
        assert (await jobs.status(job.id))["state"] == "lost"
        assert await jobs.status("nope") == {"job_id": "nope", "state": "lost", "message": jobs.LOST}
    asyncio.run(main())


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_a_background_job_holds_the_files_its_call_checked(monkeypatch, tmp_path, fresh_jobs):
    """A render's job runs after its call answered, with the call's record:
    a source the call probed and another file put at its name before the
    job's encode opens it is refused, and the job fails with the sentence."""
    import fftools
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=1)

    async def encode_later(plan, *, progress=None, cancelled=None, landing=None):
        _clip(tmp_path / "new.mp4", seconds=2)
        os.replace(tmp_path / "new.mp4", clip)
        with fftools.bound_files(["ffmpeg", "-i", str(clip), "-f", "null", "-"]):
            pass
        return {"output": plan.out, "mode": "preview"}
    monkeypatch.setattr(renderer, "run_render", encode_later)

    async def call():
        shared.begin_call()
        await fftools.probe(str(clip))
        return await jobs.start(_plan(tmp_path), str)

    async def main():
        job = await asyncio.create_task(call())     # the call ends before its job runs
        await job.task
        return job
    job = asyncio.run(main())
    assert job.state == "failed" and shared.CHANGED in job.error, job.error


def test_a_job_is_cancelled_its_plan_discarded(monkeypatch, tmp_path, fresh_jobs):
    async def slow_run(plan, *, progress=None, cancelled=None, landing=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(renderer, "run_render", slow_run)

    async def resolve(p, writing=False):
        return "/agents/ag/workspace/o.mp4"
    monkeypatch.setattr(jobs, "_resolve_path", resolve)

    async def main():
        plan = _plan(tmp_path)
        job = await jobs.start(plan, str)
        await asyncio.sleep(0.01)
        assert (await jobs.cancel(job.id))["state"] == "cancelled"
        await asyncio.gather(job.task, return_exceptions=True)
        assert (await jobs.status(job.id))["state"] == "cancelled"
        assert not plan.tmp.exists()
    asyncio.run(main())


def test_a_cancel_after_the_file_landed_says_so_and_the_job_finishes(monkeypatch, tmp_path, fresh_jobs):
    """Once a job's file has landed on its output the render is done: a
    cancel then stops nothing, says so, and the job ends with its report."""
    report_go = asyncio.Event()

    async def landed_then_report(plan, *, progress=None, cancelled=None, landing=None):
        landing()
        await report_go.wait()                # the probe and the loudness measure
        assert not cancelled()
        return {"output": plan.out, "mode": "preview"}
    monkeypatch.setattr(renderer, "run_render", landed_then_report)

    async def resolve(p, writing=False):
        return "/agents/ag/workspace/o.mp4"
    monkeypatch.setattr(jobs, "_resolve_path", resolve)

    async def main():
        job = await jobs.start(_plan(tmp_path), lambda r: "Rendered (preview)")
        await asyncio.sleep(0.01)
        answer = await jobs.cancel(job.id)
        assert answer == {"job_id": job.id, "state": "running", "output": "ag/workspace/o.mp4",
                          "message": jobs.LANDED}
        report_go.set()
        await job.task
        done = await jobs.status(job.id)
        assert done["state"] == "done" and done["report"] == "Rendered (preview)"
    asyncio.run(main())


def test_a_fifth_waiting_job_and_an_output_on_a_machine_are_refused(monkeypatch, tmp_path, fresh_jobs):
    async def slow_run(plan, *, progress=None, cancelled=None, landing=None):
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
    """The encode writes through ffmpeg's door: a temp beside the output
    that lands on the name only when the encode succeeded."""
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    clip = mount / "clip.mp4"
    _clip(clip)
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 25})
    comp["tracks"][0]["clips"] = [{"src": str(clip)}]
    comp_path = mount / "edit.vproj.json"
    comp_mod.save_composition(str(comp_path), comp)
    out = mount / "out.mp4"
    seen: list[float] = []
    import fftools
    commands: list[list[str]] = []
    real_spawn = fftools.asyncio.create_subprocess_exec

    async def spawn(*cmd, **kw):
        commands.append(list(cmd))
        return await real_spawn(*cmd, **kw)
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", spawn)

    async def main():
        plan = await renderer.plan_render(str(comp_path), same_path, output_path=str(out))
        return await renderer.run_render(plan, progress=seen.append)
    result = asyncio.run(main())
    assert result["output"] == str(out) and out.exists()
    assert seen and max(seen) <= 99.0
    assert not list(mount.glob(".out.mp4.*.partial"))
    # The encode never named the output, nor the source: descriptors only.
    encode = next(c for c in commands if c[0] == fftools.FFMPEG and "-filter_complex_script" in c)
    assert not [a for a in encode if str(mount) in a]
    assert encode[-1].startswith("/proc/self/fd/") and encode[-3:-1] == ["-f", "mp4"]

    # A failed encode leaves neither a temp file nor the output, though
    # ffmpeg wrote the whole file before it failed.
    out.unlink()
    failing = tmp_path / "ffmpeg-then-fail.sh"     # the test's own program, off the mount
    failing.write_text(f'#!/bin/sh\n"{fftools.FFMPEG}" "$@"\nexit 1\n')
    failing.chmod(0o755)
    monkeypatch.setattr(fftools, "FFMPEG", str(failing))

    async def again():
        plan = await renderer.plan_render(str(comp_path), same_path, output_path=str(out))
        await renderer.run_render(plan)
    with pytest.raises(fftools.FFmpegError, match=r"exit 1"):
        asyncio.run(again())
    assert not out.exists() and not list(mount.glob(".out.mp4.*.partial"))


@needs_ffmpeg
def test_a_cancel_as_the_output_lands_never_stops_the_render(tmp_path, monkeypatch):
    """A cancel that arrives as the encode's file lands finds no cancel
    point after it: the render returns its result and the file stays."""
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    clip = mount / "clip.mp4"
    _clip(clip, seconds=1)
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 25})
    comp["tracks"][0]["clips"] = [{"src": str(clip)}]
    comp_path = mount / "edit.vproj.json"
    comp_mod.save_composition(str(comp_path), comp)
    out = mount / "out.mp4"
    asked = {"cancel": False}

    def landing():
        asked["cancel"] = True

    async def main():
        plan = await renderer.plan_render(str(comp_path), same_path, mode="final",
                                          output_path=str(out))
        return await renderer.run_render(plan, cancelled=lambda: asked["cancel"],
                                         landing=landing)
    result = asyncio.run(main())
    assert asked["cancel"] and result["output"] == str(out)
    assert out.stat().st_size > 0 and "audio" in result


def _temp(folder: Path, name: str, tag: str, written: float) -> Path:
    """A temp of ``name`` the way the atomic writer names it, last written
    at ``written`` (wall clock)."""
    temp = folder / f".{name}.{tag}.partial"
    temp.write_bytes(b"x")
    os.utime(temp, (written, written))
    return temp


def test_the_sweep_removes_only_a_temp_no_writer_can_hold(tmp_path, monkeypatch):
    """Beside an output: a temp last written before this process started
    (a write the container's end stopped) and one untouched for an hour go.
    Another writer's temp written now, or half an hour ago, stays, as do
    another name's temp and the output itself."""
    import time

    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(tmp_path))
    now = time.time()
    monkeypatch.setattr(shared, "PROCESS_START", now - 2 * 3600)
    killed = _temp(tmp_path, "out.mp4", "0123456789ab", now - 2 * 3600 - 60)
    idle = _temp(tmp_path, "out.mp4", "123456789abc", now - shared.PARTIAL_ORPHAN_AGE_S - 60)
    paused = _temp(tmp_path, "out.mp4", "23456789abcd", now - 1800)
    live = _temp(tmp_path, "out.mp4", "3456789abcde", now)
    other = _temp(tmp_path, "other.mp4", "0123456789ab", now - 3 * 3600)
    (tmp_path / "out.mp4").write_bytes(b"done")
    shared.cleanup_partials(str(tmp_path / "out.mp4"))
    assert not killed.exists() and not idle.exists()
    assert paused.exists() and live.exists() and other.exists()
    assert (tmp_path / "out.mp4").read_bytes() == b"done"


@needs_ffmpeg
def test_a_render_keeps_another_writers_live_temp_of_its_output(tmp_path, monkeypatch):
    """A render sweeps its output's temp of a write that ended with the
    last process, and keeps the temp another writer of that name holds."""
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    clip = mount / "clip.mp4"
    _clip(clip, seconds=1)
    comp = comp_mod.new_composition({"width": 320, "height": 180, "fps": 25})
    comp["tracks"][0]["clips"] = [{"src": str(clip)}]
    comp_path = mount / "edit.vproj.json"
    comp_mod.save_composition(str(comp_path), comp)
    out = mount / "out.mp4"
    killed = _temp(mount, "out.mp4", "0123456789ab", shared.PROCESS_START - 60)
    with shared.safe_open_write(str(out)) as other_writer:   # still writing
        other_writer.write(b"another writer's bytes")
        other_writer.flush()
        held = [p for p in mount.glob(".out.mp4.*.partial") if p != killed]

        async def main():
            plan = await renderer.plan_render(str(comp_path), same_path, output_path=str(out))
            return await renderer.run_render(plan)
        asyncio.run(main())
        assert not killed.exists()
        assert len(held) == 1 and held[0].read_bytes() == b"another writer's bytes"
    assert out.read_bytes() == b"another writer's bytes"     # the last to land wins


@needs_ffmpeg
def test_the_filmstrip_is_one_small_picture(tmp_path, monkeypatch):
    import analysis
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=4)
    monkeypatch.setattr(analysis, "_resolve_path", same_path)
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

    async def resolve(path, writing=False):
        return str(tmp_path / Path(path).name)
    monkeypatch.setattr(analysis, "_resolve_path", resolve)
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

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            sent.append(json)
            return _Answer()
    monkeypatch.setattr(shared.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(shared, "PROXY_URL", "http://proxy")
    shared.set_request_context("s1", "Bearer t")
    assert asyncio.run(shared._resolve_via_proxy("/workspace/o.mp4", writing=True))[:2] \
        == ("ag/workspace/o.mp4", "ag")
    assert asyncio.run(shared._resolve_via_proxy("/workspace/o.mp4"))[:2] == ("ag/workspace/o.mp4", "ag")
    assert sent == [{"session_id": "s1", "path": "/workspace/o.mp4", "writing": True},
                    {"session_id": "s1", "path": "/workspace/o.mp4", "writing": False}]


# ── Every output is asked as a write ─────────────────────────────────────


class _Stop(Exception):
    pass


def _recorder(seen, tmp_path):
    """Records ``(path, writing)``; a read answers a file in ``tmp_path``,
    a write stops the call once recorded."""
    async def resolve(path, writing=False):
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

    async def resolve(path, writing=False):
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

    async def deny(path, writing=False):
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
    assert asyncio.run(shared._write_beside("/agents/.remote-host-cache/s1/clip.analysis.json")) is None
    assert not [s for s in proxy["sent"] if s[0].startswith(".remote-host-cache")]
    src = tmp_path / "clip.mp4"
    src.write_text("x")

    async def resolve(path, writing=False):
        return str(src)
    monkeypatch.setattr(quickops, "_resolve_path", resolve)
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

    async def resolve(path, writing=False):
        return str(tmp_path / Path(path).name)
    monkeypatch.setattr(analysis, "_resolve_path", resolve)
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
    # The test's own program is no file of the mount.
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(tmp_path / "mount"))
    with pytest.raises(fftools.FFmpegError) as err:
        asyncio.run(fftools.run_ffmpeg(["-version"]))
    assert str(err.value) == fftools.OUT_OF_MEMORY


def test_a_stuck_read_in_colour_matching_ends(monkeypatch, tmp_path):
    import colormatch
    clip = tmp_path / "x.mp4"
    clip.write_bytes(b"x")

    def stuck(*a, **kw):
        assert kw.get("timeout")
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])
    monkeypatch.setattr(subprocess, "run", stuck)
    with pytest.raises(ValueError, match="frame size"):
        colormatch._frame_size(str(clip))


def test_a_job_past_its_ceiling_fails_and_frees_the_tool(monkeypatch, tmp_path):
    jobs.reset_for_tests()
    monkeypatch.setattr(jobs, "JOB_MAX_S", 0.2)

    async def resolve(path, writing=False):
        return str(tmp_path / "out.mp4")
    monkeypatch.setattr(jobs, "_resolve_path", resolve)

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

    async def resolve(path, writing=False):
        return out
    monkeypatch.setattr(jobs, "_resolve_path", resolve)

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
    answer = asyncio.run(jobs.status(job.id))
    assert answer["state"] == "done" and answer["note"] == jobs.NOT_SENT


def _looped_source(folder: Path) -> list[str]:
    """Input arguments that keep ffmpeg reading until it is stopped: a
    short clip beneath the mount, looped at its own pace."""
    import fftools
    clip = folder / "loop-source.mp4"
    subprocess.run([fftools.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True, timeout=120)
    return ["-stream_loop", "-1", "-re", "-i", str(clip)]


@needs_ffmpeg
def test_a_cancel_ends_ffmpeg(tmp_path):
    """A cancelled encode is killed, not left to run: the process is gone
    right after the cancel."""
    import fftools
    source = _looped_source(tmp_path)
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
                [*source, "-f", "null", "-"], heavy=False))
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


@needs_ffmpeg
def test_a_cancelled_encode_into_the_mount_leaves_no_file_and_no_temp(tmp_path, monkeypatch):
    """Cancelled mid-encode, the child is killed before the cancel goes on,
    and its output under the mount is neither at its name nor left as a
    temp beside it."""
    import fftools
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(mount))
    out = mount / "long.mp4"
    source = _looped_source(mount)
    started: list = []
    real = asyncio.create_subprocess_exec

    async def spawn(*cmd, **kw):
        proc = await real(*cmd, **kw)
        started.append(proc)
        return proc
    monkeypatch.setattr(fftools.asyncio, "create_subprocess_exec", spawn)

    async def main():
        task = asyncio.create_task(fftools.run_ffmpeg(
            [*source, "-c:v", "libx264", "-preset", "ultrafast", str(out)], heavy=False))
        temps: list = []
        for _ in range(200):
            temps = list(mount.glob(".long.mp4.*.partial"))
            if started and temps and temps[0].stat().st_size > 0:
                break
            await asyncio.sleep(0.05)
        assert temps and started[0].returncode is None   # encoding into its temp
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started[0].returncode is not None
        assert not out.exists() and not list(mount.glob(".long.mp4.*.partial"))
    asyncio.run(main())


def _fds_on(path: Path) -> int:
    """How many of this process's descriptors are open on ``path``."""
    real = os.path.realpath(path)
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            count += os.readlink(f"/proc/self/fd/{fd}") == real
        except OSError:
            pass
    return count


@needs_ffmpeg
def test_a_reading_thread_keeps_its_descriptor_when_its_caller_is_cancelled(tmp_path):
    """A library that reads by name (OpenCV, scenedetect) reads in a thread
    through ``opened_name``: the thread holds the block, so a cancelled
    caller never closes the descriptor under it; it closes when the
    thread's block ends."""
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=1)
    opened, go_on = threading.Event(), threading.Event()
    seen: dict = {}

    def read_by_name():
        with shared.opened_name(str(clip)) as name:
            seen["name"] = name
            opened.set()
            go_on.wait(30)
            with open(name, "rb") as fh:
                seen["head"] = fh.read(8)
            seen["open_inside"] = _fds_on(clip)
        seen["open_after"] = _fds_on(clip)

    async def main():
        reader = asyncio.ensure_future(asyncio.to_thread(read_by_name))
        assert await asyncio.to_thread(opened.wait, 30)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert os.readlink(seen["name"]) == os.path.realpath(clip)   # still open
        go_on.set()
    asyncio.run(main())                       # waits for the thread to end
    assert seen["head"][4:8] == b"ftyp"
    assert seen["open_inside"] >= 1 and seen["open_after"] == 0


@needs_ffmpeg
def test_an_opencv_capture_holds_its_own_descriptor_after_its_caller_is_cancelled(tmp_path):
    """``open_capture`` closes the descriptor it opened as soon as OpenCV
    has its own: a capture made in a thread whose caller was cancelled
    keeps reading frames."""
    clip = tmp_path / "clip.mp4"
    _clip(clip, seconds=1)
    opened, go_on = threading.Event(), threading.Event()
    seen: dict = {}

    def capture():
        cap = shared.open_capture(str(clip))
        seen["open"] = _fds_on(clip)                 # the capture's own, only
        opened.set()
        go_on.wait(30)
        try:
            ok, frame = cap.read()
            seen["frame"] = frame.shape if ok else None
        finally:
            cap.release()

    async def main():
        reader = asyncio.ensure_future(asyncio.to_thread(capture))
        assert await asyncio.to_thread(opened.wait, 30)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        go_on.set()
    asyncio.run(main())
    assert seen["open"] == 1
    assert seen["frame"] == (180, 320, 3)


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

    async def resolve(path, writing=False):
        asked.append((path, writing))
        if writing:
            return str(tmp_path / "s.png")
        raise ValueError("denied")
    monkeypatch.setattr(motion, "_resolve_path", resolve)
    html = f'<body style="margin:0"><img src="file://{secret}" width="32" height="32"></body>'
    asyncio.run(motion.handle_render_still({"html": html, "width": 64, "height": 64,
                                            "output_path": "/workspace/s.png"}))
    assert (str(secret), False) in asked


def test_a_real_page_gets_a_picture_its_script_names_through_the_route(proxy, monkeypatch, tmp_path):
    """A page under the mount whose script builds, at run time, a relative
    URL to a picture beside it (a name the preflight never saw): the browser
    asks the page's origin, the route serves the picture read beneath the
    mount. A file outside the session's tree and another origin are
    aborted."""
    from test_motion import HAVE_CHROMIUM
    if not (HAVE_FFMPEG and HAVE_CHROMIUM):
        pytest.skip("ffmpeg or Playwright's chromium is not installed in this venv")
    import motion
    from PIL import Image
    agents = tmp_path / "agents"
    site = agents / "ag" / "workspace" / "site"
    site.mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    Image.new("RGB", (32, 32), (255, 0, 0)).save(site / "pic.png")
    outside = tmp_path / "outside.png"                     # not in the session's tree
    Image.new("RGB", (32, 32), (0, 255, 0)).save(outside)
    (site / "page.html").write_text(
        '<body style="margin:0;background:#000">'
        '<img id="a" style="position:absolute;left:0;top:0;width:32px;height:32px">'
        '<img id="b" style="position:absolute;left:32px;top:0;width:32px;height:32px">'
        '<img id="c" style="position:absolute;left:64px;top:0;width:32px;height:32px">'
        "<script>"
        "document.getElementById('a').src = 'pi' + 'c.png';"
        f"document.getElementById('b').src = '{outside.parent}' + '/' + 'outside.png';"
        "document.getElementById('c').src = 'https://example.' + 'com/pic.png';"
        "</script></body>")
    for name in ("site/page.html", "site/pic.png", "shot.png"):
        proxy["answers"][f"/workspace/{name}"] = (f"ag/workspace/{name}", "ag")
    verdicts: dict[str, str] = {}
    real_route = motion._page_route

    def recording(doc):
        inner = real_route(doc)

        async def route(r):
            class Seen:
                request = r.request

                async def fulfill(self, **kw):
                    verdicts[r.request.url] = "served"
                    await r.fulfill(**kw)

                async def abort(self, *a):
                    verdicts[r.request.url] = "aborted"
                    await r.abort(*a)

                async def continue_(self, *a, **kw):
                    verdicts[r.request.url] = "loaded"
                    await r.continue_(*a, **kw)
            await inner(Seen())
        return route
    monkeypatch.setattr(motion, "_page_route", recording)
    answer = asyncio.run(motion.handle_render_still(
        {"html_path": "/workspace/site/page.html", "width": 96, "height": 32,
         "output_path": "/workspace/shot.png"}))
    assert answer.startswith("Rendered still: ag/workspace/shot.png"), answer
    # The still's echo is taken back as a write, to the same file.
    assert _resolve("ag/workspace/shot.png", writing=True) == str(agents / "ag" / "workspace" / "shot.png")
    assert verdicts[motion.PAGE_URL] == "served"
    assert verdicts[f"{motion.ORIGIN}/__page__/pic.png"] == "served"
    assert verdicts[f"{motion.ORIGIN}{outside}"] == "aborted"
    assert verdicts["https://example.com/pic.png"] == "aborted"
    shot = Image.open(agents / "ag" / "workspace" / "shot.png").convert("RGB")

    def box(left):
        return {shot.getpixel((x, y)) for x in range(left, left + 32) for y in range(32)}
    assert box(0) == {(255, 0, 0)}                  # the picture, through the route
    # The other two hold the browser's broken-image mark, never a picture.
    assert (0, 255, 0) not in box(32) and (255, 0, 0) not in box(64)


@contextlib.contextmanager
def _loopback_ports():
    """A TCP and a UDP port of the test's own on loopback, each counting
    what reaches it. The test reaches each once itself, so a count that
    stays at one shows a port that listened and got nothing else."""
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    tcp.settimeout(0.1)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.1)
    seen = {"tcp": 0, "udp": 0}
    stop = threading.Event()

    def count():
        while not stop.is_set():
            with contextlib.suppress(OSError):
                conn, _ = tcp.accept()
                seen["tcp"] += 1
                conn.close()
            with contextlib.suppress(OSError):
                udp.recvfrom(2048)
                seen["udp"] += 1
    counter = threading.Thread(target=count, daemon=True)
    counter.start()
    port, udp_port = tcp.getsockname()[1], udp.getsockname()[1]
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.sendto(b"x", ("127.0.0.1", udp_port))
        for _ in range(100):
            if seen == {"tcp": 1, "udp": 1}:
                break
            threading.Event().wait(0.05)
        assert seen == {"tcp": 1, "udp": 1}
        yield port, udp_port, seen
    finally:
        stop.set()
        counter.join()
        tcp.close()
        udp.close()


# What a page tries, each answered "refused" or with what it got instead.
_PAGE_TRIES = """async ({port, udp}) => {
  const out = {};
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  for (const [key, url] of [["fetch_loopback", `https://127.0.0.1:${port}/x`],
                            ["fetch_named", "https://example.com/x"]]) {
    try { await fetch(url); out[key] = "loaded"; } catch (e) { out[key] = "refused"; }
  }
  out.socket = await Promise.race([wait(5000).then(() => "no answer"), new Promise((done) => {
    const ws = new WebSocket(`wss://127.0.0.1:${port}/`);
    ws.onopen = () => done("open");
    ws.onclose = (e) => done(`closed ${e.code} ${e.reason}`);
  })]);
  out.worker_socket = await Promise.race([wait(5000).then(() => "no answer"), new Promise((done) => {
    const src = `const ws = new WebSocket("wss://127.0.0.1:${port}/");
      ws.onopen = () => postMessage("open");
      ws.onerror = () => postMessage("refused");
      ws.onclose = () => postMessage("refused");`;
    const worker = new Worker(URL.createObjectURL(new Blob([src], {type: "text/javascript"})));
    worker.onmessage = (e) => done(e.data);
    worker.onerror = () => done("no worker");
  })]);
  try {
    const reg = await navigator.serviceWorker.register("sw.js");
    out.service_worker = reg ? "registered" : "refused";
  } catch (e) { out.service_worker = "refused"; }
  out.registrations = (await navigator.serviceWorker.getRegistrations()).length;
  out.ice_candidates = await new Promise((done) => {
    const pc = new RTCPeerConnection({iceServers: [{urls: `stun:127.0.0.1:${udp}`}]});
    const found = [];
    pc.onicecandidate = (e) => { if (e.candidate && e.candidate.candidate) found.push(e.candidate.candidate); };
    pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") done(found); };
    pc.createDataChannel("x");
    pc.createOffer().then((offer) => pc.setLocalDescription(offer));
    wait(5000).then(() => done(found));
  });
  return out;
}"""


def test_a_real_page_reaches_no_network_but_its_route(proxy, monkeypatch, tmp_path):
    """A page under the mount tries a fetch to another origin, a WebSocket
    from the page and from a worker, a service worker of its own folder and
    a WebRTC connection. Each is refused, and the test's own loopback ports
    get nothing."""
    from test_motion import HAVE_CHROMIUM
    if not (HAVE_FFMPEG and HAVE_CHROMIUM):
        pytest.skip("ffmpeg or Playwright's chromium is not installed in this venv")
    from playwright.async_api import async_playwright

    import motion
    agents = tmp_path / "agents"
    site = agents / "ag" / "workspace" / "site"
    site.mkdir(parents=True)
    monkeypatch.setattr(shared, "MOUNT_AGENTS_DIR", str(agents))
    monkeypatch.setenv("VIDEOTOOLS_WRITE_ROOT", str(agents))
    # A worker script the session may read: a registration fails on the
    # block, never on a missing file.
    (site / "sw.js").write_text("self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)));")
    proxy["answers"]["/workspace/site/sw.js"] = ("ag/workspace/site/sw.js", "ag")
    verdicts: dict[str, str] = {}
    real_route = motion._page_route

    def recording(doc):
        inner = real_route(doc)

        async def route(r):
            class Seen:
                request = r.request

                async def fulfill(self, **kw):
                    verdicts[r.request.url] = "served"
                    await r.fulfill(**kw)

                async def abort(self, *a):
                    verdicts[r.request.url] = "aborted"
                    await r.abort(*a)

                async def continue_(self, *a, **kw):
                    verdicts[r.request.url] = "loaded"
                    await r.continue_(*a, **kw)
            await inner(Seen())
        return route
    monkeypatch.setattr(motion, "_page_route", recording)
    doc = motion.PageDoc(b"<!doctype html><p>page</p>", "/workspace/site")

    async def tries(port, udp):
        async with async_playwright() as p:
            browser = await motion._launch(p)
            try:
                context = await motion._page_context(browser, doc, viewport={"width": 64, "height": 64})
                page = await context.new_page()
                await page.goto(motion.PAGE_URL, wait_until="load")
                return await page.evaluate(_PAGE_TRIES, {"port": port, "udp": udp})
            finally:
                await browser.close()
    with _loopback_ports() as (port, udp, seen):
        out = asyncio.run(tries(port, udp))
        assert seen == {"tcp": 1, "udp": 1}, out          # nothing of the page's reached them
    assert verdicts[motion.PAGE_URL] == "served"
    assert out["fetch_loopback"] == out["fetch_named"] == "refused"
    assert verdicts[f"https://127.0.0.1:{port}/x"] == verdicts["https://example.com/x"] == "aborted"
    code, reason = motion.SOCKET_REFUSED
    assert out["socket"] == f"closed {code} {reason}"
    assert out["worker_socket"] == "refused"
    assert out["service_worker"] == "refused" and out["registrations"] == 0
    assert out["ice_candidates"] == []


def test_the_browser_itself_reaches_no_address(monkeypatch):
    """Chromium as the tool launches it, in a context with no route at all:
    a navigation to the test's own loopback port fails and reaches
    nothing."""
    from test_motion import HAVE_CHROMIUM
    if not (HAVE_FFMPEG and HAVE_CHROMIUM):
        pytest.skip("ffmpeg or Playwright's chromium is not installed in this venv")
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    import motion

    async def go(port):
        async with async_playwright() as p:
            browser = await motion._launch(p)
            try:
                page = await (await browser.new_context()).new_page()
                with pytest.raises(PlaywrightError):
                    await page.goto(f"http://127.0.0.1:{port}/", timeout=15000)
            finally:
                await browser.close()
    with _loopback_ports() as (port, _udp, seen):
        asyncio.run(go(port))
        assert seen["tcp"] == 1


def test_the_browser_resolves_no_name(monkeypatch):
    """Chromium with the tool's resolver rule alone resolves no name: a
    navigation to a name of the test's own loopback port fails and reaches
    nothing."""
    from test_motion import HAVE_CHROMIUM
    if not (HAVE_FFMPEG and HAVE_CHROMIUM):
        pytest.skip("ffmpeg or Playwright's chromium is not installed in this venv")
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    import motion
    rule = [a for a in motion._CHROMIUM_ARGS if a.startswith("--host-resolver-rules=")]
    assert len(rule) == 1

    async def go(port):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", *rule])
            try:
                page = await (await browser.new_context()).new_page()
                with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
                    await page.goto(f"http://localhost:{port}/", timeout=15000)
            finally:
                await browser.close()
    with _loopback_ports() as (port, _udp, seen):
        asyncio.run(go(port))
        assert seen["tcp"] == 1

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

    async def resolve(path, writing=False):
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
