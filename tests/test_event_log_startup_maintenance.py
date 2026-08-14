"""Storage retention must start even when no new task emits an event."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_public_start_is_idempotent_singleton(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    sentinel = object()
    calls = []
    monkeypatch.setattr(event_log, '_ensure_maintenance',
                        lambda: calls.append(True) or sentinel)

    assert event_log.start_storage_maintenance() is sentinel
    assert calls == [True]


def test_serving_loop_eagerly_starts_storage_maintenance():
    import lib.production_lifecycle as production_lifecycle

    source = inspect.getsource(production_lifecycle)
    assert 'start_storage_maintenance()' in source


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
    monkeypatch.setattr(event_log, '_MAINTENANCE_STOP', stop)
    monkeypatch.setattr(event_log, '_MAINTENANCE_THREAD', thread)

    assert event_log.stop_storage_maintenance(timeout='0.25') is True
    assert stop.was_set is True
    assert thread.joined == [0.25]


def test_atexit_stops_maintenance_before_pool_teardown(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    calls = []
    monkeypatch.setattr(event_log, 'stop_storage_maintenance',
                        lambda timeout=0: calls.append(('stop', timeout)) or True)
    monkeypatch.setattr(event_log, 'stop_event_writer',
                        lambda timeout=0: calls.append(('writer', timeout)) or True)

    event_log._flush_lane_at_exit()
    assert calls == [('stop', 3.0), ('writer', 3.0)]


def test_stop_event_writer_flushes_sets_stop_and_joins(monkeypatch):
    import lib.tasks_pkg.event_log as event_log

    class _Stop:
        def __init__(self):
            self.was_set = False

        def set(self):
            self.was_set = True

        def clear(self):
            self.was_set = False

    class _Thread:
        def __init__(self):
            self.joined = []
            self.alive = True

        def join(self, timeout):
            self.joined.append(timeout)
            self.alive = False

        def is_alive(self):
            return self.alive

    stop = _Stop()
    thread = _Thread()
    waited = []
    monkeypatch.setattr(event_log, '_WRITER_STOP', stop)
    monkeypatch.setattr(event_log, '_WRITER_THREAD', thread)
    monkeypatch.setattr(event_log, '_current_ticket', lambda: 17)
    monkeypatch.setattr(
        event_log, '_wait_through',
        lambda ticket, timeout: waited.append((ticket, timeout)) or True)

    assert event_log.stop_event_writer(timeout='0.25') is True
    assert waited and waited[0][0] == 17
    assert stop.was_set is True
    assert len(thread.joined) == 1 and 0 <= thread.joined[0] <= 0.25
    assert event_log._WRITER_THREAD is None
