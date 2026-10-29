"""Unit tests for stream_sidecar's session-lifecycle helpers (stdlib-only —
aiohttp is imported lazily by the handlers, so the module imports clean).
The same text is the platform's ``mcps/_shared/stream_sidecar.py``.

The regression under test (2026-07-09): the idle GC reaped sessions whose
client still held the standalone SSE GET open — a warm CLI/codex daemon idles
between turns without issuing requests, so ``last`` went stale, the GC
DELETEd the upstream session at idle+10min, and the attached client fell into
a reconnect→404 retry storm. The SSE forwarding loop now calls ``_refresh``
on every successful keepalive/chunk write, so an attached stream counts as
activity; ``_refresh`` must never resurrect a session that was already
DELETEd/GC'd (its upstream state is gone).
"""

import asyncio
import time

import pytest

import stream_sidecar as sc


@pytest.fixture(autouse=True)
def _clean_maps():
    sc._sessions.clear()
    sc._oto_to_mcp.clear()
    yield
    sc._sessions.clear()
    sc._oto_to_mcp.clear()


def test_touch_creates_and_maps():
    sc._touch("mcp-1", "oto-1")
    assert "mcp-1" in sc._sessions
    assert sc._oto_to_mcp["oto-1"] == "mcp-1"


def test_refresh_keeps_attached_session_out_of_gc():
    sc._touch("mcp-1", "oto-1")
    # Simulate a long think-gap: the entry is stale by TTL...
    sc._sessions["mcp-1"]["last"] = time.time() - sc.SESSION_IDLE_S - 60
    assert sc._stale_sids(time.time()) == ["mcp-1"]
    # ...but the attached stream's keepalive write refreshes it.
    sc._refresh("mcp-1")
    assert sc._stale_sids(time.time()) == []


def test_refresh_never_resurrects_forgotten_session():
    sc._touch("mcp-1", "oto-1")
    sc._forget("mcp-1")
    sc._refresh("mcp-1")  # straggling stream after DELETE/GC
    assert "mcp-1" not in sc._sessions
    assert "oto-1" not in sc._oto_to_mcp


def test_refresh_unknown_sid_is_noop():
    sc._refresh("never-seen")
    assert sc._sessions == {}


def test_stale_selection_honors_ttl():
    sc._touch("fresh", "")
    sc._touch("stale", "")
    sc._sessions["stale"]["last"] = time.time() - sc.SESSION_IDLE_S - 1
    assert sc._stale_sids(time.time()) == ["stale"]


def test_forget_does_not_clobber_reinit_mapping():
    # A re-init reuses the oto id with a fresh mcp-session-id; forgetting the
    # OLD session must not orphan the live one's active-close mapping.
    sc._touch("mcp-old", "oto-1")
    sc._touch("mcp-new", "oto-1")  # remap
    sc._forget("mcp-old")
    assert sc._oto_to_mcp["oto-1"] == "mcp-new"


def test_open_sessions_leaves_out_the_checker():
    sc._touch("held", "otodock-checker-held")
    sc._touch("fresh", "otodock-checker-fresh")
    sc._touch("agent", "6e5475d2-3492")
    sc._touch("anon", "")
    assert sc._open_sessions() == 4
    # A session with no OtoDock id still counts: unknown use is use.
    assert sc._open_sessions("otodock-checker") == 2


def test_gc_pass_deletes_only_stale_sessions(monkeypatch):
    deleted = []

    async def fake_delete(sid):
        deleted.append(sid)
        return 200

    monkeypatch.setattr(sc, "_delete_upstream", fake_delete)
    monkeypatch.setattr(sc, "SCREENSHOTS_DIR", "/nonexistent-screenshots")
    sc._touch("old", "oto-old")
    sc._touch("new", "oto-new")
    sc._sessions["old"]["last"] = time.time() - sc.SESSION_IDLE_S - 1
    asyncio.run(sc._gc_pass())
    assert deleted == ["old"]
    assert list(sc._sessions) == ["new"]
