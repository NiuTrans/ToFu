"""Bound actual translation-provider concurrency across every caller.

Responsibility
--------------
General/PPTX/paper tasks already enter a finite worker lane, while incremental
previews and synchronous send translation have their own bounded carriers.
This module is the final provider boundary shared by those paths: FIFO waiters,
cooperative cancellation, and launch-probed hard active/waiting ceilings.

Entry points
------------
``translation_model_slot`` wraps one MT or LLM dispatch. Cache, identity, and
protected-only fast paths never enter it. ``translation_model_gate_snapshot``
exposes aggregate capacity evidence without task or owner identifiers.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import threading
import time

from lib.log import get_logger
from lib.observability import (
    observe_executor_queue_wait,
    publish_executor_state,
)
from lib.translate.errors import TranslationProviderQueueFull
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)


class TranslationModelGate:
    """A finite FIFO concurrency gate with cancellable admission waits."""

    def __init__(
        self,
        capacity: int,
        *,
        waiting_capacity: int,
        cancellation_poll_seconds: float = 0.1,
        metric_pool: str = 'translation-provider',
    ) -> None:
        if (isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity <= 0):
            raise ValueError('capacity must be a positive integer')
        if (isinstance(waiting_capacity, bool)
                or not isinstance(waiting_capacity, int)
                or waiting_capacity <= 0):
            raise ValueError('waiting_capacity must be a positive integer')
        if cancellation_poll_seconds <= 0:
            raise ValueError('cancellation_poll_seconds must be positive')
        self.capacity = int(capacity)
        self.waiting_capacity = int(waiting_capacity)
        self.cancellation_poll_seconds = float(cancellation_poll_seconds)
        self.metric_pool = str(metric_pool or 'translation-provider')
        self._condition = threading.Condition(threading.RLock())
        self._waiters: deque[object] = deque()
        self._active = 0
        self._acquired = 0
        self._cancelled_waits = 0
        self._rejected_waits = 0
        self._peak_active = 0
        self._peak_waiting = 0
        self._publish_locked()

    @contextmanager
    def slot(
        self,
        *,
        abort_check: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        """Enter one provider call in FIFO order or abort while waiting."""
        if abort_check is not None and not callable(abort_check):
            raise TypeError('abort_check must be callable or None')
        if abort_check is not None and abort_check():
            self._raise_cancelled()

        waiter = object()
        wait_started = time.monotonic()
        with self._condition:
            if len(self._waiters) >= self.waiting_capacity:
                self._rejected_waits += 1
                self._publish_locked()
                raise TranslationProviderQueueFull(
                    capacity=self.waiting_capacity)
            self._waiters.append(waiter)
            self._peak_waiting = max(
                self._peak_waiting, len(self._waiters))
            self._publish_locked()
            try:
                while (self._waiters[0] is not waiter
                       or self._active >= self.capacity):
                    if abort_check is not None and abort_check():
                        self._cancelled_waits += 1
                        self._raise_cancelled()
                    self._condition.wait(
                        self.cancellation_poll_seconds
                        if abort_check is not None else None)
                if abort_check is not None and abort_check():
                    self._cancelled_waits += 1
                    self._raise_cancelled()
                self._waiters.popleft()
                self._active += 1
                self._acquired += 1
                self._peak_active = max(self._peak_active, self._active)
                self._publish_locked()
                # Wake the next FIFO waiter immediately when capacity > 1.
                self._condition.notify_all()
            except BaseException:
                self._remove_waiter_locked(waiter)
                self._publish_locked()
                self._condition.notify_all()
                raise

        self._observe_wait(wait_started)
        try:
            yield
        finally:
            with self._condition:
                self._active = max(0, self._active - 1)
                self._publish_locked()
                self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        """Return aggregate gate state without caller identities."""
        with self._condition:
            return {
                'capacity': self.capacity,
                'waitingCapacity': self.waiting_capacity,
                'active': self._active,
                'waiting': len(self._waiters),
                'acquired': self._acquired,
                'cancelledWaits': self._cancelled_waits,
                'rejectedWaits': self._rejected_waits,
                'peakActive': self._peak_active,
                'peakWaiting': self._peak_waiting,
            }

    def _remove_waiter_locked(self, waiter: object) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    @staticmethod
    def _raise_cancelled() -> None:
        from lib.llm_errors import AbortedError

        raise AbortedError(
            'Translation aborted while waiting for provider capacity')

    def _observe_wait(self, wait_started: float) -> None:
        try:
            observe_executor_queue_wait(
                self.metric_pool,
                max(0.0, time.monotonic() - wait_started),
            )
        except Exception as exc:
            logger.debug(
                '[TranslateGate] queue-wait metric skipped: %s', exc)

    def _publish_locked(self) -> None:
        try:
            publish_executor_state(
                self.metric_pool,
                workers=self.capacity,
                queued=len(self._waiters),
                active=self._active,
                resident_threads=0,
            )
        except Exception as exc:
            # Observability is never authority over admission or release.
            logger.debug('[TranslateGate] state metric skipped: %s', exc)


_translation_model_gate = TranslationModelGate(
    resolve_resource_budget('TOFU_TRANSLATE_WORKERS', maximum=64),
    waiting_capacity=resolve_resource_budget(
        'TOFU_TRANSLATE_QUEUE_CAPACITY', maximum=1024),
)


def translation_model_slot(
    *,
    abort_check: Callable[[], bool] | None = None,
) -> AbstractContextManager[None]:
    """Return a context manager for one actual translation-provider call."""
    return _translation_model_gate.slot(abort_check=abort_check)


def translation_model_gate_snapshot() -> dict[str, int]:
    """Expose bounded concurrency evidence for diagnostics and tests."""
    return _translation_model_gate.snapshot()


__all__ = [
    'TranslationModelGate',
    'TranslationProviderQueueFull',
    'translation_model_gate_snapshot',
    'translation_model_slot',
]
