"""Bounded client-side event coalescing through a real Sidecar."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import gc
import threading
import time
import weakref

import pytest

from lib.storage import StorageError, StorageEventBatcher, StorageSupervisor
from lib.storage import event_batcher as event_batcher_module
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


class _RecordingClient:
    def __init__(self, *, blocked=False, failure=None):
        self.calls = []
        self.deadlines = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.failure = failure
        if not blocked:
            self.release.set()

    def command(self, operation, payload, *_args, **kwargs):
        assert operation == 'event.append_batch'
        events = list(payload['events'])
        self.calls.append(events)
        self.deadlines.append(kwargs.get('deadline'))
        self.started.set()
        assert self.release.wait(3)
        if self.failure is not None:
            raise self.failure
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


def _expire_only_after_dispatch(monkeypatch, batcher, dispatched):
    """Hold confirmation expiry until the batch RPC started: the producer's
    event is then claimed, whatever the host's thread scheduling."""
    cancel_queued = batcher._cancel_queued

    def cancel_after_dispatch(pending):
        assert dispatched.wait(_DURABLE_WAIT_S)
        return cancel_queued(pending)

    monkeypatch.setattr(batcher, '_cancel_queued', cancel_after_dispatch)


class _FirstTransactionGate:
    """The real Sidecar client, holding the first event transaction open."""

    def __init__(self, client):
        self._client = client
        self.release = threading.Event()
        self._held = False

    def __getattr__(self, name):
        return getattr(self._client, name)

    def command(self, operation, payload, *args, **kwargs):
        if operation == 'event.append_batch' and not self._held:
            self._held = True
            self.release.wait()
        return self._client.command(operation, payload, *args, **kwargs)


# Completion-driven waits; only a wedged batcher may exhaust them. Persistence
# latency is a certification acceptance (scripts/storage_certify.py), not a
# unit-test bound: a loaded host decides it.
_DURABLE_WAIT_S = 30


def test_two_hundred_confirmed_streams_share_transactions(tmp_path):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    gate = _FirstTransactionGate(supervisor.client)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: gate,
        coalesce_ms=20)

    def append(index):
        return batcher.append(
            f'task-{index}', 0, {'kind': 'delta', 'index': index},
            timeout=_DURABLE_WAIT_S)

    try:
        with ThreadPoolExecutor(max_workers=200) as pool:
            try:
                streams = [pool.submit(append, index) for index in range(200)]
                # Every stream queues while one transaction is open, so the
                # transaction count is independent of thread scheduling.
                while batcher.metrics['submitted'] < 200:
                    assert not any(stream.done() for stream in streams)
                    time.sleep(0.01)
            finally:
                gate.release.set()
            results = [stream.result() for stream in streams]
        assert all(item['inserted'] for item in results)
        metrics = batcher.metrics
        assert metrics['inserted'] == 200
        # The open transaction holds the streams that reached it first; all
        # streams queued behind it share at most one more.
        assert metrics['batches'] <= 2
        assert metrics['queue_capacity'] == 0
        assert metrics['queue_rejections'] == 0

        for index in range(200, 250):
            accepted = batcher.append(
                f'task-{index}', 0, {'kind': 'delta', 'index': index},
                wait=False)
            assert accepted['accepted'] is True
        assert batcher.flush(timeout=_DURABLE_WAIT_S) is True
        persisted = supervisor.client.query(
            'event.list', {
                'task_id': 'task-249', 'after_sequence': -1, 'limit': 10})
        assert [row['sequence'] for row in persisted] == [0]
        assert batcher.metrics['persist_lag_samples'] == 250

        assert batcher.append(
            'task-0', 0, {'kind': 'delta', 'index': 0},
            timeout=_DURABLE_WAIT_S)['inserted'] is False
        with pytest.raises(StorageError) as raised:
            batcher.append(
                'task-0', 0, {'kind': 'different', 'index': 0},
                timeout=_DURABLE_WAIT_S)
        assert raised.value.code == 'database_conflict'
    finally:
        assert batcher.close(timeout=_DURABLE_WAIT_S)
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


def test_queue_byte_saturation_accepts_and_spills_additional_event():
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

        assert batcher.append(
            'overflow', 0, {'text': 'y' * (110 * 1024)}, wait=False)['accepted']
        metrics = batcher.metrics
        assert metrics['queue_depth'] == 2
        assert metrics['queued_bytes'] > metrics['queue_byte_capacity']
        assert metrics['frame_buffer_memory_bytes'] <= metrics['queue_byte_capacity']
        assert metrics['frame_buffer_file_bytes'] > 0
        assert metrics['queue_rejections'] == 0
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


def test_claimed_event_awaits_its_in_flight_batch_instead_of_timing_out(monkeypatch):
    client = _RecordingClient(blocked=True)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client, coalesce_ms=0)
    _expire_only_after_dispatch(monkeypatch, batcher, client.started)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed = pool.submit(
                batcher.append, 'claimed', 0, {'text': 'in flight'},
                timeout=0.05)
            assert client.started.wait(_DURABLE_WAIT_S)
            # The confirmation wait expired long ago, but the row is inside
            # the batch RPC: answering now would invite a concurrent re-send.
            with pytest.raises(FutureTimeout):
                claimed.result(timeout=0.5)
            client.release.set()
            assert claimed.result(_DURABLE_WAIT_S)['inserted'] is True
        assert len(client.calls) == 1
        metrics = batcher.metrics
        assert metrics['cancelled_before_start'] == 0
        assert metrics['inserted'] == 1
    finally:
        client.release.set()
        assert batcher.close(timeout=_DURABLE_WAIT_S)


def test_claimed_event_receives_its_batch_failure_only_after_the_batch_settles(monkeypatch):
    failure = StorageError('database_timeout', 'deadline_elapsed')
    client = _RecordingClient(blocked=True, failure=failure)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client, coalesce_ms=0)
    _expire_only_after_dispatch(monkeypatch, batcher, client.started)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed = pool.submit(
                batcher.append, 'claimed', 0, {'text': 'in flight'},
                timeout=0.05)
            assert client.started.wait(_DURABLE_WAIT_S)
            with pytest.raises(FutureTimeout):
                claimed.result(timeout=0.5)
            client.release.set()
            with pytest.raises(StorageError) as raised:
                claimed.result(_DURABLE_WAIT_S)
        # The definite outcome of the settled batch, never a fabricated
        # confirmation timeout while the row was still in flight.
        assert raised.value is failure
        assert len(client.calls) == 1
        assert batcher.metrics['failed'] == 1
    finally:
        client.release.set()
        assert batcher.close(timeout=_DURABLE_WAIT_S)


def test_claimed_event_is_released_when_its_worker_dies_before_settling(monkeypatch):
    client = _RecordingClient()
    release_payload = event_batcher_module._release_pending_payload

    def fail_once(pending):
        monkeypatch.setattr(
            event_batcher_module, '_release_pending_payload', release_payload)
        raise OSError('injected spill close failure')

    # The worker dies after its RPC, before it settles the claimed event.
    monkeypatch.setattr(threading, 'excepthook', lambda _args: None)
    monkeypatch.setattr(event_batcher_module, '_release_pending_payload', fail_once)
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client, coalesce_ms=0)
    _expire_only_after_dispatch(monkeypatch, batcher, client.started)
    outcome = {}

    def produce():
        try:
            outcome['result'] = batcher.append(
                'claimed', 0, {'text': 'committed'}, timeout=0.05)
        except StorageError as error:
            outcome['error'] = error

    producer = threading.Thread(target=produce, daemon=True)
    try:
        producer.start()
        producer.join(_DURABLE_WAIT_S)
        assert not producer.is_alive(), 'a dead worker stranded its claimed event'
        assert outcome['error'].code == 'database_unavailable'
        assert outcome['error'].retryable is True
        assert len(client.calls) == 1
    finally:
        assert batcher.close(timeout=_DURABLE_WAIT_S)


def test_batch_deadline_follows_the_observed_authority_service_time():
    client = _RecordingClient()
    batcher = StorageEventBatcher(
        client_provider=lambda **_kwargs: client, coalesce_ms=0)
    try:
        assert batcher.append(
            'cold', 0, {'text': 'a'}, timeout=_DURABLE_WAIT_S)['inserted']
        for _ in range(16):
            observe_storage_service('command', 'event.append_batch', 3.0)
        assert batcher.append(
            'congested', 0, {'text': 'b'}, timeout=_DURABLE_WAIT_S)['inserted']
        # Cold: the batcher's own budget is the floor. Congested: four times
        # the observed tail.
        assert client.deadlines == [2.0, 12.0]
    finally:
        assert batcher.close(timeout=_DURABLE_WAIT_S)


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
