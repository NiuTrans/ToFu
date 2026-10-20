"""Protocol codes preserve recovery/liveness even when message wording changes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.mcp.client._errors import (
    _EXCEPTION_GROUP,
    _is_call_timeout_error,
    _is_peer_answered_error,
    _is_transport_dead_error,
    _jsonrpc_error_code,
)
from lib.mcp.types import MCP_DEGRADED_TIMEOUT_STREAK
from tests.mcp_protocol.test_mcp_call_health import _make_bridge_with_server

pytestmark = pytest.mark.unit


def protocol_error(code, message):
    """Construct the installed SDK's actual error, across supported majors."""
    from mcp.shared import exceptions
    import mcp.types

    cls = getattr(exceptions, 'MCPError', None) or exceptions.McpError
    try:
        return cls(code, message)
    except TypeError:
        return cls(mcp.types.ErrorData(code=code, message=message))


@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('code,message,timeout,dead', [
    (-32602, 'Invalid timeout parameter: expected a positive integer.', False, False),
    (-32601, 'Unknown method connection closed.', False, False),
    (-32042, 'Upstream connection closed after business timeout.', False, False),
    (-32001, "Request 'tools/call' timed out", True, False),
    (-32001, '等待响应超时', True, False),
    (-32000, '连接已关闭', False, True),
    (-32000, 'Connection closed during timeout handling.', False, True),
])
def test_structured_protocol_code_outranks_error_prose(
        wrapped, code, message, timeout, dead):
    error = protocol_error(code, message)
    if wrapped:
        if not isinstance(_EXCEPTION_GROUP, type):
            pytest.skip('Exception groups unavailable on this interpreter')
        error = _EXCEPTION_GROUP('outer', [
            _EXCEPTION_GROUP('transport wrapper', [error]),
        ])
    assert _jsonrpc_error_code(error) == code
    assert _is_call_timeout_error(error) is timeout
    assert _is_transport_dead_error(error) is dead
    assert _is_peer_answered_error(error) is (not timeout and not dead)


@pytest.mark.parametrize('code', [True, False, '-32001', -32001.0])
def test_malformed_codes_cannot_establish_a_protocol_reply(code):
    error = RuntimeError('unclassified failure')
    error.error = SimpleNamespace(code=code)
    assert _jsonrpc_error_code(error) is None
    assert _is_peer_answered_error(error) is False


def test_current_sdk_timeout_wording_drives_bounded_health_gate(monkeypatch):
    from mcp.types import REQUEST_TIMEOUT

    bridge, name = _make_bridge_with_server()
    calls = []

    def fail(coro, timeout=None):
        coro.close()
        calls.append(name)
        raise protocol_error(REQUEST_TIMEOUT, "Request 'tools/call' timed out")

    monkeypatch.setattr(bridge, '_run_async', fail)
    for index in range(MCP_DEGRADED_TIMEOUT_STREAK):
        with pytest.raises(Exception, match='timed out'):
            bridge.call_tool(name, {})
        assert bridge._timeout_streak.get('hope') == index + 1
    result = bridge.call_tool(name, {})
    assert 'degraded' in result.lower()
    assert len(calls) == MCP_DEGRADED_TIMEOUT_STREAK


@pytest.mark.parametrize('message', [
    'Invalid timeout parameter: expected a positive integer.',
    'Invalid query: connection closed is not a supported filter.',
])
def test_answered_parameter_errors_neither_reconnect_nor_block_correction(
        message, monkeypatch):
    bridge, name = _make_bridge_with_server()
    calls, reconnects = [], []
    state = {'corrected': False}
    error = protocol_error(-32602, message)

    def respond(coro, timeout=None):
        coro.close()
        calls.append(name)
        if state['corrected']:
            return 'corrected response'
        raise error

    def reconnect(server):
        reconnects.append(server)
        return bridge._servers[server]

    monkeypatch.setattr(bridge, '_run_async', respond)
    monkeypatch.setattr(bridge, '_reconnect_server', reconnect)
    for _ in range(MCP_DEGRADED_TIMEOUT_STREAK + 1):
        with pytest.raises(type(error)) as caught:
            bridge.call_tool(name, {})
        assert caught.value is error
    assert len(calls) == MCP_DEGRADED_TIMEOUT_STREAK + 1
    assert reconnects == []
    assert 'hope' not in bridge._timeout_streak
    state['corrected'] = True
    assert bridge.call_tool(name, {'timeout': 60}) == 'corrected response'


def test_peer_answer_breaks_a_prior_timeout_streak(monkeypatch):
    bridge, name = _make_bridge_with_server()
    bridge._timeout_streak['hope'] = MCP_DEGRADED_TIMEOUT_STREAK - 1
    error = protocol_error(-32602, 'Correct the timeout argument.')
    state = {'timeout': False}

    def respond(coro, timeout=None):
        coro.close()
        if state['timeout']:
            raise TimeoutError('later independent deadline')
        raise error

    monkeypatch.setattr(bridge, '_run_async', respond)
    with pytest.raises(type(error)):
        bridge.call_tool(name, {})
    assert 'hope' not in bridge._timeout_streak, 'a peer response proves the timeout streak ended'
    state['timeout'] = True
    with pytest.raises(TimeoutError):
        bridge.call_tool(name, {})
    assert bridge._timeout_streak['hope'] == 1


def test_sdk_timeout_without_configured_deadline_remains_the_primary_failure(
        monkeypatch, caplog):
    bridge, name = _make_bridge_with_server(timeout=None)
    error = protocol_error(-32001, "Request 'tools/call' timed out")

    def fail(coro, timeout=None):
        coro.close()
        raise error

    monkeypatch.setattr(bridge, '_run_async', fail)
    with pytest.raises(type(error)) as caught:
        bridge.call_tool(name, {})
    assert caught.value is error
    assert bridge._timeout_streak['hope'] == 1
    assert 'budget=none' in caplog.text
    for _ in range(MCP_DEGRADED_TIMEOUT_STREAK - 1):
        with pytest.raises(type(error)):
            bridge.call_tool(name, {})
    assert 'configured call budget: none' in bridge.call_tool(name, {})
