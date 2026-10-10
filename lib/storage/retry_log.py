"""Rate-limited WARNING emission for storage client retry loops.

A retrying RPC sleeps inside a bounded backoff loop; without a throttle,
every attempt of a long brownout emits one WARNING and a sustained outage
becomes log spam that hides the first (most diagnostic) record. The process
DuplicateCoalescingFilter already collapses physical duplicates downstream,
but callers deserve a deliberate, key-stable signal: at most one record per
operation per interval, carrying operation, attempt, delay, and error class.

This helper owns no I/O and never raises; it is a pure in-memory throttle
with a hard key cap so a hostile operation-name stream cannot grow it.
"""

from __future__ import annotations

import threading
import time

_DEFAULT_INTERVAL_S = 30.0
_MAX_KEYS = 512


class RetryLogThrottle:
    """At most one admitted emission per key per interval."""

    def __init__(
        self,
        interval_s: float = _DEFAULT_INTERVAL_S,
        *,
        max_keys: int = _MAX_KEYS,
        clock=time.monotonic,
    ) -> None:
        self._interval_s = max(0.0, float(interval_s))
        self._max_keys = max(1, int(max_keys))
        self._clock = clock
        self._lock = threading.Lock()
        self._last_emitted: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        """Return True at most once per key per interval."""
        now = self._clock()
        with self._lock:
            previous = self._last_emitted.get(key)
            if previous is not None and now - previous < self._interval_s:
                return False
            if len(self._last_emitted) >= self._max_keys:
                # Bounded: a key flood resets rather than growing memory;
                # the interval throttle still applies to every real key.
                self._last_emitted.clear()
            self._last_emitted[key] = now
            return True


_shared_throttle = RetryLogThrottle()


def retry_log_throttle() -> RetryLogThrottle:
    """The process-shared throttle for storage client retry warnings."""
    return _shared_throttle


__all__ = ['RetryLogThrottle', 'retry_log_throttle']
