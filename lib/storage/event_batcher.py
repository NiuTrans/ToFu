"""Owned natural-key event batching with RAM/file overflow before storage."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import json
import tempfile
import threading
import time
from typing import Any, Callable

from lib.storage.client import StorageClient
from lib.storage.errors import ERROR_CODES, StorageError
from lib.storage.protocol import canonical_json
from lib.storage_event_policy import resolve_storage_event_budget
from lib.log import get_logger
from lib.storage_metric_policy import bounded_storage_metric_sample_capacity
from lib.storage.event_composition import EventBatchCompositionBudget
from lib.storage.frame_admission import FrameByteAdmission


logger = get_logger(__name__)

# Pending-node metadata is included in representation accounting.
_COMPOSITION_PENDING_ACCOUNTING_BYTES = 256


@dataclass(frozen=True, slots=True)
class _PrefixFailure:
    ordinal: int
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None
    operation_id: str
    request_not_dispatched: bool

    @classmethod
    def capture(cls, ordinal: int, error: BaseException) -> '_PrefixFailure':
        if isinstance(error, StorageError):
            message = error.message[:1024] if type(error.message) is str else 'Storage event prefix persistence failed'
            operation_id = error.operation_id[:128] if type(error.operation_id) is str else ''
            code = error.code if type(error.code) is str and error.code in ERROR_CODES else 'database_internal'
            retry_after = error.retry_after_ms
            if type(retry_after) is not int or not 0 <= retry_after < (1 << 64):
                retry_after = None
            return cls(ordinal, code, message, error.retryable is True,
                retry_after, operation_id, error.request_not_dispatched is True)
        return cls(ordinal, 'database_internal', 'Storage event batch failed', False, None, '', False)

    def error(self) -> StorageError:
        return StorageError(self.code, self.message, self.retryable,
            self.retry_after_ms, self.operation_id,
            request_not_dispatched=self.request_not_dispatched)


class _OwnedEventPayload:
    """Immutable event bytes, with one exact RAM/file representation lease."""

    def __init__(self, data, retained_bytes, owner):
        self._reservation = owner.reserve_frame(retained_bytes)
        self._data = None
        self._file = None
        try:
            if self._reservation.spilled:
                self._file = tempfile.TemporaryFile(mode='w+b')
                remaining = memoryview(data)
                while remaining:
                    written = self._file.write(remaining[:self._reservation.chunk_bytes])
                    if not written:
                        raise OSError('Storage event temporary payload write made no progress')
                    remaining = remaining[written:]
                self._file.flush()
            else:
                self._data = data
        except BaseException:
            self.close()
            raise

    def decode(self):
        if self._file is None:
            return json.loads(self._data)
        try:
            self._file.seek(0)
            return json.load(self._file)
        except OSError as error:
            raise StorageError('database_unavailable',
                'Storage event temporary payload could not be read',
                request_not_dispatched=True) from error
        except (ValueError, UnicodeError) as error:
            raise StorageError('database_protocol_error',
                'Storage event temporary payload is invalid',
                request_not_dispatched=True) from error

    def close(self):
        try:
            if self._file is not None:
                self._file.close()
            self._data = None
        finally:
            self._reservation.close()


def _release_pending_payload(pending):
    payload, pending.payload = pending.payload, None
    try:
        if isinstance(payload, _OwnedEventPayload):
            payload.close()
    finally:
        pending.retained_bytes = 0


@dataclass(slots=True)
class _PendingEvent:
    # None is an in-order durability barrier, never sent to storage.
    payload: dict[str, Any] | bytes | _OwnedEventPayload | None
    task_id: str = ''
    wire_bytes: int = 0
    retained_bytes: int = 0
    raw_event_json_bytes: int = 0
    task_id_bytes: int = 0
    event_type_bytes: int = 0
    logical_type_bytes: int = 0
    accepted_ordinal: int = 0
    barrier_prefix_ordinal: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class StorageEventBatcher:
    """Coalesce accepted events; excess retained bodies use owned files."""

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
        resolved_queue_byte_capacity = (
            event_budget.queue_byte_capacity
            if queue_byte_capacity is None else int(queue_byte_capacity)
        )
        resolved_max_batch_bytes = (
            event_budget.batch_max_bytes
            if max_batch_bytes is None else int(max_batch_bytes)
        )
        # Legacy count hints grant no admission quota.
        self._queue_capacity = 0
        self._queue_byte_capacity = max(
            1, min(1024 * 1024 * 1024, resolved_queue_byte_capacity))
        self._max_batch = max(
            1, min(event_budget.batch_max_events, int(max_batch)))
        self._max_batch_bytes = max(
            1,
            min(
                event_budget.batch_max_bytes,
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
        self._payload_buffers = FrameByteAdmission(capacity_bytes=self._queue_byte_capacity)
        self._pending: deque[_PendingEvent] = deque()
        self._queued_bytes = 0
        self._preparing_payloads = 0
        self._composition_budget: EventBatchCompositionBudget | None = None
        self._accepted_event_ordinal = 0
        # One bounded primitive summary, no traceback or payload graph.
        self._prefix_failure: _PrefixFailure | None = None
        self._condition = threading.Condition()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            'submitted': 0, 'batches': 0, 'inserted': 0,
            'deduplicated': 0, 'failed': 0, 'max_queue_depth': 0,
            'max_queue_bytes': 0, 'queue_rejections': 0,
            'cancelled_before_start': 0, 'max_batch_bytes': 0,
            'max_estimated_composition_bytes': 0,
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
            preparing_payloads = self._preparing_payloads
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
        result.update(self._payload_buffers.metrics())
        result['preparing_payloads'] = preparing_payloads
        result['queue_depth'] = queue_depth
        result['queued_bytes'] = queued_bytes
        result['batch_byte_capacity'] = self._max_batch_bytes
        result['event_byte_capacity'] = self._event_max_bytes
        composition = self._composition_budget
        result['composition_byte_capacity'] = (
            composition.composition_byte_capacity if composition is not None else 0)
        result['native_carried_payload_byte_capacity'] = (
            composition.maximum_carried_payload_bytes if composition is not None else 0)
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
        require_attempts_live: list[str] | None = None,
    ) -> _PendingEvent:
        payload = {
            'task_id': task_id,
            'sequence': sequence,
            'event': event,
        }
        if require_attempts_live is not None:
            # Conversation-attempt fence enforced by the storage authority
            # inside the append transaction (lib/tasks_pkg/event_log.py owns
            # shape validation and the rejection mapping).
            payload['require_attempts_live'] = list(require_attempts_live)
        try:
            encoded_payload = canonical_json(payload)
            wire_bytes = len(encoded_payload)
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
        # Reuse the existing full-wire encoded length. Encoding the small
        # envelope with null removes the event without encoding it again.
        # Optional estimation cannot introduce a new admission failure.
        try:
            raw_event_json_bytes = wire_bytes - len(canonical_json({**payload, 'event': None})) + 4
        except Exception:
            raw_event_json_bytes = wire_bytes
        task_id_bytes = len(task_id.encode('utf-8')) if type(task_id) is str else 2048
        if type(event) is dict:
            logical_type = event.get('type')
            if type(logical_type) is str:
                logical_type_bytes = len(logical_type[:128].encode('utf-8'))
                event_type_bytes = logical_type_bytes or len('task_event')
            else:
                logical_type_bytes, event_type_bytes = 0, len('task_event')
        else:
            # Non-object events have no type index. Mapping subclasses may
            # normalize to an object; charge the maximum without calling
            # arbitrary .get/encoding overrides after original admission.
            logical_type_bytes = event_type_bytes = 512
        return _PendingEvent(
            encoded_payload,
            task_id=task_id,
            wire_bytes=wire_bytes,
            # Retain the frozen canonical bytes, never the caller's mutable
            # graph. The queue node also participates in RAM/file accounting.
            retained_bytes=wire_bytes + 1_024 + _COMPOSITION_PENDING_ACCOUNTING_BYTES,
            raw_event_json_bytes=max(0, raw_event_json_bytes),
            task_id_bytes=task_id_bytes,
            event_type_bytes=event_type_bytes,
            logical_type_bytes=logical_type_bytes,
        )

    def _enqueue(self, pending: _PendingEvent) -> tuple[int, int]:
        with self._condition:
            if self._closed:
                raise StorageError('database_unavailable', 'Storage event batcher is closed')
            self._preparing_payloads += 1
        body = None
        try:
            if pending.payload is not None:
                data = pending.payload
                if isinstance(data, dict):
                    pending.task_id = str(data['task_id'])
                    data = canonical_json(data)
                try:
                    body = _OwnedEventPayload(data, pending.retained_bytes, self._payload_buffers)
                except OSError as error:
                    raise StorageError('database_unavailable',
                        'Storage event temporary payload could not be retained',
                        request_not_dispatched=True) from error
            with self._condition:
                if self._closed:
                    raise StorageError(
                        'database_unavailable', 'Storage event batcher is closed')
                if body is None:
                    pending.barrier_prefix_ordinal = self._accepted_event_ordinal
                else:
                    self._accepted_event_ordinal += 1
                    pending.accepted_ordinal = self._accepted_event_ordinal
                    pending.payload = body
                self._pending.append(pending)
                self._queued_bytes += pending.retained_bytes
                queue_depth = len(self._pending)
                queued_bytes = self._queued_bytes
                self._condition.notify()
            return queue_depth, queued_bytes
        except BaseException:
            if body is not None:
                body.close()
            raise
        finally:
            with self._condition:
                self._preparing_payloads -= 1
                self._condition.notify_all()

    def _cancel_queued(self, pending: _PendingEvent) -> bool:
        with self._condition:
            for index, candidate in enumerate(self._pending):
                if candidate is not pending:
                    continue
                del self._pending[index]
                self._note_prefix_failure_locked(pending, StorageError(
                    'database_timeout', 'Storage event confirmation timed out', True, 25))
                self._queued_bytes = max(
                    0, self._queued_bytes - pending.retained_bytes)
                _release_pending_payload(pending)
                pending.done.set()
                self._condition.notify_all()
                with self._metrics_lock:
                    self._metrics['cancelled_before_start'] += 1
                return True
        return False

    def _note_prefix_failure_locked(self, pending: _PendingEvent, error: BaseException) -> None:
        if pending.accepted_ordinal == 0:
            return
        if self._prefix_failure is None or pending.accepted_ordinal < self._prefix_failure.ordinal:
            self._prefix_failure = _PrefixFailure.capture(pending.accepted_ordinal, error)

    def append(
        self,
        task_id: str,
        sequence: int,
        event: Any,
        *,
        timeout: float = 2.0,
        wait: bool = True,
        require_attempts_live: list[str] | None = None,
    ) -> dict[str, Any]:
        pending = self._build_pending_event(
            task_id, sequence, event, require_attempts_live)
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

        The barrier shares the owned FIFO with events, so acceptance order is
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

    def _take_batch(self, *, barrier_only: bool = False,
                    expected_head: _PendingEvent | None = None) -> tuple[list[_PendingEvent], bool]:
        batch: list[_PendingEvent] = []
        batch_bytes = 0
        composition_bytes = 0
        composition = self._composition_budget
        def cost(pending: _PendingEvent) -> int:
            if composition is None or pending.payload is None:
                return 0
            return composition.estimate(pending.raw_event_json_bytes, pending.task_id_bytes,
                pending.event_type_bytes, pending.logical_type_bytes)
        with self._condition:
            while not self._pending and not self._stop:
                self._condition.wait()
            if not self._pending:
                return [], True
            if expected_head is not None and self._pending[0] is not expected_head:
                # Confirmation cancellation may remove the head while the
                # selected client is obtained. Do not apply its failure to a
                # later caller or claim a replacement barrier/event early.
                return [], False

            # Existing backlog is already coalesced. Keep FIFO items in the
            # deque when adding one would exceed the storage.v1 frame budget;
            # no pop/requeue operation can reorder a durability barrier.
            while self._pending and len(batch) < self._max_batch:
                candidate = self._pending[0]
                candidate_cost = cost(candidate)
                if (
                    candidate.payload is not None
                    and batch
                    and (batch_bytes + candidate.wire_bytes > self._max_batch_bytes
                         or (composition is not None and composition_bytes + candidate_cost
                             > composition.composition_byte_capacity))
                ):
                    break
                self._pending.popleft()
                self._queued_bytes = max(
                    0, self._queued_bytes - candidate.retained_bytes)
                batch.append(candidate)
                batch_bytes += candidate.wire_bytes
                composition_bytes += candidate_cost
                if barrier_only or candidate.payload is None:
                    break

            # One millisecond catches near-simultaneous producers without
            # charging a sparse stream a fixed 5 ms before every durable
            # frame. Busy backlogs never wait here.
            if len(batch) == 1 and not self._stop and not barrier_only:
                deadline = time.monotonic() + min(
                    self._coalesce_s, self._max_window_s)
                while len(batch) < self._max_batch:
                    if self._pending:
                        candidate = self._pending[0]
                        candidate_cost = cost(candidate)
                        if (
                            candidate.payload is not None
                            and (batch_bytes + candidate.wire_bytes > self._max_batch_bytes
                                 or (composition is not None and composition_bytes + candidate_cost
                                     > composition.composition_byte_capacity))
                        ):
                            break
                        self._pending.popleft()
                        self._queued_bytes = max(
                            0,
                            self._queued_bytes - candidate.retained_bytes,
                        )
                        batch.append(candidate)
                        batch_bytes += candidate.wire_bytes
                        composition_bytes += candidate_cost
                        if candidate.payload is None:
                            break
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
            self._metrics['max_estimated_composition_bytes'] = max(
                self._metrics['max_estimated_composition_bytes'], composition_bytes)
        return batch, stop_after

    def _run(self) -> None:
        while True:
            # Resolve the selected client/cached format declaration on this
            # worker only after work arrives, while all items remain cancellable
            # in the original deque. Future groups are never claimed early.
            with self._condition:
                while not self._pending and not self._stop:
                    self._condition.wait()
                if not self._pending:
                    return
                expected_head = self._pending[0]
                barrier_only = self._pending[0].payload is None
            client = None
            client_error = None
            if not barrier_only:
                try:
                    client = self._client_provider(write=True)
                except BaseException as exc:
                    client_error = exc
                else:
                    # Cached capability decoding is optional performance
                    # metadata. Its ordinary failures use legacy grouping;
                    # provider and command/source failures stay real errors.
                    # Do not intercept shutdown BaseExceptions in this reader.
                    try:
                        reader = getattr(client, 'event_batch_composition_budget', None)
                        composition = reader() if callable(reader) else None
                        self._composition_budget = (
                            composition if isinstance(composition, EventBatchCompositionBudget) else None)
                    except Exception:
                        self._composition_budget = None
            batch, stop_after = self._take_batch(barrier_only=barrier_only, expected_head=expected_head)
            if not batch:
                if stop_after:
                    return
                continue
            storage_batch = [
                pending for pending in batch if pending.payload is not None
            ]
            try:
                if storage_batch:
                    if client_error is not None:
                        raise client_error
                    response = client.command(
                        'event.append_batch',
                        {'events': [item.payload.decode() for item in storage_batch]},
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
                        if isinstance(result, dict) and result.get('rejected') == 'attempt_not_live':
                            # Preserve the individual business result and all
                            # sibling ACKs. This accepted event was not stored;
                            # a later prefix barrier cannot promise it durable.
                            with self._condition:
                                self._note_prefix_failure_locked(pending, StorageError(
                                    'database_conflict', 'Storage event attempt is not live'))
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
                with self._condition:
                    for pending in batch:
                        self._note_prefix_failure_locked(pending, exc)
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
                        pending.task_id
                        for pending in storage_batch
                        if pending.payload is not None)
                    try:
                        self._on_commit(task_ids)
                    except Exception as exc:
                        logger.exception(
                            'storage event commit callback failed: %s', exc)
            finally:
                for pending in batch:
                    if pending.payload is None:
                        with self._condition:
                            failure = self._prefix_failure
                            if failure is not None and failure.ordinal <= pending.barrier_prefix_ordinal:
                                pending.error = failure.error()
                    _release_pending_payload(pending)
                    pending.done.set()
            if stop_after:
                return

    def _fail_queued_locked(self, error: StorageError) -> int:
        """Complete every still-queued pending event with ``error``.

        Caller must hold ``self._condition``. Events already claimed by the
        worker keep the established ambiguous-commit contract; only work the
        worker has not started is failed here. Shared error instance is
        deliberate: the typed classification is the contract, and every
        event waiter gets the same immutable evidence. Barriers receive fresh
        typed errors for their own accepted prefix.
        """
        queued = list(self._pending)
        self._pending.clear()
        self._queued_bytes = 0
        self._condition.notify_all()
        for pending in queued:
            self._note_prefix_failure_locked(pending, error)
        for pending in queued:
            is_barrier = pending.payload is None
            _release_pending_payload(pending)
            if is_barrier:
                failure = self._prefix_failure
                pending.error = (failure.error() if failure is not None
                    and failure.ordinal <= pending.barrier_prefix_ordinal
                    else _PrefixFailure.capture(0, error).error())
            else:
                pending.error = error
            pending.done.set()
        return len(queued)

    def close(self, timeout: float = 10.0) -> bool:
        with self._condition:
            if not self._closed:
                self._closed = True
                self._stop = True
                self._condition.notify_all()
        # A first bounded shutdown can time out while the sole storage RPC is
        # still settling.  Later lifecycle owners must be able to wait again;
        # treating close as a one-shot status probe would strand that drain.
        wait_s = max(0.1, timeout)
        deadline = time.monotonic() + wait_s
        self._thread.join(timeout=wait_s)
        with self._condition:
            while not self._thread.is_alive() and self._preparing_payloads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if not self._thread.is_alive() and not self._preparing_payloads:
                self._prefix_failure = None
                return True
        # Fail-fast: producers blocked in append()/flush() on still-queued
        # events must not dangle behind a wedged worker. Complete the queue
        # with the typed availability error; the in-flight batch keeps its
        # own deadline and ambiguous-commit contract.
        with self._condition:
            failed = self._fail_queued_locked(StorageError(
                'database_unavailable',
                'Storage event batcher close timed out with events queued',
                True, 50))
        if failed:
            logger.warning(
                '[StorageEventBatcher] close timed out; failed %d queued '
                'event(s) fast', failed)
        return False

    def __enter__(self) -> 'StorageEventBatcher':
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ['StorageEventBatcher']
