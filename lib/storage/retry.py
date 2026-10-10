"""Bounded retry for transient storage-authority classifications.

``deadline_elapsed`` (``database_timeout``), ``database_busy`` and
``database_unavailable`` describe a temporarily unreachable or over-deadline
authority — never a durable verdict about the operation itself. Callers that
hold durable user state retry the identical operation a bounded number of
times and then fail honestly; nothing is ever skipped or dropped.

Replay safety is structural, not flag-based: the wire ``retryable`` flag is
not required because the authority marks ``deadline_elapsed`` non-retryable
only to gate command replay, while both server variants are safe to replay
for an identical operation — the admission variant rejects pre-dispatch, the
execution variant aborts the transaction pre-commit, and committed commands
always leave a durable ``(tenant, owner, command_id)`` receipt that returns
the recorded outcome instead of double-applying (the same classification
``lib/evolution/records.py`` already applies to its reads and writes).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

T = TypeVar('T')

#: Storage classifications that are always safe to retry with the identical
#: operation (see the module docstring for the replay-safety argument).
TRANSIENT_STORAGE_CODES = frozenset({
    'database_timeout',
    'database_busy',
    'database_unavailable',
})

#: Default bounded backoff schedule (seconds): ~7s of waiting on top of the
#: per-attempt RPC budgets, so a brief authority brownout (WAL rotation,
#: checkpoint, pressure spike) degrades to a wait instead of a fatal error.
DEFAULT_TRANSIENT_DELAYS_S = (1.0, 2.0, 4.0)


def is_transient_storage_error(exc: BaseException | None) -> bool:
    """Classify by code through the cause chain.

    Wrappers such as ``EventDurabilityError`` lose the original
    ``StorageError`` flag surface but keep it as ``__cause__``; walk the
    chain (bounded) so classification happens at the site that catches.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 8:
        if (isinstance(current, StorageError)
                and current.code in TRANSIENT_STORAGE_CODES):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def call_with_transient_retry(
    fn: Callable[[], T],
    *,
    delays: tuple[float, ...] = DEFAULT_TRANSIENT_DELAYS_S,
    abort_event=None,
    label: str = 'storage operation',
) -> T:
    """Run ``fn``, retrying transient storage classifications with backoff.

    Non-transient errors and budget exhaustion re-raise unchanged. When
    ``abort_event`` is supplied the wait is interruptible: an owner abort
    (user stop, invocation teardown) during a backoff re-raises the last
    error immediately instead of sleeping out the schedule.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= len(delays) or not is_transient_storage_error(exc):
                raise
            delay = delays[attempt]
            attempt += 1
            logger.warning(
                '[StorageRetry] transient %s failure (attempt %d/%d): %s '
                '— retrying in %.1fs',
                label, attempt, len(delays) + 1, exc, delay)
            if abort_event is not None:
                if abort_event.wait(timeout=delay):
                    raise
            else:
                time.sleep(delay)


__all__ = [
    'DEFAULT_TRANSIENT_DELAYS_S',
    'TRANSIENT_STORAGE_CODES',
    'call_with_transient_retry',
    'is_transient_storage_error',
]
