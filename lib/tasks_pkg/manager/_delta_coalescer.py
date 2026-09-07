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

    ``emit`` is serialized under the same lock as ``add``/``flush``. If the
    worker reaches storage first, the provider callback therefore applies
    backpressure instead of growing another in-memory buffer or overtaking the
    durable event sequence.
    """

    def __init__(
        self,
        emit: Callable[[str, str], None],
        *,
        accumulate: Callable[[str, str], None] | None = None,
        delay_s: float = TEXT_DELTA_COALESCE_S,
        max_chars: int = TEXT_DELTA_MAX_CHARS,
    ) -> None:
        self._emit = emit
        self._accumulate = accumulate
        self._delay_s = max(0.001, float(delay_s))
        self._max_chars = max(1, int(max_chars))
        self._condition = threading.Condition()
        self._flush_deadline: float | None = None
        self._worker: threading.Thread | None = None
        self._pending_content: list[str] = []
        self._pending_thinking: list[str] = []
        self._pending_chars = 0
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
            if not self._first_emitted:
                # Use the same retained buffer as every later flush. If the
                # synchronous leading-edge emit fails, close/error handling
                # can retry without losing bytes already accumulated in the
                # task projection.
                self._flush_locked()
                self._first_emitted = True
                return
            if self._pending_chars >= self._max_chars:
                self._flush_deadline = None
                self._flush_locked()
                self._condition.notify_all()
            elif self._flush_deadline is None:
                self._flush_deadline = time.monotonic() + self._delay_s
                self._ensure_worker_locked()
                self._condition.notify_all()

    def flush(self) -> bool:
        """Synchronously emit pending text before a structural boundary."""
        with self._condition:
            if self._closed:
                return False
            self._flush_deadline = None
            flushed = self._flush_locked()
            self._condition.notify_all()
            return flushed

    def close(self) -> bool:
        """Flush once and prevent late worker/provider emissions."""
        error: BaseException | None = None
        with self._condition:
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
                    self._flush_deadline = None
                    try:
                        self._flush_locked()
                    except BaseException as exc:
                        # Pending parts clear only after a successful emit.
                        # The next callback or boundary flush retries them.
                        logger.exception(
                            'background text-delta flush failed: %s', exc)
                    break
                if self._closed:
                    return

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
        max_chars: int = TEXT_DELTA_MAX_CHARS,
    ) -> None:
        self._task = task
        self._append_event = append_event_fn
        self._on_first_delta = on_first_delta
        self._on_after_delta = on_after_delta
        self._log_prefix = log_prefix
        self._coalescer = BoundedTextDeltaCoalescer(
            self._emit,
            accumulate=self._accumulate,
            delay_s=delay_s,
            max_chars=max_chars,
        )

    def on_thinking(self, value: str) -> None:
        self._on_first_delta()
        self._coalescer.add(thinking=value)
        self._on_after_delta()

    def on_content(self, value: str) -> None:
        self._on_first_delta()
        self._coalescer.add(content=value)
        self._on_after_delta()

    def flush(self) -> bool:
        return self._coalescer.flush()

    def wrap_boundary(self, callback: Callable) -> Callable:
        """Return a callback that flushes text before one structural action."""
        def _after_text(*args, **kwargs):
            self.flush()
            return callback(*args, **kwargs)

        return _after_text

    def close(self) -> bool:
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
