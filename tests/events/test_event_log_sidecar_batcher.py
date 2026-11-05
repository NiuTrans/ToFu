"""Sidecar-mode event batcher singleton in ``lib.tasks_pkg.event_log``.

Regression pin for the 2026-08-19 flood: ``append_persistent_event`` called
``_ensure_sidecar_batcher()`` but the helper was never defined, so every
Sidecar-mode event append died with ``name '_ensure_sidecar_batcher' is not
defined`` — authoritative persistence failed and pushes were withheld.
"""

from __future__ import annotations

import threading

import pytest

from lib.storage.deadline_policy import (
    observe_storage_service,
    reset_storage_service_observations,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_service_observations():
    reset_storage_service_observations()
    yield
    reset_storage_service_observations()


class _FakeBatcher:
    def __init__(self):
        self.closed = False
        self.close_timeouts = []

    def close(self, timeout: float = 10.0) -> bool:
        self.close_timeouts.append(timeout)
        self.closed = True
        return True


@pytest.fixture
def fresh(monkeypatch):
    from lib.tasks_pkg import event_log as el

    created = []

    def factory(**_kwargs):
        batcher = _FakeBatcher()
        created.append(batcher)
        return batcher

    monkeypatch.setattr('lib.storage.StorageEventBatcher', factory)
    monkeypatch.setattr(el, '_SIDECAR_BATCHER', None)
    try:
        yield el, created
    finally:
        monkeypatch.setattr(el, '_SIDECAR_BATCHER', None)


def test_ensure_returns_one_shared_singleton(fresh):
    el, created = fresh
    first = el._ensure_sidecar_batcher()
    second = el._ensure_sidecar_batcher()
    assert first is second
    assert len(created) == 1


def test_ensure_is_race_safe(fresh):
    el, created = fresh
    barrier = threading.Barrier(8)
    results = []

    def grab():
        barrier.wait(timeout=5)
        results.append(el._ensure_sidecar_batcher())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(created) == 1
    assert all(r is created[0] for r in results)


def test_stop_drains_and_clears_the_singleton(fresh):
    el, created = fresh
    batcher = el._ensure_sidecar_batcher()
    assert el.stop_sidecar_batcher(timeout=1.5) is True
    assert batcher.closed
    assert batcher.close_timeouts == [1.5]
    assert el._SIDECAR_BATCHER is None
    # A later append can re-create the lane after a clean stop.
    assert el._ensure_sidecar_batcher() is not batcher


def test_construction_failure_is_explicit(fresh, monkeypatch):
    el, _created = fresh
    monkeypatch.setattr(
        'lib.storage.StorageEventBatcher',
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError, match='boom'):
        el._ensure_sidecar_batcher()


@pytest.mark.parametrize('observed_seconds', [None, 2.5])
def test_batcher_rejection_uses_one_natural_key_direct_fallback(
    fresh,
    monkeypatch,
    observed_seconds,
):
    from lib.storage import StorageError

    el, _created = fresh
    batch_calls = []
    direct_calls = []

    class RejectingBatcher:
        def append(self, **kwargs):
            batch_calls.append(kwargs)
            raise StorageError(
                'database_busy', 'event queue saturated', True, 25)

    class DirectClient:
        def command(self, operation, payload, command_id, **kwargs):
            direct_calls.append((operation, payload, command_id, kwargs))
            return {'inserted': True}

    monkeypatch.setattr(el, '_ensure_sidecar_batcher', RejectingBatcher)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kwargs: DirectClient())
    monkeypatch.setattr(el, '_invalidate_event_read_caches', lambda _task_id: None)
    if observed_seconds is not None:
        for _ in range(16):
            observe_storage_service('command', 'event.append', observed_seconds)
    # Cold: the writer's own budget. Observed: four times the service tail.
    expected_deadline = el._PERSIST_TIMEOUT_S if observed_seconds is None else 10.0

    result = el.append_persistent_event(
        'task-fallback', 7, {'type': 'delta', 'content': 'durable'})

    assert result == {'inserted': True}
    assert len(batch_calls) == 1
    assert direct_calls == [(
        'event.append',
        {
            'task_id': 'task-fallback',
            'sequence': 7,
            'event': {'type': 'delta', 'content': 'durable'},
        },
        None,
        {'priority': 'event', 'deadline': expected_deadline},
    )]


def test_batch_confirmation_and_flush_waits_follow_the_observed_service_time(
    fresh,
    monkeypatch,
):
    el, _created = fresh
    waits = []

    class RecordingBatcher:
        def append(self, **kwargs):
            waits.append(('append', kwargs['timeout']))
            return {'inserted': True}

        def flush(self, timeout):
            waits.append(('flush', timeout))
            return True

    batcher = RecordingBatcher()
    monkeypatch.setattr(el, '_ensure_sidecar_batcher', lambda: batcher)
    monkeypatch.setattr(el, '_SIDECAR_BATCHER', batcher)
    monkeypatch.setattr(el, '_invalidate_event_read_caches', lambda _task_id: None)
    for _ in range(16):
        observe_storage_service('command', 'event.append_batch', 3.0)

    el.append_persistent_event('task-waits', 1, {'type': 'delta', 'content': 'x'})
    assert el.flush_pending('task-waits') is True

    # Both wait for the batch path, so both get four times its observed tail.
    assert waits == [('append', 12.0), ('flush', 12.0)]


@pytest.mark.parametrize('batch_fails', [False, True])
def test_slow_batch_is_awaited_and_never_raced_by_a_direct_append(
    monkeypatch,
    batch_fails,
):
    from lib.storage import StorageError, StorageEventBatcher
    from lib.tasks_pkg import event_log as el

    batch_entered = threading.Event()
    release = threading.Event()
    in_flight = threading.Event()
    direct_called = threading.Event()
    direct_calls = []
    failure = StorageError('database_timeout', 'deadline_elapsed')

    class Authority:
        def command(self, operation, payload, command_id, **kwargs):
            if operation == 'event.append_batch':
                in_flight.set()
                batch_entered.set()
                try:
                    assert release.wait(10)
                finally:
                    in_flight.clear()
                if batch_fails:
                    raise failure
                return {'results': [
                    {'inserted': True, 'task_id': row['task_id'], 'sequence': row['sequence']}
                    for row in payload['events']], 'inserted': len(payload['events'])}
            direct_calls.append((operation, payload['sequence'], in_flight.is_set()))
            direct_called.set()
            return {'inserted': False, 'task_id': payload['task_id'],
                    'sequence': payload['sequence']}

    authority = Authority()
    # With no observations the budget is this floor: the confirmation wait
    # expires almost at once, while the batch is held.
    monkeypatch.setattr(el, '_PERSIST_TIMEOUT_S', 0.05)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_kwargs: authority)
    monkeypatch.setattr(el, '_invalidate_event_read_caches', lambda _task_id: None)
    batcher = StorageEventBatcher(client_provider=lambda **_kwargs: authority,
                                  coalesce_ms=0)
    cancel_queued = batcher._cancel_queued

    def cancel_after_dispatch(pending):
        # Expiry is judged once the row is claimed, whatever the scheduling.
        assert batch_entered.wait(10)
        return cancel_queued(pending)

    monkeypatch.setattr(batcher, '_cancel_queued', cancel_after_dispatch)
    monkeypatch.setattr(el, '_ensure_sidecar_batcher', lambda: batcher)
    outcome = {}

    def produce():
        try:
            outcome['result'] = el.append_persistent_event(
                'task-slow', 3, {'type': 'delta', 'content': 'durable'})
        except Exception as error:  # surfaced through the assertion below
            outcome['error'] = error

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    try:
        assert batch_entered.wait(10)
        assert not direct_called.wait(0.5), 'the row was re-sent while its batch was in flight'
        release.set()
        producer.join(10)
        assert not producer.is_alive()
        assert 'error' not in outcome
        if batch_fails:
            # One natural-key fallback, only after the batch settled.
            assert direct_calls == [('event.append', 3, False)]
            assert outcome['result'] == {'inserted': False, 'task_id': 'task-slow',
                                         'sequence': 3}
        else:
            assert direct_calls == []
            assert outcome['result']['inserted'] is True
    finally:
        release.set()
        producer.join(10)
        assert batcher.close(timeout=10)
