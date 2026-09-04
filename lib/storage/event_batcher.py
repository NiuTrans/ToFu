"""Bounded natural-key event batching before the storage writer."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Any, Callable

from lib.storage.client import StorageClient
from lib.storage.errors import StorageError
from lib.storage.protocol import canonical_json
from lib.storage_event_policy import resolve_storage_event_budget
from lib.log import get_logger
from lib.storage_metric_policy import bounded_storage_metric_sample_capacity


logger = get_logger(__name__)


@dataclass(slots=True)
class _PendingEvent:
    # None is an in-order durability barrier, never sent to storage.
    payload: dict[str, Any] | None
    wire_bytes: int = 0
    retained_bytes: int = 0
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
        on_commit: Callable[[frozenset[str]], None] | None = None,
        max_batch: int = 500,
        max_window_ms: int = 250,
        coalesce_ms: int = 1,
        queue_capacity: int | None = None,
        queue_byte_capacity: int | None = None,
        max_batch_bytes: int | None = None,
        metric_sample_capacity: int | None = None,
    ) -> None:
        if client_provider is None:
            from lib.storage import get_storage_client
            client_provider = get_storage_client
        self._client_provider = client_provider
        self._on_commit = on_commit
        event_budget = resolve_storage_event_budget()
        resolved_queue_capacity = (
            event_budget.queue_capacity
            if queue_capacity is None else int(queue_capacity)
        )
        resolved_queue_byte_capacity = (
            event_budget.queue_byte_capacity
            if queue_byte_capacity is None else int(queue_byte_capacity)
        )
        resolved_max_batch_bytes = (
            event_budget.batch_max_bytes
            if max_batch_bytes is None else int(max_batch_bytes)
        )
        self._queue_capacity = max(1, min(8_192, resolved_queue_capacity))
        self._queue_byte_capacity = max(
            1, min(1024 * 1024 * 1024, resolved_queue_byte_capacity))
        self._max_batch = max(
            1, min(event_budget.batch_max_events, int(max_batch)))
        self._max_batch_bytes = max(
            1,
            min(
                event_budget.batch_max_bytes,
                self._queue_byte_capacity,
                resolved_max_batch_bytes,
            ),
        )
        self._event_max_bytes = min(
            event_budget.event_max_bytes,
            max(1, self._max_batch_bytes - 64 * 1024),
        )
        self._max_window_s = max(0.001, min(0.3, max_window_ms / 1000))
        self._coalesce_s = max(
            0.0, min(self._max_window_s, coalesce_ms / 1000))
        self._pending: deque[_PendingEvent] = deque()
        self._queued_bytes = 0
        self._condition = threading.Condition()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            'submitted': 0, 'batches': 0, 'inserted': 0,
            'deduplicated': 0, 'failed': 0, 'max_queue_depth': 0,
            'max_queue_bytes': 0, 'queue_rejections': 0,
            'cancelled_before_start': 0, 'max_batch_bytes': 0,
        }
        self._metric_sample_capacity = bounded_storage_metric_sample_capacity(
            metric_sample_capacity)
        self._persist_lags_ms: deque[float] = deque(
            maxlen=self._metric_sample_capacity)
        self._closed = False
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, name='storage-event-batcher', daemon=True)
        self._thread.start()

    @property
    def metrics(self) -> dict[str, Any]:
        with self._condition:
            queue_depth = len(self._pending)
            queued_bytes = self._queued_bytes
        with self._metrics_lock:
            result = dict(self._metrics)
            samples = tuple(self._persist_lags_ms)
        # Sorting is reconstructible metrics work.  Keep it outside the lock
        # that records commits so a scrape cannot delay event durability.
        ordered = sorted(samples)
        result['coalesce_window_ms'] = round(self._coalesce_s * 1000, 3)
        result['max_window_ms'] = round(self._max_window_s * 1000, 3)
        result['queue_capacity'] = self._queue_capacity
        result['queue_byte_capacity'] = self._queue_byte_capacity
        result['queue_depth'] = queue_depth
        result['queued_bytes'] = queued_bytes
        result['batch_byte_capacity'] = self._max_batch_bytes
        result['event_byte_capacity'] = self._event_max_bytes
        result['persist_lag_sample_capacity'] = self._metric_sample_capacity
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

    def _build_pending_event(
        self,
        task_id: str,
        sequence: int,
        event: Any,
    ) -> _PendingEvent:
        payload = {
            'task_id': task_id,
            'sequence': sequence,
            'event': event,
        }
        try:
            wire_bytes = len(canonical_json(payload))
        except Exception as exc:
            raise StorageError(
                'database_protocol_error',
                'Storage event is not serializable',
            ) from exc
        if wire_bytes > self._event_max_bytes:
            raise StorageError(
                'database_protocol_error',
                'Storage event exceeds the batch frame budget',
            )
        return _PendingEvent(
            payload,
            wire_bytes=wire_bytes,
            # Account for the queue node/payload envelope without retaining a
            # second encoded copy beside the live Python event graph.
            retained_bytes=wire_bytes + 1_024,
        )

    def _enqueue(self, pending: _PendingEvent) -> tuple[int, int]:
        with self._condition:
            if self._closed:
                raise StorageError(
                    'database_unavailable', 'Storage event batcher is closed')
            rejected = (
                len(self._pending) >= self._queue_capacity
                or self._queued_bytes + pending.retained_bytes
                > self._queue_byte_capacity
            )
            if rejected:
                with self._metrics_lock:
                    self._metrics['queue_rejections'] += 1
                raise StorageError(
                    'database_busy', 'Storage event queue is full',
                    True, 25)
            self._pending.append(pending)
            self._queued_bytes += pending.retained_bytes
            queue_depth = len(self._pending)
            queued_bytes = self._queued_bytes
            self._condition.notify()
        return queue_depth, queued_bytes

    def _cancel_queued(self, pending: _PendingEvent) -> bool:
        with self._condition:
            for index, candidate in enumerate(self._pending):
                if candidate is not pending:
                    continue
                del self._pending[index]
                self._queued_bytes = max(
                    0, self._queued_bytes - pending.retained_bytes)
                pending.payload = None
                pending.retained_bytes = 0
                pending.done.set()
                self._condition.notify_all()
                with self._metrics_lock:
                    self._metrics['cancelled_before_start'] += 1
                return True
        return False

    def append(
        self,
        task_id: str,
        sequence: int,
        event: Any,
        *,
        timeout: float = 2.0,
        wait: bool = True,
    ) -> dict[str, Any]:
        pending = self._build_pending_event(task_id, sequence, event)
        queue_depth, queued_bytes = self._enqueue(pending)
        with self._metrics_lock:
            self._metrics['submitted'] += 1
            self._metrics['max_queue_depth'] = max(
                self._metrics['max_queue_depth'], queue_depth)
            self._metrics['max_queue_bytes'] = max(
                self._metrics['max_queue_bytes'], queued_bytes)
        if not wait:
            return {
                'accepted': True, 'task_id': task_id, 'sequence': sequence,
            }
        if not pending.done.wait(max(0.001, timeout)):
            self._cancel_queued(pending)
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

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until every item accepted before this call is durable.

        The barrier shares the bounded queue with events, so FIFO ordering is
        the contract; it does not create a synthetic storage row or close the
        reusable process-wide batcher.
        """
        wait_s = max(0.001, min(30.0, float(timeout)))
        barrier = _PendingEvent(None)
        self._enqueue(barrier)
        if not barrier.done.wait(wait_s):
            self._cancel_queued(barrier)
            raise StorageError(
                'database_timeout', 'Storage event flush timed out', True, 25)
        if barrier.error is not None:
            if isinstance(barrier.error, StorageError):
                raise barrier.error
            raise StorageError(
                'database_internal', 'Storage event flush failed') \
                from barrier.error
        return True

    def _take_batch(self) -> tuple[list[_PendingEvent], bool]:
        batch: list[_PendingEvent] = []
        batch_bytes = 0
        with self._condition:
            while not self._pending and not self._stop:
                self._condition.wait()
            if not self._pending:
                return [], True

            # Existing backlog is already coalesced. Keep FIFO items in the
            # deque when adding one would exceed the storage.v1 frame budget;
            # no pop/requeue operation can reorder a durability barrier.
            while self._pending and len(batch) < self._max_batch:
                candidate = self._pending[0]
                if (
                    candidate.payload is not None
                    and batch
                    and batch_bytes + candidate.wire_bytes
                    > self._max_batch_bytes
                ):
                    break
                self._pending.popleft()
                self._queued_bytes = max(
                    0, self._queued_bytes - candidate.retained_bytes)
                batch.append(candidate)
                batch_bytes += candidate.wire_bytes

            # One millisecond catches near-simultaneous producers without
            # charging a sparse stream a fixed 5 ms before every durable
            # frame. Busy backlogs never wait here.
            if len(batch) == 1 and not self._stop:
                deadline = time.monotonic() + min(
                    self._coalesce_s, self._max_window_s)
                while len(batch) < self._max_batch:
                    if self._pending:
                        candidate = self._pending[0]
                        if (
                            candidate.payload is not None
                            and batch_bytes + candidate.wire_bytes
                            > self._max_batch_bytes
                        ):
                            break
                        self._pending.popleft()
                        self._queued_bytes = max(
                            0,
                            self._queued_bytes - candidate.retained_bytes,
                        )
                        batch.append(candidate)
                        batch_bytes += candidate.wire_bytes
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._stop:
                        break
                    self._condition.wait(remaining)
            stop_after = self._stop and not self._pending
            self._condition.notify_all()

        with self._metrics_lock:
            self._metrics['max_batch_bytes'] = max(
                self._metrics['max_batch_bytes'], batch_bytes)
        return batch, stop_after

    def _run(self) -> None:
        while True:
            batch, stop_after = self._take_batch()
            if not batch:
                return
            storage_batch = [
                pending for pending in batch if pending.payload is not None
            ]
            try:
                if storage_batch:
                    response = self._client_provider(write=True).command(
                        'event.append_batch',
                        {'events': [item.payload for item in storage_batch]},
                        None,
                        priority='event',
                        deadline=max(2.0, self._max_window_s + 1.0),
                    )
                    results = response.get('results') or []
                    if len(results) != len(storage_batch):
                        raise StorageError(
                            'database_protocol_error',
                            'Storage event batch response length mismatch')
                    for pending, result in zip(storage_batch, results):
                        pending.result = result
                    completed_at = time.monotonic()
                    with self._metrics_lock:
                        self._metrics['batches'] += 1
                        self._metrics['inserted'] += int(
                            response.get('inserted') or 0)
                        self._metrics['deduplicated'] += int(
                            response.get('deduplicated') or 0)
                        self._persist_lags_ms.extend(
                            (completed_at - pending.enqueued_at) * 1000
                            for pending in storage_batch)
            except BaseException as exc:
                for pending in batch:
                    pending.error = exc
                with self._metrics_lock:
                    self._metrics['failed'] += len(storage_batch)
            else:
                # A producer invalidates read caches when it enqueues, but a
                # reader can repopulate an old snapshot before this async
                # transaction commits. Invalidate again at the durability
                # boundary. The callback is observational: a cache failure
                # must never turn an already-committed event into a reported
                # storage failure.
                if storage_batch and self._on_commit is not None:
                    task_ids = frozenset(
                        str(pending.payload['task_id'])
                        for pending in storage_batch
                        if pending.payload is not None)
                    try:
                        self._on_commit(task_ids)
                    except Exception as exc:
                        logger.exception(
                            'storage event commit callback failed: %s', exc)
            finally:
                for pending in batch:
                    pending.payload = None
                    pending.retained_bytes = 0
                    pending.done.set()
            if stop_after:
                return

    def close(self, timeout: float = 10.0) -> bool:
        with self._condition:
            if not self._closed:
                self._closed = True
                self._stop = True
                self._condition.notify_all()
        # A first bounded shutdown can time out while the sole storage RPC is
        # still settling.  Later lifecycle owners must be able to wait again;
        # treating close as a one-shot status probe would strand that drain.
        self._thread.join(timeout=max(0.1, timeout))
        return not self._thread.is_alive()

    def __enter__(self) -> 'StorageEventBatcher':
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ['StorageEventBatcher']
