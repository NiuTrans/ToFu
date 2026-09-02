"""Contracts for bounded cleanup inside Quart's shutdown lifespan."""

from __future__ import annotations

import asyncio
import importlib
import threading

import pytest

from lib.server_shutdown import (
    _stop_storage_boundary_for_shutdown,
    shutdown_production_runtime,
)


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
    import lib.cgroup_guard as cgroup_guard
    import lib.fs_keepalive as fs_keepalive
    import lib.http_client as http_client
    import lib.integration_control as integration_control
    import lib.llm_dispatch.health_local as health_local
    import lib.mcp.client as mcp_client
    import lib.mcp.startup as mcp_startup
    import lib.netpath as netpath
    import lib.presence as presence
    import lib.pricing as pricing
    import lib.runtime_state_store as runtime_state_store
    import lib.server_background_services as background_services
    import lib.storage as storage
    import lib.swarm.integration as swarm_integration
    import lib.tasks_pkg.manager as task_manager
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

    monkeypatch.setattr(
        task_manager, 'quiesce_running_tasks', lambda **_kwargs: 0)
    monkeypatch.setattr(
        swarm_integration, 'stop_swarm_cleanup_timer',
        sync_call('swarm-cleanup'))
    monkeypatch.setattr(netpath, 'stop_prober', sync_call('netpath'))
    monkeypatch.setattr(
        background_services, 'stop_lan_discovery_responder',
        sync_call('lan-discovery'))
    monkeypatch.setattr(cgroup_guard, 'stop_monitor', sync_call('cgroup'))
    monkeypatch.setattr(
        health_local, 'stop_local_health_checker', sync_call('local-health'))
    monkeypatch.setattr(
        fs_keepalive, 'stop_fs_keepalive', sync_call('fs-keepalive'))
    monkeypatch.setattr(presence, 'stop_sweeper', sync_call('presence'))
    monkeypatch.setattr(
        integration_control, 'stop_worker', sync_call('integration'))
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
    monkeypatch.setattr(event_log, 'stop_sidecar_batcher',
                        sync_call('event-batcher'))
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
        'swarm-cleanup', 'netpath', 'lan-discovery', 'cgroup', 'local-health',
        'fs-keepalive', 'presence', 'integration', 'route-services', 'pricing-refresh',
        'mcp-startup', 'mcp', 'admission', 'push', 'http', 'runtime',
        'event-maintenance', 'event-batcher', 'storage',
    ]
    assert next(thread for name, thread, _ in calls if name == 'http') == loop_thread
    for name, thread, _ in calls:
        if name != 'http':
            assert thread != loop_thread
    assert calls[-3][2] == {'timeout': 2.0}
    assert calls[-1][2] == {'timeout': 5.0}
    assert calls[-2][2] == {'timeout': 3.0}
    assert calls[0][2] == {'timeout': 2.0}
    assert calls[1][2] == {}
    assert all(kwargs == {'timeout': 2.0} for _, _, kwargs in calls[2:7])
    assert all(kwargs == {'timeout': 2.0} for _, _, kwargs in calls[7:11])


def test_storage_shutdown_certifies_reexec_only_after_release(monkeypatch):
    import lib.server_reexec as server_reexec
    import lib.storage as storage

    calls = []

    def stop_storage(*, timeout):
        calls.append(('stop', timeout))

    def certify():
        calls.append(('certify', None))
        return True

    class Log:
        def info(self, *_args):
            calls.append(('log', None))

        def warning(self, *_args):
            raise AssertionError('successful release must not warn')

    monkeypatch.setattr(storage, 'stop_storage', stop_storage)
    monkeypatch.setattr(
        server_reexec,
        'confirm_server_reexec_storage_boundary_released',
        certify,
    )

    assert _run_async(_stop_storage_boundary_for_shutdown(Log())) is True
    assert calls[:2] == [('stop', 5.0), ('certify', None)]


def test_storage_shutdown_failure_cannot_certify_reexec(monkeypatch):
    import lib.server_reexec as server_reexec
    import lib.storage as storage

    certified = []
    warnings = []

    def fail_stop(*, timeout):
        raise RuntimeError(f'still alive after {timeout}s')

    class Log:
        def info(self, *_args):
            raise AssertionError('failed release must not report success')

        def warning(self, *args):
            warnings.append(args)

    monkeypatch.setattr(storage, 'stop_storage', fail_stop)
    monkeypatch.setattr(
        server_reexec,
        'confirm_server_reexec_storage_boundary_released',
        lambda: certified.append(True) or True,
    )

    assert _run_async(_stop_storage_boundary_for_shutdown(Log())) is False
    assert certified == []
    assert warnings


def test_legacy_billing_janitor_facade_is_thread_free():
    import lib.billing.janitor as janitor

    assert janitor.start_janitor() is False
    assert janitor.stop_janitor(timeout=0.25) is True


def test_legacy_daily_report_scheduler_facade_is_thread_free(monkeypatch):
    import lib.daily_report.scheduler as scheduler
    import lib.scheduler.manager as manager
    from lib.identity import PrincipalContext

    monkeypatch.setattr(
        manager, 'ensure_daily_report_schedule', lambda **_kwargs: False)
    principal = PrincipalContext.system(
        subject_id='daily-report-test',
        owner_user_id=1,
        scopes={'reports:maintain'},
    )
    assert scheduler.start_report_scheduler(principal=principal) is False
    assert scheduler.stop_report_scheduler(timeout=0.25) is True
    assert not hasattr(scheduler, '_scheduler_thread')


@pytest.mark.parametrize(('module_name', 'thread_name', 'event_names', 'stop_name'), [
    ('lib.cgroup_guard', '_monitor_thread', ('_monitor_stop',), 'stop_monitor'),
    ('lib.fs_keepalive', '_thread', ('_stop_event',), 'stop_fs_keepalive'),
    ('lib.llm_dispatch.health_local', '_thread', ('_stop_event', '_wake_event'),
     'stop_local_health_checker'),
    ('lib.llm_dispatch.model_catalog_sync', '_thread',
     ('_stop_event', '_wake_event'), 'stop_model_catalog_sync'),
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
        monkeypatch.setattr(module, '_probe_runtime', None)
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


def test_visual_enrichment_stop_contract_releases_each_owner(monkeypatch):
    """The knowledge worker registry is partitioned by explicit owner."""
    from lib.knowledge import enrichment

    class Thread:
        alive = True

        def __init__(self):
            self.joined = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

    first = Thread()
    second = Thread()
    first_stop = threading.Event()
    second_stop = threading.Event()
    monkeypatch.setattr(enrichment, '_workers', {7: first, 9: second})
    monkeypatch.setattr(
        enrichment, '_worker_stops', {7: first_stop, 9: second_stop})

    assert enrichment.stop_visual_enrichment(timeout=0.125) is True
    assert first.joined == [0.0625]
    assert second.joined == [0.0625]
    assert first_stop.is_set() and second_stop.is_set()
    assert enrichment._workers == {}
    assert enrichment._worker_stops == {}


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
