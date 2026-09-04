"""Sidecar-mode event batcher singleton in ``lib.tasks_pkg.event_log``.

Regression pin for the 2026-08-19 flood: ``append_persistent_event`` called
``_ensure_sidecar_batcher()`` but the helper was never defined, so every
Sidecar-mode event append died with ``name '_ensure_sidecar_batcher' is not
defined`` — authoritative persistence failed and pushes were withheld.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


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


def test_batcher_rejection_uses_one_natural_key_direct_fallback(
    fresh,
    monkeypatch,
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
        {'priority': 'event', 'deadline': el._PERSIST_TIMEOUT_S},
    )]
