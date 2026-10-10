"""Storage retention must start even when no new task emits an event."""

from __future__ import annotations

import inspect

import pytest

from lib.storage_sidecar.schema import TASK_EVENT_RETENTION_SPECS

pytestmark = pytest.mark.unit


def test_public_start_is_idempotent_singleton(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class Thread:
        def __init__(self):
            self.started = 0
            self.alive = False

        def start(self):
            self.started += 1
            self.alive = True

        def is_alive(self):
            return self.alive

    thread = Thread()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_THREAD', None)
    monkeypatch.setattr(event_log.threading, 'Thread',
                        lambda **_kwargs: thread)

    assert event_log.start_storage_maintenance() is thread
    assert event_log.start_storage_maintenance() is thread
    assert thread.started == 1


def test_serving_loop_eagerly_starts_storage_maintenance():
    import lib.production_lifecycle as production_lifecycle

    source = inspect.getsource(production_lifecycle)
    assert 'start_storage_maintenance()' in source


def test_sidecar_event_backlog_drains_separate_bounded_batches(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class Client:
        def __init__(self):
            self.calls = []
            self.results = [
                event_log._PRUNE_BATCH_ROWS,
                event_log._PRUNE_BATCH_ROWS,
                3,
            ]

        def command(self, operation, payload, command_id, **kwargs):
            self.calls.append((operation, payload, command_id, kwargs))
            return {'deleted': self.results.pop(0)}

    monkeypatch.setattr(
        event_log._SIDECAR_MAINTENANCE_STOP, 'is_set', lambda: False)
    client = Client()
    result = event_log._prune_sidecar_event_backlog(
        client, 1234, retention_class='streaming')

    assert result == {
        'deleted': event_log._PRUNE_BATCH_ROWS * 2 + 3,
        'batches': 3,
        'remaining': False,
    }
    assert all(call[0] == 'event.prune' and call[2] is None
               for call in client.calls)
    assert all(call[1]['retention_class'] == 'streaming'
               for call in client.calls)
    assert all(call[1]['limit'] == event_log._PRUNE_BATCH_ROWS
               for call in client.calls)
    assert all(
        call[1]['legacy_recovery_limit']
        == event_log._LEGACY_EVENT_RECOVERY_ROWS
        for call in client.calls
    )
    assert event_log._LEGACY_EVENT_RECOVERY_ROWS > event_log._PRUNE_BATCH_ROWS
    assert all(call[3]['priority'] == 'maintenance'
               for call in client.calls)


def test_sidecar_event_backlog_stops_immediately_when_index_is_missing(
        monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class Client:
        def __init__(self):
            self.calls = 0

        def command(self, *_args, **_kwargs):
            self.calls += 1
            return {
                'deleted': 0,
                'deferred': True,
                'reason': 'missing_index',
                'required_index': TASK_EVENT_RETENTION_SPECS['streaming'][0],
            }

    monkeypatch.setattr(
        event_log._SIDECAR_MAINTENANCE_STOP, 'is_set', lambda: False)
    client = Client()
    result = event_log._prune_sidecar_event_backlog(
        client, 1234, retention_class='streaming')

    assert result == {
        'deleted': 0,
        'batches': 1,
        'remaining': False,
        'deferred': True,
        'reason': 'missing_index',
        'required_index': TASK_EVENT_RETENTION_SPECS['streaming'][0],
    }
    assert client.calls == 1


def test_sidecar_event_backlog_honors_explicit_underfilled_has_more(
        monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class Client:
        def __init__(self):
            self.results = [
                {'deleted': 3, 'has_more': True},
                {'deleted': 0, 'has_more': False},
            ]

        def command(self, *_args, **_kwargs):
            return self.results.pop(0)

    monkeypatch.setattr(
        event_log._SIDECAR_MAINTENANCE_STOP, 'is_set', lambda: False)
    result = event_log._prune_sidecar_event_backlog(
        Client(), 1234, retention_class='streaming')

    assert result == {
        'deleted': 3,
        'batches': 2,
        'remaining': False,
    }


def test_backlog_cadence_returns_to_slow_probe_after_drain(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    monkeypatch.setattr(event_log, '_BACKLOG_MAINTENANCE_INTERVAL_S', 30.0)
    assert event_log._backlog_cadence(300.0, True) == 30.0
    assert event_log._backlog_cadence(300.0, False) == 300.0
    assert event_log._backlog_cadence(15.0, True) == 15.0
    assert event_log._TASK_EVENT_PRUNE_INTERVAL_S >= 300.0


def test_stop_storage_maintenance_is_bounded_and_joins(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class _Stop:
        def __init__(self):
            self.was_set = False

        def set(self):
            self.was_set = True

    class _Thread:
        def __init__(self):
            self.joined = []

        def join(self, timeout):
            self.joined.append(timeout)

        def is_alive(self):
            return False

    stop = _Stop()
    thread = _Thread()
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_STOP', stop)
    monkeypatch.setattr(event_log, '_SIDECAR_MAINTENANCE_THREAD', thread)

    assert event_log.stop_storage_maintenance(timeout='0.25') is True
    assert stop.was_set is True
    assert thread.joined == [0.25]


def test_atexit_stops_maintenance_then_drains_batcher(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    calls = []
    monkeypatch.setattr(event_log, 'stop_storage_maintenance',
                        lambda timeout=0: calls.append(('stop', timeout)) or True)
    monkeypatch.setattr(event_log, 'stop_sidecar_batcher',
                        lambda timeout=0: calls.append(('batcher', timeout)) or True)

    event_log._shutdown_event_storage()
    assert calls == [('stop', 3.0), ('batcher', 3.0)]
