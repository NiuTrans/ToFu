"""Bounded 300 ms / 500-row natural-key event batcher."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import queue
import threading
import time
from typing import Any, Callable

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError


@dataclass(slots=True)
class _PendingEvent:
    payload: dict[str, Any]
    enqueued_at: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class StorageEventBatcher:
    """Coalesce events without allowing an unbounded memory or loss window."""

    def __init__(
        self,
        client_provider: Callable[..., StorageClient] | None = None,
        *,
        max_batch: int = 500,
        max_window_ms: int = 250,
        coalesce_ms: int = 5,
        queue_capacity: int = 10_000,
    ) -> None:
        if client_provider is None:
            from lib.storage import get_storage_client
            client_provider = get_storage_client
        self._client_provider = client_provider
        self._max_batch = max(1, min(500, int(max_batch)))
        self._max_window_s = max(0.001, min(0.3, max_window_ms / 1000))
        self._coalesce_s = max(
            0.0, min(self._max_window_s, coalesce_ms / 1000))
        self._queue: queue.Queue[_PendingEvent | None] = queue.Queue(
            maxsize=max(1, int(queue_capacity)))
        self._metrics_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._metrics = {
            'submitted': 0, 'batches': 0, 'inserted': 0,
            'deduplicated': 0, 'failed': 0, 'max_queue_depth': 0,
        }
        self._persist_lags_ms: deque[float] = deque(maxlen=200_000)
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name='storage-event-batcher', daemon=True)
        self._thread.start()

    @property
    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            result = dict(self._metrics)
            ordered = sorted(self._persist_lags_ms)
            result['persist_lag_samples'] = len(ordered)
            result['persist_lag_p95_ms'] = (
                round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3)
                if ordered else 0.0)
            result['persist_lag_p99_ms'] = (
                round(ordered[max(0, math.ceil(len(ordered) * 0.99) - 1)], 3)
                if ordered else 0.0)
            result['persist_lag_max_ms'] = (
                round(ordered[-1], 3) if ordered else 0.0)
            return result

    def append(
        self,
        task_id: str,
        sequence: int,
        event: Any,
        *,
        timeout: float = 2.0,
        wait: bool = True,
    ) -> dict[str, Any]:
        pending = _PendingEvent({
            'task_id': task_id, 'sequence': sequence, 'event': event,
        })
        with self._state_lock:
            if self._closed:
                raise StorageError(
                    'database_unavailable', 'Storage event batcher is closed')
            try:
                self._queue.put(
                    pending, timeout=max(0.001, min(2.0, timeout)))
            except queue.Full as exc:
                raise StorageError(
                    'database_busy', 'Storage event queue is full',
                    True, 25) from exc
        with self._metrics_lock:
            self._metrics['submitted'] += 1
            self._metrics['max_queue_depth'] = max(
                self._metrics['max_queue_depth'], self._queue.qsize())
        if not wait:
            return {
                'accepted': True, 'task_id': task_id, 'sequence': sequence,
            }
        if not pending.done.wait(max(0.001, timeout)):
            raise StorageError(
                'database_timeout', 'Storage event confirmation timed out',
                True, 25)
        if pending.error is not None:
            if isinstance(pending.error, StorageError):
                raise pending.error
            raise StorageError(
                'database_internal', 'Storage event batch failed') from pending.error
        return pending.result or {
            'inserted': False, 'task_id': task_id, 'sequence': sequence,
        }

    def _take_batch(self) -> tuple[list[_PendingEvent], bool]:
        first = self._queue.get()
        if first is None:
            return [], True
        batch = [first]
        stop_after = False
        # Events accumulated while the previous transaction was in flight are
        # already coalesced. Drain that backlog immediately; waiting another
        # window here would add avoidable FUSE fsync latency to every busy batch.
        backlog = min(self._queue.qsize(), self._max_batch - 1)
        for _ in range(backlog):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stop_after = True
                break
            batch.append(item)
        if len(batch) > 1 or stop_after:
            return batch, stop_after
        deadline = time.monotonic() + min(
            self._coalesce_s, self._max_window_s)
        while len(batch) < self._max_batch:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                stop_after = True
                break
            batch.append(item)
        return batch, stop_after

    def _run(self) -> None:
        while True:
            batch, stop_after = self._take_batch()
            if not batch:
                return
            try:
                response = self._client_provider(write=True).command(
                    'event.append_batch',
                    {'events': [item.payload for item in batch]},
                    None,
                    priority='event',
                    deadline=max(2.0, self._max_window_s + 1.0),
                )
                results = response.get('results') or []
                if len(results) != len(batch):
                    raise StorageError(
                        'database_protocol_error',
                        'Storage event batch response length mismatch')
                for pending, result in zip(batch, results):
                    pending.result = result
                completed_at = time.monotonic()
                with self._metrics_lock:
                    self._metrics['batches'] += 1
                    self._metrics['inserted'] += int(response.get('inserted') or 0)
                    self._metrics['deduplicated'] += int(
                        response.get('deduplicated') or 0)
                    self._persist_lags_ms.extend(
                        (completed_at - pending.enqueued_at) * 1000
                        for pending in batch)
            except BaseException as exc:
                for pending in batch:
                    pending.error = exc
                with self._metrics_lock:
                    self._metrics['failed'] += len(batch)
            finally:
                for pending in batch:
                    pending.done.set()
            if stop_after:
                return

    def close(self, timeout: float = 10.0) -> bool:
        with self._state_lock:
            if self._closed:
                return not self._thread.is_alive()
            self._closed = True
            self._queue.put(None, timeout=max(0.1, min(2.0, timeout)))
        self._thread.join(timeout=max(0.1, timeout))
        return not self._thread.is_alive()

    def __enter__(self) -> 'StorageEventBatcher':
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ['StorageEventBatcher']
