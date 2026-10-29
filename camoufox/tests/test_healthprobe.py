"""The health checker's pure parts: how an answer is judged, how checks are
counted, and what the healthcheck and the watchdog read from the status.

The regression behind it: the old probe counted an empty answer
as a pass, so a browser that loaded nothing read healthy for three days.
"""

import json

import pytest

import healthprobe as hp

NOW = 1_800_000_000.0
DAY = hp.MAX_AGE_S


def _ok(req_id="chk-2"):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {
        "content": [{"type": "text", "text": "### Page\n- Page URL: http://127.0.0.1:8930/"}]}})


def _sse(*msgs, comments=0):
    parts = [": keepalive\n\n"] * comments
    parts += [f"event: message\ndata: {json.dumps(m)}\n\n" for m in msgs]
    return "".join(parts)


# --- judging an answer --------------------------------------------------------

def test_a_json_result_passes():
    assert hp.rpc_result(_ok(), "chk-2") == (True, "")


def test_an_sse_result_passes_after_keepalives():
    raw = _sse(json.loads(_ok()), comments=3)
    assert hp.rpc_result(raw, "chk-2") == (True, "")


def test_an_empty_answer_fails():
    # The stream the server ends when it closes a session mid-call.
    assert hp.rpc_result("", "chk-2") == (False, "empty answer")


def test_keepalive_comments_alone_fail():
    # The stream the sidecar ends when it cuts a call at its limit.
    assert hp.rpc_result(_sse(comments=4), "chk-2") == (False, "no answer")


def test_an_answer_to_another_request_fails():
    assert hp.rpc_result(_ok("chk-9"), "chk-2") == (False, "no answer")


def test_a_server_request_with_the_same_id_is_not_an_answer():
    ping = {"jsonrpc": "2.0", "id": "chk-2", "method": "ping"}
    assert hp.rpc_result(_sse(ping), "chk-2")[0] is False


def test_a_json_rpc_error_fails_with_its_message():
    raw = json.dumps({"jsonrpc": "2.0", "id": "chk-2", "error": {"code": -32000, "message": "boom"}})
    assert hp.rpc_result(raw, "chk-2") == (False, "JSON-RPC error: boom")


def test_a_tool_error_fails_with_its_text():
    raw = _sse({"jsonrpc": "2.0", "id": "chk-2", "result": {
        "isError": True, "content": [{"type": "text", "text": "TimeoutError: page.goto"}]}})
    assert hp.rpc_result(raw, "chk-2") == (False, "tool error: TimeoutError: page.goto")


def test_a_multi_line_sse_data_field_is_one_message():
    raw = 'event: message\ndata: {"jsonrpc": "2.0", "id": "chk-2",\ndata:  "result": {"content": []}}\n\n'
    assert hp.rpc_result(raw, "chk-2") == (True, "")


def test_a_json_batch_is_read():
    raw = json.dumps([json.loads(_ok("chk-1")), json.loads(_ok("chk-2"))])
    assert hp.rpc_result(raw, "chk-2") == (True, "")


# --- counting checks ----------------------------------------------------------

def test_failures_count_per_kind_and_a_pass_resets_its_own():
    s = hp.starting_status(NOW)
    s = hp.record(s, "held", False, "no answer within 30 s", NOW + 1)
    s = hp.record(s, "fresh", False, "empty answer", NOW + 2)
    s = hp.record(s, "held", True, "", NOW + 3)
    assert s["held_failures"] == 0
    assert s["fresh_failures"] == 1
    assert s["starting"] is False and s["last_ok"] == NOW + 3


def test_idle_since_starts_at_the_first_empty_count_and_keeps():
    s = hp.starting_status(NOW)
    s = hp.note_sessions(s, 0, NOW + 10)
    s = hp.note_sessions(s, 0, NOW + 130)
    assert s["idle_since"] == NOW + 10


def test_a_session_or_an_unknown_count_resets_idle():
    s = hp.note_sessions(hp.starting_status(NOW), 0, NOW + 10)
    assert hp.note_sessions(s, 1, NOW + 20)["idle_since"] is None
    assert hp.note_sessions(s, None, NOW + 20)["idle_since"] is None


# --- the healthcheck ------------------------------------------------------------

def _checked(**extra):
    s = hp.record(hp.starting_status(NOW - 600), "held", True, "", NOW - 10)
    s.update(extra)
    return s


def test_healthy_after_a_recent_pass():
    assert hp.healthy(_checked(), NOW) == (True, "")


def test_unhealthy_without_a_status_or_while_starting():
    assert hp.healthy(None, NOW)[0] is False
    assert hp.healthy(hp.starting_status(NOW), NOW)[0] is False


def test_one_failure_stays_healthy_two_do_not():
    assert hp.healthy(_checked(held_failures=1), NOW)[0] is True
    assert hp.healthy(_checked(fresh_failures=2, error="fresh: empty answer"), NOW) == (
        False, "fresh: empty answer")


def test_a_stale_status_is_unhealthy():
    assert hp.healthy(_checked(last_check=NOW - hp.STALE_S - 1), NOW)[0] is False


# --- the watchdog's verdict ----------------------------------------------------------

def test_no_status_is_fine_at_first_then_recycles():
    assert hp.verdict(None, NOW, NOW - 60)[0] == 0
    assert hp.verdict(None, NOW, NOW - hp.STALE_S - 1)[0] == 1


def test_a_checker_stuck_starting_recycles():
    assert hp.verdict(hp.starting_status(NOW - 30), NOW, NOW - 30)[0] == 0
    assert hp.verdict(hp.starting_status(NOW - hp.STALE_S - 1), NOW, NOW - 900)[0] == 1


def test_two_failures_of_either_kind_recycle():
    assert hp.verdict(_checked(held_failures=1), NOW, NOW - 900)[0] == 0
    assert hp.verdict(_checked(held_failures=2), NOW, NOW - 900)[0] == 1
    assert hp.verdict(_checked(fresh_failures=2), NOW, NOW - 900)[0] == 1


def test_a_stale_status_recycles():
    assert hp.verdict(_checked(last_check=NOW - hp.STALE_S - 1), NOW, NOW - 900)[0] == 1


def _old_and_idle(idle_for):
    return _checked(started=NOW - DAY - 60, idle_since=NOW - idle_for, agent_sessions=0)


def test_the_idle_restart_needs_a_day_up_long_idle_and_a_live_zero():
    code, why = hp.verdict(_old_and_idle(hp.IDLE_S + 1), NOW, NOW - DAY, live_open=0)
    assert code == 2 and "no agent session" in why
    # A session that arrived since the last tick keeps the browser up.
    assert hp.verdict(_old_and_idle(hp.IDLE_S + 1), NOW, NOW - DAY, live_open=1)[0] == 0
    # An unknown count is never idle.
    assert hp.verdict(_old_and_idle(hp.IDLE_S + 1), NOW, NOW - DAY, live_open=None)[0] == 0


def test_no_idle_restart_when_young_or_recently_used():
    young = _checked(started=NOW - 3600, idle_since=NOW - hp.IDLE_S - 1, agent_sessions=0)
    assert hp.verdict(young, NOW, NOW - 3600, live_open=0)[0] == 0
    assert hp.verdict(_old_and_idle(hp.IDLE_S - 60), NOW, NOW - DAY, live_open=0)[0] == 0


def test_the_reason_reads_minutes_then_hours():
    assert hp.span(480) == "8 min"
    assert hp.span(1800) == "30 min"
    assert hp.span(DAY + 600) == "24 h"
    why = hp.verdict(_old_and_idle(hp.IDLE_S + 1), NOW, NOW - DAY, live_open=0)[1]
    assert why.startswith("browser up 24 h with no agent session for 30 min")


def test_failures_win_over_the_idle_restart():
    s = _old_and_idle(hp.IDLE_S + 1)
    s["held_failures"] = 2
    assert hp.verdict(s, NOW, NOW - DAY, live_open=0)[0] == 1


# --- a session that fails to open ---------------------------------------------

class _FakeServer:
    """initialize answers with a session id; what follows is the test's."""

    def __init__(self, monkeypatch, *, initialized_fails):
        self.deletes = 0
        self.initialized_fails = initialized_fails
        monkeypatch.setattr(hp, "_post", self.post)
        monkeypatch.setattr(hp, "_with_deadline", self.with_deadline)

    def post(self, url, body, headers, timeout):
        if body.get("method") == "initialize":
            return "sid-1", ""
        if self.initialized_fails:
            raise OSError("connection reset")
        return None, ""

    def with_deadline(self, fn, deadline_s):
        self.deletes += 1  # Session.close's DELETE is the only deadline call here
        return True, None


def test_a_failure_after_initialize_closes_the_session(monkeypatch):
    server = _FakeServer(monkeypatch, initialized_fails=True)
    s = hp.Session(hp.HELD_ID)
    with pytest.raises(OSError):
        s.open()
    assert s.alive is False and s.stop.is_set()
    assert server.deletes == 1  # the server's session is closed, not left behind


def test_an_event_stream_that_does_not_open_closes_the_session(monkeypatch):
    server = _FakeServer(monkeypatch, initialized_fails=False)

    def listen_fails(self, ready):
        self.gone.set()
        ready.set()

    monkeypatch.setattr(hp.Session, "_listen", listen_fails)
    s = hp.Session(hp.FRESH_ID)
    with pytest.raises(RuntimeError, match="event stream did not open"):
        s.open()
    assert s.alive is False
    assert server.deletes == 1
