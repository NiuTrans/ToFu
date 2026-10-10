"""Event batcher close fail-fast.

A close(timeout) that times out behind a wedged storage RPC must fail every
still-queued pending event with typed ``database_unavailable`` instead of
leaving producers hanging on their confirmation events. The in-flight batch
keeps its own deadline and ambiguous-commit contract; normal closes keep
the drain-first semantics.
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage.event_batcher import StorageEventBatcher


pytestmark = pytest.mark.unit


class _WedgedClient:
    """command() blocks until released, then answers a well-formed batch."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def command(self, operation, payload, *_args, **_kwargs):
        assert operation == 'event.append_batch'
        self.calls += 1
        self.started.set()
        assert self.release.wait(10)
        return {
            'results': [
                {
                    'inserted': True,
                    'task_id': event['task_id'],
                    'sequence': event['sequence'],
                }
                for event in payload['events']
            ],
            'inserted': len(payload['events']),
            'deduplicated': 0,
        }


@pytest.mark.parametrize('queue_byte_capacity', [None, 1])
def test_close_timeout_fails_queued_producers_fast(queue_byte_capacity):
    client = _WedgedClient()
    batcher = StorageEventBatcher(
        client_provider=lambda **kwargs: client,
        coalesce_ms=0, max_window_ms=1, queue_byte_capacity=queue_byte_capacity)
    try:
        # First event: claimed by the worker, which wedges inside the RPC.
        in_flight_error = []

        def blocked_producer():
            try:
                batcher.append('task-a', 1, {'n': 1}, timeout=30)
            except StorageError as exc:
                in_flight_error.append(exc)

        producer = threading.Thread(target=blocked_producer, daemon=True)
        producer.start()
        assert client.started.wait(5)

        # Second event: stays queued behind the wedged batch.
        queued_error = []

        def queued_producer():
            try:
                batcher.append('task-b', 2, {'n': 2}, timeout=30)
            except StorageError as exc:
                queued_error.append(exc)

        queued = threading.Thread(target=queued_producer, daemon=True)
        queued.start()
        time.sleep(0.1)  # let the queued event enqueue

        finished_at = None
        started_close = time.monotonic()
        assert batcher.close(timeout=0.3) is False
        finished_at = time.monotonic() - started_close

        # The queued producer got the typed error promptly after the close
        # deadline — not after its own 30 s confirmation timeout.
        queued.join(timeout=5)
        assert not queued.is_alive()
        assert len(queued_error) == 1
        assert queued_error[0].code == 'database_unavailable'
        assert finished_at < 5
        # The in-flight batch is still wedged in its own RPC: untouched by
        # the queue drain, still the ambiguous-commit contract.
        producer.join(timeout=0.2)
        assert producer.is_alive()
    finally:
        client.release.set()
        producer.join(timeout=10)
        batcher.close(timeout=5)


@pytest.mark.parametrize('queue_byte_capacity', [None, 1])
def test_normal_close_still_drains_first(queue_byte_capacity):
    client = _WedgedClient()
    client.release.set()  # healthy: every command answers immediately
    batcher = StorageEventBatcher(
        client_provider=lambda **kwargs: client,
        coalesce_ms=0, max_window_ms=1, queue_byte_capacity=queue_byte_capacity)
    result = batcher.append('task-c', 1, {'n': 3}, timeout=5)
    assert result['inserted'] is True
    assert batcher.close(timeout=5) is True
    assert client.calls >= 1
