"""Bounded idle lifecycle for local MCP stdio process trees.

Pins the resource-saving boundary: parking closes only the live transport,
keeps the authority catalog visible, never interrupts an active call, skips
remote transports, and transparently reconnects before the next tool call.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from lib.mcp.client import _bridge as bridge_module


pytestmark = pytest.mark.unit


def _bridge_with_local_server():
    bridge = bridge_module.MCPBridge()
    config = {
        'command': 'fake-mcp',
        'transport': 'stdio',
        'description': 'local test server',
    }
    handle = bridge_module._MCPServerHandle('local', config)
    handle.session = object()
    handle.tools = [SimpleNamespace(name='echo')]
    handle.server_name = 'fake-server'
    handle.server_version = '1.0'
    handle.protocol_version = '2026-07-28'
    handle.sdk_generation = 2
    bridge._servers['local'] = handle
    bridge._configs['local'] = dict(config)
    bridge._started = True
    namespaced = 'mcp__local__echo'
    bridge._tool_index[namespaced] = {
        'server_name': 'local',
        'tool_name': 'echo',
        'namespaced_name': namespaced,
        'description': 'Echo text',
        'input_schema': {'type': 'object', 'properties': {}},
        'openai_def': {
            'type': 'function',
            'function': {
                'name': namespaced,
                'description': 'Echo text',
                'parameters': {'type': 'object', 'properties': {}},
            },
        },
        'read_only_hint': True,
        'meta': {},
        'schema_hash': 'stable',
        'catalog_version': 'v1',
    }
    return bridge, handle, namespaced


def test_idle_stdio_parking_releases_transport_but_preserves_catalog(
        monkeypatch):
    bridge, handle, namespaced = _bridge_with_local_server()
    monkeypatch.setattr(bridge_module, 'MCP_STDIO_IDLE_SECONDS', 10)
    bridge._last_activity['local'] = time.monotonic() - 20

    def _finish_shutdown(coro, timeout):
        assert timeout == bridge._DISCONNECT_TIMEOUT
        return asyncio.run(coro)

    monkeypatch.setattr(bridge, '_run_async_with_timeout', _finish_shutdown)

    assert bridge._park_idle_stdio_server('local') is True
    assert bridge._servers['local'] is handle
    assert handle.session is None
    assert 'local' in bridge._parked
    assert bridge.connected is True
    assert bridge.get_tool_info(namespaced) is not None
    assert [row['function']['name']
            for row in bridge.get_openai_tool_defs()] == [namespaced]
    assert bridge.list_servers()[0]['parked'] is True


def test_idle_parking_skips_active_young_and_remote_servers(monkeypatch):
    bridge, handle, _ = _bridge_with_local_server()
    monkeypatch.setattr(bridge_module, 'MCP_STDIO_IDLE_SECONDS', 10)
    bridge._last_activity['local'] = time.monotonic() - 20
    bridge._active_calls['local'] = 1

    assert bridge._park_idle_stdio_server('local') is False
    assert handle.session is not None

    bridge._active_calls.clear()
    bridge._last_activity['local'] = time.monotonic()
    assert bridge._park_idle_stdio_server('local') is False

    handle.config = {'transport': 'streamable-http',
                     'url': 'https://example.invalid/mcp'}
    bridge._last_activity['local'] = time.monotonic() - 20
    assert bridge._park_idle_stdio_server('local') is False
    assert handle.session is not None


def test_maintenance_offload_releases_worker_without_loop_default_executor():
    bridge, _handle, _ = _bridge_with_local_server()
    worker_threads = []

    def _blocking_result():
        worker_threads.append(threading.current_thread())
        return 'done'

    async def _run():
        loop = asyncio.get_running_loop()
        assert await bridge._run_maintenance_blocking(
            _blocking_result) == 'done'
        assert getattr(loop, '_default_executor', None) is None

    asyncio.run(_run())
    assert len(worker_threads) == 1
    assert worker_threads[0].name.startswith('mcp-maintenance')
    deadline = time.monotonic() + 1
    while worker_threads[0].is_alive():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_credential_probe_thread_is_singleflight_and_exits(monkeypatch):
    bridge, _handle, _ = _bridge_with_local_server()
    entered = threading.Event()
    release = threading.Event()
    workers = []

    monkeypatch.setattr(
        bridge, '_cred_probe_spec',
        lambda _name: {'tool': 'echo', 'args': {}})

    def _probe(_name):
        workers.append(threading.current_thread())
        entered.set()
        release.wait(1)

    monkeypatch.setattr(bridge, '_run_cred_probe', _probe)
    try:
        assert bridge._probe_cred_health_async('local') is True
        assert entered.wait(1)
        assert bridge._probe_cred_health_async('local') is False
        assert bridge._cred_probe_due('local') is False
    finally:
        release.set()

    deadline = time.monotonic() + 1
    while bridge._cred_probe_inflight or workers[0].is_alive():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert len(workers) == 1


def test_call_transparently_reconnects_a_parked_catalog(monkeypatch):
    bridge, old_handle, namespaced = _bridge_with_local_server()
    old_handle.session = None
    bridge._parked.add('local')
    bridge._last_activity['local'] = time.monotonic() - 100
    reconnects = []

    def _reconnect(name):
        reconnects.append(name)
        fresh = bridge_module._MCPServerHandle(
            name, dict(bridge._configs[name]))
        fresh.session = object()
        bridge._servers[name] = fresh
        bridge._parked.discard(name)
        return fresh

    def _run_async(coro, timeout=None):
        coro.close()
        return 'OK'

    monkeypatch.setattr(bridge, '_reconnect_server', _reconnect)
    monkeypatch.setattr(bridge, '_run_async', _run_async)

    assert bridge.call_tool(namespaced, {}) == 'OK'
    assert reconnects == ['local']
    assert 'local' not in bridge._parked
    assert bridge._last_activity['local'] > 0


def test_async_call_activity_is_released_on_failure():
    bridge, handle, _ = _bridge_with_local_server()
    observed = []

    class _FailingSession:
        async def call_tool(self, *_args, **_kwargs):
            observed.append(bridge._active_calls.get('local'))
            raise RuntimeError('tool failed')

    handle.session = _FailingSession()
    before = time.monotonic()
    with pytest.raises(RuntimeError, match='tool failed'):
        asyncio.run(bridge._async_call_tool(handle, 'echo', {}, None))

    assert observed == [1]
    assert 'local' not in bridge._active_calls
    assert bridge._last_activity['local'] >= before


@pytest.mark.parametrize('keepalive_interval', [0, 0.01])
def test_maintenance_parks_before_optional_liveness_or_credential_work(
        monkeypatch, keepalive_interval):
    bridge, _handle, _ = _bridge_with_local_server()
    bridge._last_activity['local'] = time.monotonic() - 100
    parked = []
    monkeypatch.setattr(bridge_module, 'MCP_STDIO_IDLE_SECONDS', 0.01)
    monkeypatch.setattr(
        bridge_module, 'MCP_KEEPALIVE_INTERVAL', keepalive_interval)

    def _park(name):
        parked.append(name)
        with bridge._lock:
            bridge._parked.add(name)
        return True

    async def _unexpected_probe(*_args, **_kwargs):
        raise AssertionError('parked server must not be pinged')

    monkeypatch.setattr(bridge, '_park_idle_stdio_server', _park)
    monkeypatch.setattr(bridge, '_probe_liveness', _unexpected_probe)

    async def _run_one_sweep():
        bridge._keepalive_stop = asyncio.Event()
        task = asyncio.create_task(bridge._keepalive_loop())
        await asyncio.sleep(0.04)
        bridge._keepalive_stop.set()
        await task

    asyncio.run(_run_one_sweep())
    assert parked == ['local']
