"""Reconnect is not permission to repeat an ambiguously completed operation."""
from __future__ import annotations

import threading
from concurrent.futures import CancelledError

import pytest

from tests.mcp_protocol.test_mcp_call_health import _make_bridge_with_server

pytestmark = pytest.mark.unit


def disconnected_bridge(monkeypatch, *, read_only=True):
    bridge, name = _make_bridge_with_server()
    info = bridge._tool_index[name]
    info.update(read_only_hint=read_only, schema_hash='original-declaration')
    calls, reconnects = [], []

    def run(coro, timeout=None, **kwargs):
        coro.close()
        calls.append(name)
        if len(calls) == 1:
            raise ConnectionResetError('reply lost after request delivery')
        return 'recovered response'

    def reconnect(server):
        reconnects.append(server)
        return bridge._servers[server]

    monkeypatch.setattr(bridge, '_run_async', run)
    monkeypatch.setattr(bridge, '_reconnect_server', reconnect)
    return bridge, name, calls, reconnects


@pytest.mark.parametrize('read_only', [True, False, None, 'true', 1])
def test_automatic_replay_requires_an_explicit_read_declaration(
        read_only, monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(
        monkeypatch, read_only=read_only)
    if read_only is True:
        assert bridge.call_tool(name, {}) == 'recovered response'
        assert len(calls) == 2 and reconnects == ['hope']
    else:
        with pytest.raises(ConnectionResetError):
            bridge.call_tool(name, {})
        assert len(calls) == 1 and reconnects == []


@pytest.mark.parametrize('policy', [True, False])
def test_explicit_host_replay_policy_remains_authoritative(policy, monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(
        monkeypatch, read_only=not policy)
    if policy:
        assert bridge.call_tool(name, {}, retry_on_disconnect=True) == 'recovered response'
        assert len(calls) == 2 and reconnects == ['hope']
    else:
        with pytest.raises(ConnectionResetError):
            bridge.call_tool(name, {}, retry_on_disconnect=False)
        assert len(calls) == 1 and reconnects == []


@pytest.mark.parametrize('change', ['permission', 'schema', 'target', 'removed', 'disabled'])
def test_reconnect_rechecks_the_original_read_declaration(change, monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(monkeypatch)
    disabled = set()
    monkeypatch.setattr(bridge, '_disabled_tools_for', lambda _server: disabled)

    def reconnect(server):
        reconnects.append(server)
        info = bridge._tool_index[name]
        if change == 'permission':
            info['read_only_hint'] = False
        elif change == 'schema':
            info['schema_hash'] = 'changed-declaration'
        elif change == 'target':
            info['tool_name'] = 'another_operation'
        elif change == 'removed':
            bridge._tool_index.pop(name)
        else:
            disabled.add(info['tool_name'])
        return bridge._servers[server]

    monkeypatch.setattr(bridge, '_reconnect_server', reconnect)
    with pytest.raises(ConnectionResetError):
        bridge.call_tool(name, {})
    assert len(calls) == 1 and reconnects == ['hope']


def test_missing_declaration_fingerprint_does_not_grant_automatic_replay(monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(monkeypatch)
    bridge._tool_index[name].pop('schema_hash')
    with pytest.raises(ConnectionResetError):
        bridge.call_tool(name, {})
    assert len(calls) == 1 and reconnects == []


def test_owner_cancellation_during_reconnect_prevents_replay(monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(monkeypatch)
    cancelled = threading.Event()

    def reconnect(server):
        reconnects.append(server)
        cancelled.set()
        return bridge._servers[server]

    monkeypatch.setattr(bridge, '_reconnect_server', reconnect)
    with pytest.raises(CancelledError):
        bridge.call_tool(name, {}, cancel=cancelled)
    assert len(calls) == 1 and reconnects == ['hope']
    assert not bridge._timeout_streak


@pytest.mark.parametrize('value', ['false', 0, 1, [], {}])
def test_replay_override_is_a_host_boolean_or_absent(value, monkeypatch):
    bridge, name, calls, reconnects = disconnected_bridge(monkeypatch)
    with pytest.raises(ValueError, match='retry_on_disconnect'):
        bridge.call_tool(name, {}, retry_on_disconnect=value)
    assert calls == [] and reconnects == []


@pytest.mark.parametrize('final_failure', ['timeout', 'peer_error'])
def test_retried_read_settles_health_once_without_a_third_execution(
        final_failure, monkeypatch):
    from tests.mcp_protocol.test_mcp_recovery_classification import protocol_error

    bridge, name, calls, reconnects = disconnected_bridge(monkeypatch)
    bridge._timeout_streak['hope'] = 1
    error = protocol_error(
        -32001 if final_failure == 'timeout' else -32602,
        "Request 'tools/call' timed out" if final_failure == 'timeout' else 'Correct the query.',
    )

    def run(coro, timeout=None):
        coro.close()
        calls.append(name)
        if len(calls) == 1:
            raise ConnectionResetError('transport closed')
        raise error

    monkeypatch.setattr(bridge, '_run_async', run)
    with pytest.raises(type(error)) as caught:
        bridge.call_tool(name, {})
    assert caught.value is error
    assert len(calls) == 2 and reconnects == ['hope']
    assert bridge._timeout_streak.get('hope', 0) == (
        2 if final_failure == 'timeout' else 0)
