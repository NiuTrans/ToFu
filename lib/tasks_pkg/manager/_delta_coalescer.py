"""Bounded coalescing for high-frequency assistant text deltas.

Responsibility: preserve byte order while reducing durable/UI event cadence.
Entry point: :class:`BoundedTextDeltaCoalescer`.
Dependencies: one synchronous emit callback plus one bounded daemon worker.

The first delta is emitted immediately for honest TTFT. Later deltas share a
short trailing window, with a hard character bound that preserves backpressure
when a provider produces data faster than storage can commit it. Callers flush
before every structural event and at the provider-stream boundary.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
import threading
import time
from typing import Callable

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger


logger = get_logger(__name__)

TEXT_DELTA_COALESCE_S = 0.1
TEXT_DELTA_MAX_CHARS = 256


class BoundedTextDeltaCoalescer:
    """Emit lossless text deltas with a leading edge and bounded trailing edge.

    ``emit`` runs under the buffer lock that ``add``/``flush`` take, so a
    provider callback waits for an emit in progress instead of overtaking the
    durable event sequence. ``emit_order`` names the lock ``emit`` itself takes
    to sequence the event; every emitter holds it before the buffer lock. An
    emitter waiting for another producer therefore holds no buffer lock:
    provider text keeps buffering, and once ``max_chars`` are pending the
    provider's own emit waits in that same order (backpressure).
    """

    def __init__(
        self,
        emit: Callable[[str, str], None],
        *,
        accumulate: Callable[[str, str], None] | None = None,
        delay_s: float = TEXT_DELTA_COALESCE_S,
        max_chars: int = TEXT_DELTA_MAX_CHARS,
        flush_deadline: Callable[[], float | None] | None = None,
        emit_order: Callable[[], AbstractContextManager] = nullcontext,
    ) -> None:
        self._emit = emit
        self._accumulate = accumulate
        self._delay_s = max(0.001, float(delay_s))
        self._max_chars = max(1, int(max_chars))
        self._deadline_observation = flush_deadline
        self._emit_order = emit_order
        self._condition = threading.Condition()
        self._flush_deadline: float | None = None
        self._worker: threading.Thread | None = None
        self._pending_content: list[str] = []
        self._pending_thinking: list[str] = []
        self._pending_chars = 0
        self._peak_pending_chars = 0
        self._first_emitted = False
        self._closed = False
        self._raw_chunks = 0
        self._raw_chars = 0
        self._emitted_events = 0
        self._failed_emits = 0

    def add(self, *, content: str = '', thinking: str = '') -> None:
        """Accept one provider delta without changing its text or order."""
        if not content and not thinking:
            return
        chunk_chars = len(content) + len(thinking)
        # An observation of the committed cadence (its own process lock); it
        # can only shorten the window, so read it before this buffer's lock.
        observed = None if self._deadline_observation is None else self._deadline_observation()
        with self._condition:
            if self._closed:
                raise RuntimeError('text delta coalescer is closed')
            # The cumulative task projection and this event buffer are one
            # ordering boundary. Updating projection state before acquiring
            # this lock lets a worker snapshot bytes whose delta has not yet
            # entered the buffer; updating it afterward makes the first
            # synchronous emit persist a stale projection.
            if self._accumulate is not None:
                self._accumulate(content, thinking)
            self._raw_chunks += 1
            self._raw_chars += chunk_chars
            if content:
                self._pending_content.append(content)
            if thinking:
                self._pending_thinking.append(thinking)
            self._pending_chars += chunk_chars
            self._peak_pending_chars = max(self._peak_pending_chars, self._pending_chars)
            if self._first_emitted and self._pending_chars < self._max_chars:
                deadline = self._flush_deadline
                if deadline is None:
                    deadline = time.monotonic() + self._delay_s
                if observed is not None:
                    deadline = min(deadline, observed)
                if deadline != self._flush_deadline:
                    self._flush_deadline = deadline
                    self._ensure_worker_locked()
                    self._condition.notify_all()
                return
        # The leading edge and the character bound emit now, in order.
        with self._emit_order(), self._condition:
            if self._closed:
                return
            if not self._first_emitted:
                # Use the same retained buffer as every later flush. If the
                # synchronous leading-edge emit fails, close/error handling
                # can retry without losing bytes already accumulated in the
                # task projection.
                self._flush_locked()
                self._first_emitted = True
            elif self._pending_chars >= self._max_chars:
                self._flush_deadline = None
                self._flush_locked()
                self._condition.notify_all()

    def flush(self) -> bool:
        """Synchronously emit pending text before a structural boundary."""
        with self._emit_order(), self._condition:
            if self._closed:
                return False
            self._flush_deadline = None
            flushed = self._flush_locked()
            self._condition.notify_all()
            return flushed

    def close(self) -> bool:
        """Flush once and prevent late worker/provider emissions."""
        error: BaseException | None = None
        with self._emit_order(), self._condition:
            if self._closed:
                return False
            self._flush_deadline = None
            try:
                flushed = self._flush_locked()
            except BaseException as exc:
                logger.debug(
                    '[DeltaCoalescer] final flush failed before close: %s',
                    type(exc).__name__,
                )
                flushed = False
                error = exc
            self._closed = True
            worker = self._worker
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(1.0, self._delay_s * 2))
        if error is not None:
            raise error
        return flushed

    @property
    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                'raw_chunks': self._raw_chunks,
                'raw_chars': self._raw_chars,
                'emitted_events': self._emitted_events,
                'failed_emits': self._failed_emits,
                'pending_chars': self._pending_chars,
                'peak_pending_chars': self._peak_pending_chars,
                'worker_started': int(self._worker is not None),
                'worker_alive': int(
                    self._worker is not None and self._worker.is_alive()),
            }

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name='tofu-text-delta-coalescer',
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._closed:
                    if self._flush_deadline is None:
                        self._condition.wait()
                        continue
                    remaining = self._flush_deadline - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    break
                if self._closed:
                    return
            try:
                # Wait for the event order without the buffer lock: provider
                # text keeps buffering meanwhile. Recheck what is still due.
                with self._emit_order(), self._condition:
                    deadline = self._flush_deadline
                    if self._closed or deadline is None or deadline > time.monotonic():
                        continue
                    self._flush_deadline = None
                    self._flush_locked()
            except BaseException as exc:
                # Pending parts clear only after a successful emit.
                # The next callback or boundary flush retries them.
                logger.exception('background text-delta flush failed: %s', exc)

    def _flush_locked(self) -> bool:
        if self._pending_chars <= 0:
            return False
        content = ''.join(self._pending_content)
        thinking = ''.join(self._pending_thinking)
        try:
            self._emit(content, thinking)
        except BaseException:
            self._failed_emits += 1
            raise
        self._pending_content.clear()
        self._pending_thinking.clear()
        self._pending_chars = 0
        self._emitted_events += 1
        return True


class TaskTextDeltaCoalescer:
    """Own task accumulation, event emission, boundaries, and worker cleanup."""

    def __init__(
        self,
        task: dict,
        append_event_fn: Callable[[dict, dict], None],
        *,
        on_first_delta: Callable[[], None],
        on_after_delta: Callable[[], None],
        log_prefix: str = '',
        delay_s: float = TEXT_DELTA_COALESCE_S,
        max_chars: int | None = None,
    ) -> None:
        self._task = task
        self._append_event = append_event_fn
        self._on_first_delta = on_first_delta
        self._on_after_delta = on_after_delta
        self._log_prefix = log_prefix
        self._held_content: list[str] = []
        self._held_lock = threading.Lock()
        deadline = None
        attempt_id = task.get('_attemptId') or task.get('attemptId')
        if attempt_id:
            from lib.turn_lifecycle import next_progress_projection_deadline
            deadline = lambda: next_progress_projection_deadline(attempt_id)
            if max_chars is None:
                from lib.storage.storage_event_policy import (
                    resolve_storage_event_budget, STORAGE_EVENT_FRAME_HEADROOM_BYTES,
                )
                from lib.agent_core.generated_task_event_delivery_contract import PROGRESS_BATCH_MAX_WIRE_BYTES
                # Reuse both existing frame envelopes and metadata headroom.
                # JSON control characters need six bytes; the short deadline,
                # not a tiny character cap, sets ordinary streaming cadence.
                frame_bytes = min(resolve_storage_event_budget().event_max_bytes,
                    max(1, PROGRESS_BATCH_MAX_WIRE_BYTES - STORAGE_EVENT_FRAME_HEADROOM_BYTES))
                max_chars = max(1, frame_bytes // 6)
        if max_chars is None:
            max_chars = TEXT_DELTA_MAX_CHARS
        self._coalescer = BoundedTextDeltaCoalescer(
            self._emit,
            accumulate=self._accumulate,
            delay_s=delay_s,
            max_chars=max_chars,
            flush_deadline=deadline,
            emit_order=self._event_order,
        )

    def on_thinking(self, value: str) -> None:
        self._on_first_delta()
        self._coalescer.add(thinking=value)
        self._on_after_delta()

    def _release_held_content(self, *, discard: bool = False) -> None:
        from lib.tasks_pkg.stream_handler._router_silence import hold_silent_text_delta
        with self._held_lock:
            candidate = ''.join(self._held_content)
            if not candidate:
                return
            if discard or not hold_silent_text_delta(self._task, candidate):
                self._held_content.clear()
                if not discard:
                    self._coalescer.add(content=candidate)

    def on_content(self, value: str) -> None:
        self._on_first_delta()
        with self._held_lock:
            self._held_content.append(value)
        self._release_held_content()
        self._on_after_delta()

    def flush(self) -> bool:
        # Before tools/retries an incomplete declaration is ordinary text. Only
        # the terminal close may discard an exactly recognized silent outcome.
        with self._held_lock:
            candidate = ''.join(self._held_content)
            if candidate and not candidate.rstrip().endswith(']'):
                self._held_content.clear()
                self._coalescer.add(content=candidate)
        self._release_held_content()
        return self._coalescer.flush()

    def discard_held_content(self) -> None:
        """Drop a candidate only when its provider attempt is discarded."""
        with self._held_lock:
            self._held_content.clear()

    def wrap_boundary(self, callback: Callable) -> Callable:
        """Return a callback that flushes text before one structural action."""
        def _after_text(*args, **kwargs):
            self.flush()
            return callback(*args, **kwargs)

        return _after_text

    def close(self) -> bool:
        self.flush()
        flushed = self._coalescer.close()
        stats = self._coalescer.stats
        if stats['raw_chunks'] > stats['emitted_events']:
            logger.debug(
                '%s text deltas coalesced %d provider chunks into %d durable '
                'events (%d chars)',
                self._log_prefix, stats['raw_chunks'], stats['emitted_events'],
                stats['raw_chars'],
            )
        return flushed

    def close_after_error(self, error: Exception) -> None:
        try:
            self.close()
        except Exception as flush_error:
            logger.warning(
                '%s final text-delta flush failed while handling %s: %s',
                self._log_prefix, type(error).__name__, flush_error)

    @property
    def stats(self) -> dict[str, int]:
        return self._coalescer.stats

    def _event_order(self) -> AbstractContextManager:
        from lib.tasks_pkg.event_delivery import task_event_ordering
        return task_event_ordering(self._task)

    def _accumulate(self, content: str, thinking: str) -> None:
        with self._task['content_lock']:
            if content:
                self._task['content'] += content
            if thinking:
                self._task['thinking'] += thinking

    def _emit(self, content: str, thinking: str) -> None:
        fields = {}
        if content:
            fields['content'] = content
        if thinking:
            fields['thinking'] = thinking
        self._append_event(
            self._task, build_event(EventType.DELTA, **fields))


__all__ = [
    'BoundedTextDeltaCoalescer',
    'TaskTextDeltaCoalescer',
    'TEXT_DELTA_COALESCE_S',
    'TEXT_DELTA_MAX_CHARS',
]
