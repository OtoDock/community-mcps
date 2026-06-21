"""github-mcp sidecar — async tests.

Subprocess is mocked; tests verify the sidecar's bearer-to-env discipline,
session lifecycle, and error paths without spawning the real Go binary.

Run from the github-mcp directory::

    python -m pytest test_sidecar.py -v

Requires fastapi + uvicorn (the sidecar's own requirements.txt) plus pytest +
pytest-asyncio + httpx.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import sidecar


@pytest_asyncio.fixture
async def client():
    """Lifespan-aware test client — runs the sidecar's startup + shutdown hooks."""
    async with AsyncClient(
        transport=ASGITransport(app=sidecar.app), base_url="http://test"
    ) as ac:
        yield ac
    # Wipe any sessions a test left behind so the next test starts clean.
    async with sidecar.SESSIONS_LOCK:
        for sess in list(sidecar.SESSIONS.values()):
            await sess.close()
        sidecar.SESSIONS.clear()


def _build_mock_proc(stdout_lines: list[bytes]) -> MagicMock:
    """Build a mock subprocess.Process whose stdout yields ``stdout_lines``
    one at a time and whose stdin records all writes."""
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    # readline() returns the next queued line each call
    lines_iter = iter(stdout_lines + [b""])  # trailing EOF
    proc.stdout.readline = AsyncMock(side_effect=lambda: next(lines_iter))
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.terminate = MagicMock(side_effect=lambda: setattr(proc, "returncode", 0))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


@pytest.mark.asyncio
async def test_sidecar_rejects_missing_bearer(client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert "bearer" in r.text.lower()


@pytest.mark.asyncio
async def test_sidecar_rejects_empty_bearer(client):
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


_INIT_RESP = b'{"jsonrpc":"2.0","id":"_sidecar_init","result":{}}\n'


@pytest.mark.asyncio
async def test_sidecar_spawns_subprocess_with_bearer_in_env(client):
    """First request for a new session_id spawns the stdio child and forwards
    the bearer as GITHUB_PERSONAL_ACCESS_TOKEN in the child's env. Client
    is sending its own ``initialize`` so the sidecar passes it through."""
    proc = _build_mock_proc([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])
    with patch(
        "sidecar.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Authorization": "Bearer ghp_test_token_xyz"},
        )
    assert r.status_code == 200
    assert r.json()["result"] == {}
    spawn.assert_called_once()
    _, kwargs = spawn.call_args
    assert kwargs["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test_token_xyz"
    assert "LOG_LEVEL" not in kwargs["env"]
    assert sidecar.SESSION_HEADER in {h.lower() for h in r.headers.keys()}


@pytest.mark.asyncio
async def test_sidecar_auto_handshake_for_stateless_client(client):
    """Stateless client sends a non-initialize method as its first request.
    Sidecar must do the initialize handshake internally before routing,
    consuming one extra stdout line (the init response)."""
    proc = _build_mock_proc([
        _INIT_RESP,  # internal initialize response (discarded)
        b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n',  # real response
    ])
    with patch(
        "sidecar.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer t"},
        )
    assert r.status_code == 200
    assert r.json()["result"] == {"tools": []}
    # Verify the internal handshake actually wrote initialize +
    # notifications/initialized to the child's stdin.
    writes = [call.args[0] for call in proc.stdin.write.call_args_list]
    method_calls = []
    for raw in writes:
        try:
            method_calls.append(json.loads(raw.decode()).get("method"))
        except Exception:
            pass
    assert method_calls == ["initialize", "notifications/initialized", "tools/list"]


@pytest.mark.asyncio
async def test_sidecar_notification_returns_202_empty_body(client):
    """A JSON-RPC notification (no ``id``) must get 202 Accepted + an EMPTY
    body — NOT 200 with ``{}``. Codex's Rust rmcp transport rejects a 200/{}
    for ``notifications/initialized`` ("data did not match any variant of
    untagged enum JsonRpcMessage") and tears the whole session down. The
    notification is still forwarded to the child's stdin."""
    proc = _build_mock_proc([
        _INIT_RESP,  # internal handshake initialize response (discarded)
    ])
    with patch(
        "sidecar.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Authorization": "Bearer t"},
        )
    assert r.status_code == 202
    assert r.content == b""
    methods = []
    for call in proc.stdin.write.call_args_list:
        try:
            methods.append(json.loads(call.args[0].decode()).get("method"))
        except Exception:
            pass
    assert "notifications/initialized" in methods


@pytest.mark.asyncio
async def test_sidecar_reuses_subprocess_for_same_session(client):
    """Two requests with the same Mcp-Session-Id reuse one subprocess; the
    initialize handshake only fires on the first."""
    proc = _build_mock_proc([
        _INIT_RESP,
        b'{"jsonrpc":"2.0","id":1,"result":"a"}\n',
        b'{"jsonrpc":"2.0","id":2,"result":"b"}\n',
    ])
    with patch(
        "sidecar.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ) as spawn:
        r1 = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "x"},
            headers={"Authorization": "Bearer same", "Mcp-Session-Id": "sess-1"},
        )
        r2 = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "y"},
            headers={"Authorization": "Bearer same", "Mcp-Session-Id": "sess-1"},
        )
    assert r1.status_code == r2.status_code == 200
    assert spawn.call_count == 1
    assert r1.json()["result"] == "a"
    assert r2.json()["result"] == "b"


@pytest.mark.asyncio
async def test_sidecar_bearer_change_restarts_subprocess(client):
    """Same session id with a different bearer = token rotated upstream;
    sidecar tears down the old child and spawns a new one with the new env."""
    proc_a = _build_mock_proc([
        _INIT_RESP, b'{"jsonrpc":"2.0","id":1,"result":"old"}\n',
    ])
    proc_b = _build_mock_proc([
        _INIT_RESP, b'{"jsonrpc":"2.0","id":2,"result":"new"}\n',
    ])
    spawn_mock = AsyncMock(side_effect=[proc_a, proc_b])
    with patch("sidecar.asyncio.create_subprocess_exec", spawn_mock):
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "x"},
            headers={"Authorization": "Bearer old-token", "Mcp-Session-Id": "sess-r"},
        )
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "y"},
            headers={"Authorization": "Bearer new-token", "Mcp-Session-Id": "sess-r"},
        )
    assert spawn_mock.call_count == 2
    proc_a.terminate.assert_called_once()
    _, new_kwargs = spawn_mock.call_args_list[1]
    assert new_kwargs["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "new-token"


@pytest.mark.asyncio
async def test_sidecar_delete_tears_down_session(client):
    """DELETE /mcp with a session id kills the subprocess + drops the entry."""
    proc = _build_mock_proc([
        _INIT_RESP, b'{"jsonrpc":"2.0","id":1,"result":"x"}\n',
    ])
    with patch(
        "sidecar.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
    ):
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "x"},
            headers={"Authorization": "Bearer t", "Mcp-Session-Id": "sess-d"},
        )
        assert "sess-d" in sidecar.SESSIONS
        r = await client.delete(
            "/mcp", headers={"Mcp-Session-Id": "sess-d"}
        )
    assert r.status_code == 204
    assert "sess-d" not in sidecar.SESSIONS
    proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_sidecar_health_endpoint(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "sessions" in body
