"""The deadline one storage writer may ask the selected authority to take.

Responsibility
--------------
The native authority admits foreground work through one FIFO queue in front of
one exclusive authority mutex, and it charges that queue wait against the
caller's deadline: when the wait exhausts the deadline the authority answers
``deadline_elapsed`` and the storage client reports ``database_timeout``. A
writer therefore has to budget for the authority's *observed* end-to-end
service time (queue wait included), not for a constant of its own. A constant
below the authority's service tail refuses ordinary writes under congestion,
and the refusal repeats instead of clearing: the client's attempts inside one
congestion episode are correlated, so every replay re-enters the congestion
that refused the first attempt. The same ghost
therefore reappears under the next writer — 2026-10-14 ``credential.touch``,
2026-10-22 ``task_results.checkpoint`` and ``event.append_batch`` — which is
why a per-writer constant is never the fix.

Entry points
------------
``observe_storage_service`` records one completed client-side call.
``storage_write_deadline`` answers how long one writer may ask the authority
to take. The per-operation window answers when it holds enough samples; the
pooled window of the same kind answers otherwise, so a cold operation
inherits the authority's measured service time instead of a guess.

Dependencies: the standard library only.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable


#: Completed calls retained per operation and per kind. The window is a
#: recent-sample ring: a slower authority ages out of it and the estimate
#: returns to the observed service time of the authority in use.
_WINDOW = 64

#: Distinct operations retained. Operation names come from the closed
#: operation catalog, so this bound only caps a long-lived process.
_OPERATION_LIMIT = 64

#: Samples an operation needs before its own window answers. Below this the
#: pooled window of the same kind is the better estimate.
_OPERATION_MINIMUM_SAMPLES = 8

#: The budget covers the observed service tail with headroom. One congestion
#: episode refuses every attempt inside it, so a budget equal to the tail
#: would fail all attempts together; the factor keeps a single tail event
#: from refusing the whole episode.
_SERVICE_FACTOR = 4.0

#: Cold-start budget. A healthy authority answers a foreground write in well
#: under this (measured p50 25 ms, p90 100 ms against the live authority);
#: the floor only covers the first calls before any observation exists.
_DEFAULT_FLOOR_SECONDS = 1.0

#: Hard ceiling. A writer never buys an unbounded wait, whatever the
#: authority is observed to do.
_DEFAULT_CEILING_SECONDS = 30.0

#: Quantile of the observed window that the budget must cover.
_QUANTILE = 0.99


def _nearest_rank(samples: Iterable[float], quantile: float) -> float:
    """Return the nearest-rank quantile of a non-empty sample sequence."""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError('quantile of an empty sample sequence')
    rank = int(-(-len(ordered) * quantile // 1))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


class StorageServiceObservations:
    """Bounded recent-sample record of completed authority calls."""

    def __init__(self, *, window: int = _WINDOW,
                 operation_limit: int = _OPERATION_LIMIT) -> None:
        self._window = max(1, int(window))
        self._operation_limit = max(1, int(operation_limit))
        self._lock = threading.Lock()
        self._operations: dict[tuple[str, str], deque[float]] = {}
        self._kinds: dict[str, deque[float]] = {}
        self._order: deque[tuple[str, str]] = deque()

    def observe(self, kind: str, operation: str, seconds: float) -> None:
        """Record one completed call. Refusals and errors are not service
        time: their duration is censored by the caller's own budget."""
        try:
            duration = float(seconds)
        except (TypeError, ValueError):
            return
        if duration <= 0.0:
            return
        kind = str(kind or 'command')
        operation = str(operation or 'unknown')
        with self._lock:
            key = (kind, operation)
            samples = self._operations.get(key)
            if samples is None:
                samples = deque(maxlen=self._window)
                self._operations[key] = samples
                self._order.append(key)
                while len(self._order) > self._operation_limit:
                    self._operations.pop(self._order.popleft(), None)
            samples.append(duration)
            pooled = self._kinds.get(kind)
            if pooled is None:
                pooled = deque(maxlen=self._window)
                self._kinds[kind] = pooled
            pooled.append(duration)

    def estimate(self, kind: str, operation: str) -> float | None:
        """Return the observed service time for one operation, or None when
        neither its own window nor its kind's pooled window holds enough
        samples to answer."""
        with self._lock:
            samples = list(self._operations.get((str(kind), str(operation)), ()))
            pooled = list(self._kinds.get(str(kind), ()))
        if len(samples) >= _OPERATION_MINIMUM_SAMPLES:
            return _nearest_rank(samples, _QUANTILE)
        if len(pooled) >= _OPERATION_MINIMUM_SAMPLES:
            return _nearest_rank(pooled, _QUANTILE)
        return None

    def reset(self) -> None:
        """Drop every recorded sample."""
        with self._lock:
            self._operations.clear()
            self._kinds.clear()
            self._order.clear()


_PROCESS_OBSERVATIONS = StorageServiceObservations()


def observe_storage_service(kind: str, operation: str, seconds: float) -> None:
    """Record one completed client-side authority call."""
    _PROCESS_OBSERVATIONS.observe(kind, operation, seconds)


def storage_write_deadline(
    operation: str,
    *,
    kind: str = 'command',
    floor_seconds: float | None = None,
    ceiling_seconds: float | None = None,
) -> float:
    """Return how long one writer may ask the authority to take.

    The answer is the observed service time of that operation (or of the same
    kind of call), scaled by the service factor and clamped into the caller's
    floor and ceiling. A caller that already owns a larger budget keeps it:
    this policy only raises a budget that the authority has been observed to
    outrun.
    """
    floor = (_DEFAULT_FLOOR_SECONDS if floor_seconds is None
             else max(0.001, float(floor_seconds)))
    ceiling = (_DEFAULT_CEILING_SECONDS if ceiling_seconds is None
               else max(floor, float(ceiling_seconds)))
    estimate = _PROCESS_OBSERVATIONS.estimate(kind, operation)
    if estimate is None:
        return floor
    return max(floor, min(ceiling, estimate * _SERVICE_FACTOR))


def storage_service_estimate(kind: str, operation: str) -> float | None:
    """Return the observed service time of one operation, or None when the
    recorded samples cannot answer."""
    return _PROCESS_OBSERVATIONS.estimate(kind, operation)


def reset_storage_service_observations() -> None:
    """Drop every recorded sample (process restart and test seam)."""
    _PROCESS_OBSERVATIONS.reset()


__all__ = [
    'StorageServiceObservations',
    'observe_storage_service',
    'reset_storage_service_observations',
    'storage_service_estimate',
    'storage_write_deadline',
]
