"""Contracts for bounded cleanup inside Quart's shutdown lifespan."""

from __future__ import annotations

import asyncio
import importlib
import threading

import pytest

from lib.server_shutdown import shutdown_production_runtime


pytestmark = pytest.mark.unit


def _run_async(awaitable):
    """Run an awaitable despite a loop marker left by a browser fixture."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result = []
    failure = []

    def runner():
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=runner, name='shutdown-test-loop')
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), 'isolated shutdown test loop did not finish'
    if failure:
        raise failure[0]
    return result[0] if result else None


def test_shutdown_closes_all_runtime_owners_and_offloads_sync_joins(monkeypatch):
    import lib.agent_core.admission as admission
    import lib.agent_core.push as push
    import lib.billing.janitor as billing_janitor
    import lib.cgroup_guard as cgroup_guard
    import lib.fs_keepalive as fs_keepalive
    import lib.http_client as http_client
    import lib.llm_dispatch.autodiscover_local as autodiscover_local
    import lib.llm_dispatch.health_local as health_local
    import lib.mcp.client as mcp_client
    import lib.mcp.startup as mcp_startup
    import lib.netpath as netpath
    import lib.presence as presence
    import lib.pricing as pricing
    import lib.runtime_state_store as runtime_state_store
    import lib.storage as storage
    import lib.tasks_pkg as tasks_pkg
    import lib.tasks_pkg.event_log as event_log
    import routes

    calls = []
    serving_loop_threads = []

    def sync_call(name):
        def run(*args, **kwargs):
            calls.append((name, threading.get_ident(), kwargs))
        return run

    async def close_http_clients():
        calls.append(('http', threading.get_ident(), {}))

    class Store:
        close = sync_call('runtime')

    class Bridge:
        disconnect_all = sync_call('mcp')

    monkeypatch.setattr(tasks_pkg, 'quiesce_running_tasks', lambda **_kwargs: 0)
    monkeypatch.setattr(billing_janitor, 'stop_janitor', sync_call('janitor'))
    monkeypatch.setattr(netpath, 'stop_prober', sync_call('netpath'))
    monkeypatch.setattr(cgroup_guard, 'stop_monitor', sync_call('cgroup'))
    monkeypatch.setattr(
        health_local, 'stop_local_health_checker', sync_call('local-health'))
    monkeypatch.setattr(
        autodiscover_local, 'stop_local_autodiscovery',
        sync_call('local-autodiscovery'))
    monkeypatch.setattr(
        fs_keepalive, 'stop_fs_keepalive', sync_call('fs-keepalive'))
    monkeypatch.setattr(presence, 'stop_sweeper', sync_call('presence'))
    monkeypatch.setattr(
        pricing, 'stop_pricing_refresh', sync_call('pricing-refresh'))
    monkeypatch.setattr(
        routes, 'stop_registered_background_services',
        sync_call('route-services'))
    monkeypatch.setattr(mcp_client, 'get_bridge', lambda: Bridge())
    monkeypatch.setattr(
        mcp_startup, 'stop_mcp_auto_connect', sync_call('mcp-startup'))
    monkeypatch.setattr(admission.controller, 'shutdown', sync_call('admission'))
    monkeypatch.setattr(push.hub, 'stop', sync_call('push'))
    monkeypatch.setattr(http_client, 'close_http_clients', close_http_clients)
    monkeypatch.setattr(runtime_state_store, 'get_store', lambda: Store())
    monkeypatch.setattr(
        event_log, 'stop_storage_maintenance', sync_call('event-maintenance'))
    monkeypatch.setattr(event_log, 'stop_event_writer', sync_call('event-writer'))
    monkeypatch.setattr(storage, 'stop_storage', sync_call('storage'))

    stopped = threading.Event()
    stopped.set()
    app = object()

    async def exercise():
        serving_loop_threads.append(threading.get_ident())
        await shutdown_production_runtime(stopped, app=app)

    _run_async(exercise())
    loop_thread = serving_loop_threads[0]

    assert [name for name, _, _ in calls] == [
        'janitor', 'netpath', 'cgroup', 'local-health',
        'local-autodiscovery', 'fs-keepalive', 'presence', 'route-services',
        'pricing-refresh', 'mcp-startup', 'mcp', 'admission', 'push', 'http', 'runtime',
        'event-maintenance', 'event-writer', 'storage',
    ]
    assert next(thread for name, thread, _ in calls if name == 'http') == loop_thread
    for name, thread, _ in calls:
        if name != 'http':
            assert thread != loop_thread
    assert calls[-3][2] == {'timeout': 2.0}
    assert calls[-1][2] == {'timeout': 5.0}
    assert calls[-2][2] == {'timeout': 3.0}
    assert calls[0][2] == {'timeout': 2.5}
    assert all(kwargs == {'timeout': 2.0} for _, _, kwargs in calls[2:6])
    assert calls[6][2] == {'timeout': 2.0}
    assert calls[7][2] == {'timeout': 2.0}
    assert calls[8][2] == {'timeout': 2.0}


def test_billing_janitor_stop_is_bounded_and_releases_thread_owner(monkeypatch):
    import lib.billing.janitor as janitor

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    thread = Thread()
    monkeypatch.setattr(janitor, '_thread', thread)
    janitor._stop.clear()

    assert janitor.stop_janitor(timeout=0.25) is True
    assert janitor._stop.is_set()
    assert thread.joined == [0.25]
    assert janitor._thread is None


@pytest.mark.parametrize(('module_name', 'thread_name', 'event_names', 'stop_name'), [
    ('lib.cgroup_guard', '_monitor_thread', ('_monitor_stop',), 'stop_monitor'),
    ('lib.daily_report.scheduler', '_scheduler_thread', ('_scheduler_stop',),
     'stop_report_scheduler'),
    ('lib.fs_keepalive', '_thread', (), 'stop_fs_keepalive'),
    ('lib.llm_dispatch.autodiscover_local', '_thread', ('_stop_event',),
     'stop_local_autodiscovery'),
    ('lib.llm_dispatch.health_local', '_thread', ('_stop_event',),
     'stop_local_health_checker'),
    ('lib.llm_dispatch.model_catalog_sync', '_thread',
     ('_stop_event', '_wake_event'), 'stop_model_catalog_sync'),
    ('lib.knowledge.enrichment', '_worker', ('_worker_stop',),
     'stop_visual_enrichment'),
    ('lib.oauth.codex_catalog', '_worker_thread',
     ('_worker_stop', '_refresh_wake'), 'stop_codex_catalog_refresher'),
    ('lib.presence.registry', '_sweeper_thread', ('_sweeper_stop',),
     'stop_sweeper'),
])
def test_worker_stop_contract_releases_only_a_stopped_owner(
        monkeypatch, module_name, thread_name, event_names, stop_name):
    module = importlib.import_module(module_name)

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    thread = Thread()
    monkeypatch.setattr(module, thread_name, thread)
    events = []
    for event_name in event_names:
        event = threading.Event()
        events.append(event)
        monkeypatch.setattr(module, event_name, event)
    if module_name == 'lib.fs_keepalive':
        monkeypatch.setattr(module, '_running', True)
    if module_name == 'lib.oauth.codex_catalog':
        monkeypatch.setattr(module, '_worker_started', True)
    if module_name == 'lib.presence.registry':
        monkeypatch.setattr(module, '_sweeper_started', True)

    assert getattr(module, stop_name)(timeout=0.125) is True
    assert thread.joined == [0.125]
    assert getattr(module, thread_name) is None
    assert all(event.is_set() for event in events)
    if module_name == 'lib.fs_keepalive':
        assert module._running is False
    if module_name == 'lib.oauth.codex_catalog':
        assert module._worker_started is False
    if module_name == 'lib.presence.registry':
        assert module._sweeper_started is False


def test_scheduler_manager_stop_interrupts_wait_and_releases_owner():
    from lib.scheduler.manager import ScheduledTaskManager

    class Thread:
        alive = True
        joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    manager = ScheduledTaskManager.__new__(ScheduledTaskManager)
    manager._running = True
    manager._stop_event = threading.Event()
    manager._thread = Thread()

    assert manager.stop(timeout=0.25) is True
    assert manager._running is False
    assert manager._stop_event.is_set()
    assert manager._thread is None
