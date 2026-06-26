"""tests/test_mcp_call_health.py — MCP call-level health gating + timeout plumbing.

Covers the fix for the 1135 ``McpError: Timed out … Waited 120.0 seconds``
events dominated by ``hope.watch_job`` (a long-poll tool whose own budget
exceeds the global MCP_CALL_TIMEOUT):

  * ``_run_async`` honors a per-call timeout instead of the hardcoded global,
    so a server with a longer per-server ``timeout`` isn't killed early.
  * The hope registry card ships a default ``timeout=360``.
  * A server that times out ``MCP_DEGRADED_TIMEOUT_STREAK`` times in a row is
    marked degraded and the next call fast-fails instead of blocking again.
  * Any successful call resets the streak.
  * ``_is_call_timeout_error`` distinguishes timeouts from tool-level errors.

Run:  pytest tests/test_mcp_call_health.py -m unit
"""
from __future__ import annotations

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge, _is_call_timeout_error
from lib.mcp.registry import is_opensource_build
from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK

pytestmark = pytest.mark.unit


class _FakeSession:
    pass


def _make_bridge_with_server(name='hope', timeout=360):
    """Build a bridge with one fake connected server + a tool index entry."""
    bridge = MCPBridge()
    handle = mc._MCPServerHandle(name, {'timeout': timeout})
    handle.session = _FakeSession()
    bridge._servers[name] = handle
    namespaced = f'mcp__{name}__watch_job'
    bridge._tool_index[namespaced] = {
        'server_name': name, 'tool_name': 'watch_job',
        'namespaced_name': namespaced, 'description': '', 'input_schema': {},
        'openai_def': {}, 'read_only_hint': False,
    }
    return bridge, namespaced


# ── _is_call_timeout_error ───────────────────────────────

def test_is_call_timeout_error_matches_timeout():
    assert _is_call_timeout_error(TimeoutError('boom'))
    assert _is_call_timeout_error(
        TimeoutError('Timed out while waiting for response to ClientRequest'))


def test_is_call_timeout_error_rejects_tool_error():
    assert not _is_call_timeout_error(ValueError('bad args'))
    assert not _is_call_timeout_error(KeyError('convId'))


# ── Per-server timeout plumbing ──────────────────────────

def test_run_async_passes_per_server_timeout(monkeypatch):
    """call_tool must drive _run_async with the per-server timeout + headroom,
    not the hardcoded global cap."""
    bridge, namespaced = _make_bridge_with_server(timeout=360)
    seen = {}

    def _fake_run_async(coro, timeout=None):
        seen['timeout'] = timeout
        # The coroutine is never awaited; close it to avoid a warning.
        coro.close()
        return 'OK'

    monkeypatch.setattr(bridge, '_run_async', _fake_run_async)
    out = bridge.call_tool(namespaced, {})
    assert out == 'OK'
    # 360 (per-server) + 10 headroom — NOT the 120+10 global default.
    assert seen['timeout'] == 370


@pytest.mark.skipif(
    is_opensource_build(),
    reason='hope is a Meituan-internal MCP server, stripped from opensource builds',
)
def test_hope_card_ships_360s_default():
    from lib.mcp.registry import build_server_config
    cfg = build_server_config('hope', {'HOPE_USERNAME': 'x'})
    assert cfg is not None
    assert cfg.get('timeout') == 360


# ── Degraded health gate ─────────────────────────────────

def test_degraded_gate_fast_fails_after_streak(monkeypatch):
    bridge, namespaced = _make_bridge_with_server()
    calls = {'n': 0}

    def _always_timeout(coro, timeout=None):
        calls['n'] += 1
        coro.close()
        raise TimeoutError('Timed out while waiting for response')

    monkeypatch.setattr(bridge, '_run_async', _always_timeout)

    # First MCP_DEGRADED_TIMEOUT_STREAK calls actually attempt and raise.
    for _ in range(MCP_DEGRADED_TIMEOUT_STREAK):
        with pytest.raises(TimeoutError):
            bridge.call_tool(namespaced, {})
    assert calls['n'] == MCP_DEGRADED_TIMEOUT_STREAK

    # The next call is GATED — fast-fails without invoking _run_async.
    out = bridge.call_tool(namespaced, {})
    assert calls['n'] == MCP_DEGRADED_TIMEOUT_STREAK  # not incremented
    assert 'degraded' in out.lower()
    assert 'MCP Error' in out


def test_success_resets_streak(monkeypatch):
    bridge, namespaced = _make_bridge_with_server()
    state = {'mode': 'timeout'}

    def _toggle(coro, timeout=None):
        coro.close()
        if state['mode'] == 'timeout':
            raise TimeoutError('Timed out while waiting for response')
        return 'OK'

    monkeypatch.setattr(bridge, '_run_async', _toggle)

    # Accumulate 2 timeouts (below the degrade threshold of 3).
    for _ in range(2):
        with pytest.raises(TimeoutError):
            bridge.call_tool(namespaced, {})
    assert bridge._timeout_streak.get('hope') == 2

    # A success clears the streak entirely.
    state['mode'] = 'ok'
    assert bridge.call_tool(namespaced, {}) == 'OK'
    assert 'hope' not in bridge._timeout_streak

    # And the gate is no longer armed — further timeouts start from zero.
    state['mode'] = 'timeout'
    with pytest.raises(TimeoutError):
        bridge.call_tool(namespaced, {})
    assert bridge._timeout_streak.get('hope') == 1


def test_tool_level_error_does_not_count_as_timeout(monkeypatch):
    """A genuine tool error must not bump the degraded streak."""
    bridge, namespaced = _make_bridge_with_server()

    def _tool_error(coro, timeout=None):
        coro.close()
        raise ValueError('bad args')

    monkeypatch.setattr(bridge, '_run_async', _tool_error)
    for _ in range(MCP_DEGRADED_TIMEOUT_STREAK + 2):
        with pytest.raises(ValueError):
            bridge.call_tool(namespaced, {})
    # Never marked degraded — streak stays empty.
    assert 'hope' not in bridge._timeout_streak
