"""Bounded client-side event coalescing through a real Sidecar."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gc
import threading
import weakref

import pytest

from lib.storage import StorageError, StorageEventBatcher, StorageSupervisor


pytestmark = pytest.mark.unit


class _RecordingClient:
    def __init__(self, *, blocked=False):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def command(self, operation, payload, *_args, **_kwargs):
        assert operation == 'event.append_batch'
        events = list(payload['events'])
        self.calls.append(events)
        self.started.set()
        assert self.release.wait(3)
        return {
            'results': [
                {
                    'inserted': True,
                    'task_id': event['task_id'],
                    'sequence': event['sequence'],
                }
                for event in events
            ],
            'inserted': len(events),
            'deduplicated': 0,
        }


@pytest.mark.serial
def test_two_hundred_confirmed_streams_share_transactions(tmp_path):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: supervisor.client,
        coalesce_ms=20)
    barrier = threading.Barrier(200)

    def append(index):
        barrier.wait(timeout=10)
        return batcher.append(
            f'task-{index}', 0, {'kind': 'delta', 'index': index},
            timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=200) as pool:
            results = list(pool.map(append, range(200)))
        assert all(item['inserted'] for item in results)
        metrics = batcher.metrics
        assert metrics['inserted'] == 200
        assert metrics['batches'] < 20
        assert metrics['max_queue_depth'] <= metrics['queue_capacity']

        for index in range(200, 250):
            accepted = batcher.append(
                f'task-{index}', 0, {'kind': 'delta', 'index': index},
                wait=False)
            assert accepted['accepted'] is True
        assert batcher.flush(timeout=5) is True
        persisted = supervisor.client.query(
            'event.list', {
                'task_id': 'task-249', 'after_sequence': -1, 'limit': 10})
        assert [row['sequence'] for row in persisted] == [0]
        assert batcher.metrics['persist_lag_max_ms'] <= 300

        assert batcher.append(
            'task-0', 0, {'kind': 'delta', 'index': 0})['inserted'] is False
        with pytest.raises(StorageError) as raised:
            batcher.append(
                'task-0', 0, {'kind': 'different', 'index': 0})
        assert raised.value.code == 'database_conflict'
    finally:
        assert batcher.close(timeout=10)
        supervisor.stop()


def test_commit_callback_runs_after_async_rows_are_queryable(tmp_path):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    committed: list[tuple[frozenset[str], list[int]]] = []

    def observe(task_ids: frozenset[str]) -> None:
        rows = supervisor.client.query(
            'event.list', {
                'task_id': 'async-task', 'after_sequence': -1, 'limit': 10})
        committed.append((task_ids, [row['sequence'] for row in rows]))

    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: supervisor.client,
        on_commit=observe)
    try:
        assert batcher.metrics['coalesce_window_ms'] == 1.0
        assert batcher.metrics['max_window_ms'] == 250.0
        accepted = batcher.append(
            'async-task', 7, {'type': 'messages_snapshot'}, wait=False)
        assert accepted['accepted'] is True
        assert batcher.flush(timeout=5)
        assert committed == [(frozenset({'async-task'}), [7])]
    finally:
        assert batcher.close(timeout=10)
        supervisor.stop()


def test_queue_byte_saturation_rejects_without_retaining_another_event():
    client = _RecordingClient(blocked=True)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client,
        queue_capacity=4,
        queue_byte_capacity=192 * 1024,
        coalesce_ms=0,
    )
    try:
        assert batcher.append(
            'active', 0, {'text': 'a'}, wait=False)['accepted']
        assert client.started.wait(1)
        assert batcher.append(
            'queued', 0, {'text': 'x' * (110 * 1024)}, wait=False)['accepted']

        with pytest.raises(StorageError) as raised:
            batcher.append(
                'rejected', 0, {'text': 'y' * (110 * 1024)}, wait=False)

        assert raised.value.code == 'database_busy'
        metrics = batcher.metrics
        assert metrics['queue_depth'] == 1
        assert metrics['queued_bytes'] <= metrics['queue_byte_capacity']
        assert metrics['queue_rejections'] == 1
        assert len(client.calls) == 1
    finally:
        client.release.set()
        assert batcher.close(timeout=3)


def test_confirmation_timeout_cancels_queued_payload_immediately():
    client = _RecordingClient(blocked=True)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client,
        queue_capacity=4,
        queue_byte_capacity=1024 * 1024,
        coalesce_ms=0,
    )

    class Payload(dict):
        pass

    def enqueue_then_timeout():
        payload = Payload(text='retained' * 10_000)
        reference = weakref.ref(payload)
        with pytest.raises(StorageError) as raised:
            batcher.append('cancelled', 0, payload, timeout=0.05)
        assert raised.value.code == 'database_timeout'
        return reference

    try:
        assert batcher.append(
            'active', 0, {'text': 'a'}, wait=False)['accepted']
        assert client.started.wait(1)
        payload_reference = enqueue_then_timeout()
        gc.collect()

        assert payload_reference() is None
        metrics = batcher.metrics
        assert metrics['cancelled_before_start'] == 1
        assert metrics['queue_depth'] == 0
        assert metrics['queued_bytes'] == 0
    finally:
        client.release.set()
        assert batcher.close(timeout=3)


def test_batch_byte_budget_splits_fifo_without_reordering():
    client = _RecordingClient()
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client,
        queue_capacity=8,
        queue_byte_capacity=2 * 1024 * 1024,
        max_batch_bytes=256 * 1024,
        coalesce_ms=50,
    )
    try:
        for sequence in range(3):
            assert batcher.append(
                'split',
                sequence,
                {'text': chr(97 + sequence) * (140 * 1024)},
                wait=False,
            )['accepted']
        assert batcher.flush(timeout=3)

        assert [
            event['sequence']
            for call in client.calls
            for event in call
        ] == [0, 1, 2]
        assert [len(call) for call in client.calls] == [1, 1, 1]
        assert batcher.metrics['max_batch_bytes'] <= 256 * 1024
    finally:
        assert batcher.close(timeout=3)


def test_oversized_event_fails_before_queue_or_sidecar_call():
    client = _RecordingClient()
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client,
        max_batch_bytes=1024 * 1024,
    )
    try:
        with pytest.raises(StorageError) as raised:
            batcher.append(
                'oversized', 0, {'text': 'x' * (1024 * 1024)}, wait=False)

        assert raised.value.code == 'database_protocol_error'
        assert client.calls == []
        assert batcher.metrics['queue_depth'] == 0
    finally:
        assert batcher.close(timeout=3)


def test_close_can_wait_again_after_an_inflight_drain_times_out():
    client = _RecordingClient(blocked=True)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client,
        coalesce_ms=0,
    )

    assert batcher.append(
        'closing', 0, {'text': 'durable'}, wait=False)['accepted']
    assert client.started.wait(1)
    assert batcher.close(timeout=0.05) is False

    with pytest.raises(StorageError) as raised:
        batcher.append('closed', 1, {'text': 'rejected'}, wait=False)
    assert raised.value.code == 'database_unavailable'

    client.release.set()
    assert batcher.close(timeout=3) is True
