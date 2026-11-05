"""Queued read coalescing for synchronous, freshness-sensitive reads.

Responsibility
--------------
Share one backing read among callers with an identical request without ever
returning a value that predates a caller's arrival. Per key at most one read
runs; callers arriving while it runs join one queued read that starts only
after the running read settles, so any burst of concurrent callers costs at
most two reads. There is no gather window, capacity or retention: an idle key
holds no state and completed values are never cached.

Entry points
------------
``QueuedReadCoalescer.run`` returns the caller's owned or shared read value.
``QueuedReadCoalescer.snapshot`` exposes coordination counters.

Dependencies
------------
The caller owns key construction (``load`` receives only the key, so the key
must carry every input that changes the result) and the copy that gives each
sharer an independent value. This coordinator owns no executor or thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar


Key = TypeVar("Key", bound=Hashable)
Value = TypeVar("Value")


@dataclass(slots=True, eq=False)
class _Read(Generic[Value]):
    done: threading.Event = field(default_factory=threading.Event)
    callers: int = 1
    value: Value | None = None
    error: BaseException | None = None


@dataclass(slots=True, eq=False)
class _Lane(Generic[Value]):
    running: _Read[Value] | None = None
    queued: _Read[Value] | None = None


class QueuedReadCoalescer(Generic[Key, Value]):
    """Share only reads that start after every one of their callers arrived."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lanes: dict[Key, _Lane[Value]] = {}
        self._reads = 0
        self._joined = 0

    def run(
        self,
        key: Key,
        load: Callable[[Key], Value],
        *,
        copy: Callable[[Value], Value] | None = None,
    ) -> Value:
        """Return ``load(key)`` from a read that started after this call.

        A read with several callers hands each of them ``copy(value)``; only
        omit ``copy`` for values nobody mutates. A failed read raises its
        exception in every caller that shared it.
        """
        with self._lock:
            lane = self._lanes.get(key)
            if lane is None:
                lane = self._lanes[key] = _Lane()
            if lane.running is None:
                read, predecessor, leads = _Read(), None, True
                lane.running = read
            elif lane.queued is None:
                read, predecessor, leads = _Read(), lane.running, True
                lane.queued = read
            else:
                read, predecessor, leads = lane.queued, None, False
                read.callers += 1
                self._joined += 1
        if not leads:
            read.done.wait()
            if read.error is not None:
                raise read.error
            return read.value if copy is None else copy(read.value)

        try:
            if predecessor is not None:
                # Freshness boundary: the running read may predate this lane's
                # callers, so the queued read starts only after it settles.
                predecessor.done.wait()
            with self._lock:
                self._reads += 1
            value = load(key)
        except BaseException as error:
            self._settle(key, lane, read, None, error)
            raise
        if self._settle(key, lane, read, value, None) == 1 or copy is None:
            return value
        return copy(value)

    def _settle(
        self,
        key: Key,
        lane: _Lane[Value],
        read: _Read[Value],
        value: Value | None,
        error: BaseException | None,
    ) -> int:
        with self._lock:
            if lane.running is read:
                # Promote before waking the queued leader so no caller can
                # join a read that is about to start.
                lane.running, lane.queued = lane.queued, None
            elif lane.queued is read:
                lane.queued = None
            if (lane.running is None and lane.queued is None
                    and self._lanes.get(key) is lane):
                del self._lanes[key]
            read.value = value
            read.error = error
            callers = read.callers
        read.done.set()
        return callers

    def snapshot(self) -> dict[str, int]:
        """Return coordination counters without exposing live reads."""
        with self._lock:
            return {
                "activeKeys": len(self._lanes),
                "running": sum(
                    lane.running is not None for lane in self._lanes.values()),
                "queuedCallers": sum(
                    lane.queued.callers
                    for lane in self._lanes.values()
                    if lane.queued is not None),
                "reads": self._reads,
                "joined": self._joined,
            }


__all__ = ["QueuedReadCoalescer"]
