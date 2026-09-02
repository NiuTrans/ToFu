"""Bounded arrival coalescing for synchronous, freshness-sensitive reads.

Responsibility
--------------
Share one backing read among equivalent callers that arrive during a short
gather window.  The flight is removed before the read begins, so later callers
cannot inherit an already-started result.  Completed values are never cached.

Entry points
------------
``BurstReadCoalescer.run`` returns the owned or shared read value.
``BurstReadCoalescer.snapshot`` exposes bounded resource counters.

Dependencies
------------
The caller owns key construction, result copying, resource-budget selection,
and storage access.  This coordinator owns no executor or background thread.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar


_MAX_ACTIVE_GATHERS = 256
_MAX_GATHER_SECONDS = 0.100
Key = TypeVar("Key", bound=Hashable)
Value = TypeVar("Value")


@dataclass(slots=True)
class _Gather(Generic[Value]):
    result: concurrent.futures.Future[Value]
    owner_thread_id: int


class BurstReadCoalescer(Generic[Key, Value]):
    """Share only pre-read arrivals and reclaim every flight deterministically."""

    def __init__(
        self,
        *,
        max_active_gathers: int,
        gather_seconds: float,
        wait_for_arrivals: Callable[[float], None] = time.sleep,
        observe_bypass: Callable[[int], None] | None = None,
    ) -> None:
        if (
            isinstance(max_active_gathers, bool)
            or not isinstance(max_active_gathers, int)
            or not 1 <= max_active_gathers <= _MAX_ACTIVE_GATHERS
        ):
            raise ValueError(
                f"max_active_gathers must be between 1 and {_MAX_ACTIVE_GATHERS}"
            )
        if not 0 <= float(gather_seconds) <= _MAX_GATHER_SECONDS:
            raise ValueError(
                f"gather_seconds must be between 0 and {_MAX_GATHER_SECONDS}"
            )
        self._max_active_gathers = max_active_gathers
        self._gather_seconds = float(gather_seconds)
        self._wait_for_arrivals = wait_for_arrivals
        self._observe_bypass = observe_bypass
        self._lock = threading.Lock()
        self._gathers: dict[Key, _Gather[Value]] = {}
        self._joined = 0
        self._bypassed = 0
        self._backing_reads = 0
        self._peak_active = 0

    def _claim(self, key: Key) -> tuple[_Gather[Value] | None, bool, int]:
        thread_id = threading.get_ident()
        with self._lock:
            existing = self._gathers.get(key)
            if existing is not None and existing.owner_thread_id != thread_id:
                self._joined += 1
                return existing, False, 0
            if existing is not None or len(self._gathers) >= self._max_active_gathers:
                self._bypassed += 1
                return None, False, self._bypassed
            gather = _Gather(concurrent.futures.Future(), thread_id)
            self._gathers[key] = gather
            self._peak_active = max(self._peak_active, len(self._gathers))
            return gather, True, 0

    def _close_gather(self, key: Key, gather: _Gather[Value]) -> None:
        with self._lock:
            if self._gathers.get(key) is gather:
                self._gathers.pop(key, None)

    def _load(self, load: Callable[[], Value]) -> Value:
        with self._lock:
            self._backing_reads += 1
        return load()

    def run(self, key: Key, load: Callable[[], Value]) -> Value:
        """Run one owned or shared read without retaining its completed value."""
        gather, leader, bypass_count = self._claim(key)
        if gather is None:
            if self._observe_bypass is not None:
                self._observe_bypass(bypass_count)
            return self._load(load)
        if not leader:
            return gather.result.result()

        try:
            self._wait_for_arrivals(self._gather_seconds)
        except BaseException as error:
            self._close_gather(key, gather)
            gather.result.set_exception(error)
            raise

        # Freshness boundary: calls arriving during execution start a new read.
        self._close_gather(key, gather)
        try:
            value = self._load(load)
        except BaseException as error:
            gather.result.set_exception(error)
            raise
        gather.result.set_result(value)
        return value

    def snapshot(self) -> dict[str, int]:
        """Return coordination counters without exposing live flight objects."""
        with self._lock:
            return {
                "capacity": self._max_active_gathers,
                "gatherMilliseconds": round(self._gather_seconds * 1000),
                "active": len(self._gathers),
                "peakActive": self._peak_active,
                "joined": self._joined,
                "bypassed": self._bypassed,
                "backingReads": self._backing_reads,
            }


__all__ = ["BurstReadCoalescer"]
