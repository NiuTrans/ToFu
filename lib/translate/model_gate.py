"""Track actual translation-provider calls without local admission quotas.

Entry points: translation_model_slot owns one live call until its context exits;
translation_model_gate_snapshot exposes aggregate lifecycle counters. Actual
provider health, billing, deadlines and cancellation belong to their existing
boundaries. Independent calls never wait for a process-local count permit.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
import threading

from lib.log import get_logger
from lib.observability import publish_executor_state

logger = get_logger(__name__)


class TranslationModelGate:
    """Exact live-call accounting; no active or pending count ceiling."""

    def __init__(self, *, metric_pool: str = 'translation-provider') -> None:
        self.metric_pool = str(metric_pool or 'translation-provider')
        self._lock = threading.RLock()
        self._active = 0
        self._acquired = 0
        self._cancelled_waits = 0
        self._peak_active = 0
        self._publish_locked()

    @contextmanager
    def slot(self, *, abort_check: Callable[[], bool] | None = None) -> Iterator[None]:
        if abort_check is not None and not callable(abort_check):
            raise TypeError('abort_check must be callable or None')
        with self._lock:
            if abort_check is not None and abort_check():
                self._cancelled_waits += 1
                from lib.llm_errors import AbortedError
                raise AbortedError('Translation aborted before provider call')
            self._active += 1
            self._acquired += 1
            self._peak_active = max(self._peak_active, self._active)
            self._publish_locked()
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                self._publish_locked()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                'capacity': 0, 'waitingCapacity': 0,
                'active': self._active, 'waiting': 0,
                'acquired': self._acquired,
                'cancelledWaits': self._cancelled_waits,
                'rejectedWaits': 0,
                'peakActive': self._peak_active, 'peakWaiting': 0,
            }

    def _publish_locked(self) -> None:
        try:
            publish_executor_state(self.metric_pool, workers=self._active,
                                   queued=0, active=self._active, resident_threads=0)
        except Exception as exc:
            logger.debug('[TranslateGate] state metric skipped: %s', exc)


_translation_model_gate = TranslationModelGate()


def translation_model_slot(*, abort_check: Callable[[], bool] | None = None) -> AbstractContextManager[None]:
    return _translation_model_gate.slot(abort_check=abort_check)


def translation_model_gate_snapshot() -> dict[str, int]:
    return _translation_model_gate.snapshot()


__all__ = ['TranslationModelGate', 'translation_model_slot',
           'translation_model_gate_snapshot']
