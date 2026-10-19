"""Production native HTTP uses one transport policy and explicit overrides."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from lib.tasks_pkg.native_http_policy import compile_native_http_policy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('idle_connections', [2, 512, 8192])
def test_production_policy_tracks_owner_capacity_and_phase_budgets(monkeypatch, idle_connections):
    from lib.llm import _transport as owner
    monkeypatch.setattr(owner, 'LLM_MAX_KEEPALIVE_CONNECTIONS', idle_connections)
    monkeypatch.setattr(owner, 'LLM_KEEPALIVE_EXPIRY_S', 47)
    monkeypatch.setattr(owner, 'CONNECT_TIMEOUT', 2.0001)
    monkeypatch.setattr(owner, 'WRITE_TIMEOUT', 71)
    monkeypatch.setattr(owner, 'IDLE_STREAM_TIMEOUT_S', 311)
    monkeypatch.setattr(owner, 'POST_TERMINAL_DRAIN_TIMEOUT_S', 0.123)
    monkeypatch.setattr(owner, 'POST_TERMINAL_DRAIN_MAX_BYTES', 98765)
    monkeypatch.setattr(owner, 'POST_TERMINAL_DRAIN_MAX_CHUNKS', 43)
    # Import the owner before poisoning probe/client entry: compiling a policy
    # must neither re-probe the host nor construct a network client.
    probe = Mock(side_effect=AssertionError('policy repeated launch probing'))
    monkeypatch.setattr('runtime_guards.deployment_resource_default', probe)
    monkeypatch.setattr(owner, 'deployment_resource_default', probe)
    result = compile_native_http_policy()
    assert result == dict(connections=idle_connections, idle_connections=idle_connections,
        keepalive_ms=47000, connect_ms=2001, request_headers_ms=None, write_ms=71000,
        pool_wait_ms=2001, stream_idle_ms=311000, drain_timeout_ms=123,
        drain_bytes=98765, drain_chunks=43)
    probe.assert_not_called()


def test_explicit_overrides_are_detached_and_disabled_idle_remains_unbounded(monkeypatch):
    from lib.llm import _transport as owner
    monkeypatch.setattr(owner, 'IDLE_STREAM_TIMEOUT_S', 0)
    source = dict(idle_connections=0, write_ms=None, request_headers_ms=1234,
                  drain_bytes=100, pool_wait_ms=500)
    before = deepcopy(source)
    result = compile_native_http_policy(source)
    assert result['stream_idle_ms'] is None
    assert all(result[key] == value for key, value in source.items())
    assert source == before
    result['write_ms'] = 1
    assert compile_native_http_policy(source)['write_ms'] is None
    assert compile_native_http_policy()['write_ms'] == 60000


@pytest.mark.parametrize('overrides', [[], {'typo': 4}, {'connect_ms': None},
    {'connections': 0}, {'idle_connections': -1}, {'write_ms': False},
    {'request_headers_ms': 0}, {'stream_idle_ms': -1}, {'drain_bytes': True},
    {'keepalive_ms': 1.5}, {'drain_chunks': 1 << 64}])
def test_invalid_explicit_policy_refuses(overrides):
    with pytest.raises(ValueError):
        compile_native_http_policy(overrides)


@pytest.mark.parametrize('seconds', [float('nan'), float('inf'), 0, -1, True, 1e308, 10**1000])
def test_invalid_owner_duration_refuses(monkeypatch, seconds):
    from lib.llm import _transport as owner
    monkeypatch.setattr(owner, 'CONNECT_TIMEOUT', seconds)
    with pytest.raises(ValueError):
        compile_native_http_policy()
