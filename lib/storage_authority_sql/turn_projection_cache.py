"""Bounded reconstructible cache for live Turn projection write baselines.

Responsibility
--------------
Keep the last successfully computed public projection beside one Sidecar
backend so a revision-contiguous executor event need not transfer and decode
the same multi-MiB database value again.  The database revision remains the
authority: a miss, stale revision, eviction, process restart, or rollback
always falls back to the durable row.

Entry points are :class:`TurnProjectionCache`, :class:`CachedTurnProjection`,
and :func:`projection_cache_key`.  This module has no database dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable


ProjectionCacheKey = tuple[str, int, str, str, str]
PROJECTION_CACHE_CHARGE_MULTIPLIER = 3


@dataclass(slots=True)
class CachedTurnProjection:
    """One immutable-by-convention public baseline and its storage evidence."""

    revision: int
    projection: dict[str, Any]
    charge_bytes: int
    text_bytes: int
    stored_payload_bytes: int | None
    stored_matches_projection: bool
    stable_segments: bool
    last_used_at: float


def projection_cache_key(
    backend: str,
    user_id: int,
    conversation_id: str,
    turn_id: str,
    attempt_id: str,
) -> ProjectionCacheKey:
    """Return the explicit authority/owner/attempt cache identity."""
    return (
        str(backend),
        int(user_id),
        str(conversation_id),
        str(turn_id),
        str(attempt_id),
    )


def projection_text_bytes(projection: Mapping[str, Any]) -> int:
    """Return exact UTF-8 bytes retained by the two cumulative text fields."""
    return sum(
        len(str(projection.get(field) or "").encode("utf-8"))
        for field in ("content", "thinking")
    )


def text_update_charge_bytes(
    entry: CachedTurnProjection,
    projection: Mapping[str, Any],
) -> int:
    """Adjust a conservative cache charge without serializing the full Turn."""
    text_bytes = projection_text_bytes(projection)
    return max(
        1,
        entry.charge_bytes
        + PROJECTION_CACHE_CHARGE_MULTIPLIER
        * (text_bytes - entry.text_bytes),
    )


class TurnProjectionCache:
    """Thread-safe LRU bounded by entries, charged bytes, and idle lifetime."""

    def __init__(
        self,
        max_bytes: int,
        *,
        max_entries: int | None = None,
        max_idle_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("projection cache max_bytes must be positive")
        if max_entries is None:
            max_entries = max(4, min(256, max_bytes // (1024 * 1024)))
        if max_entries <= 0:
            raise ValueError("projection cache max_entries must be positive")
        if max_idle_seconds <= 0:
            raise ValueError("projection cache idle lifetime must be positive")
        self.max_bytes = int(max_bytes)
        self.max_entries = int(max_entries)
        self.max_idle_seconds = float(max_idle_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[
            ProjectionCacheKey, CachedTurnProjection
        ] = OrderedDict()
        self._charged_bytes = 0
        self._metrics = {
            "hits": 0,
            "misses": 0,
            "stale_evictions": 0,
            "expired_evictions": 0,
            "capacity_evictions": 0,
            "oversize_rejections": 0,
        }

    def _remove(self, key: ProjectionCacheKey) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._charged_bytes = max(0, self._charged_bytes - entry.charge_bytes)
        return True

    def _prune_expired(self, now: float) -> None:
        cutoff = now - self.max_idle_seconds
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if entry.last_used_at > cutoff:
                return
            self._remove(key)
            self._metrics["expired_evictions"] += 1

    def get(
        self,
        key: ProjectionCacheKey,
        *,
        revision: int,
    ) -> CachedTurnProjection | None:
        """Return an exact-revision baseline or a safe cache miss."""
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            entry = self._entries.get(key)
            if entry is None:
                self._metrics["misses"] += 1
                return None
            if entry.revision != revision:
                self._remove(key)
                self._metrics["stale_evictions"] += 1
                self._metrics["misses"] += 1
                return None
            entry.last_used_at = now
            self._entries.move_to_end(key)
            self._metrics["hits"] += 1
            return entry

    def remember(
        self,
        key: ProjectionCacheKey,
        *,
        revision: int,
        projection: dict[str, Any],
        charge_bytes: int,
        stored_payload_bytes: int | None,
        stored_matches_projection: bool,
        stable_segments: bool,
    ) -> bool:
        """Retain one baseline when it fits both cache budgets."""
        charge = max(1, int(charge_bytes))
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            self._remove(key)
            if charge > self.max_bytes:
                self._metrics["oversize_rejections"] += 1
                return False
            entry = CachedTurnProjection(
                revision=int(revision),
                projection=projection,
                charge_bytes=charge,
                text_bytes=projection_text_bytes(projection),
                stored_payload_bytes=(
                    max(0, int(stored_payload_bytes))
                    if stored_payload_bytes is not None else None
                ),
                stored_matches_projection=bool(stored_matches_projection),
                stable_segments=bool(stable_segments),
                last_used_at=now,
            )
            self._entries[key] = entry
            self._charged_bytes += charge
            while (
                len(self._entries) > self.max_entries
                or self._charged_bytes > self.max_bytes
            ):
                oldest_key = next(iter(self._entries))
                self._remove(oldest_key)
                self._metrics["capacity_evictions"] += 1
            return key in self._entries

    def discard(self, key: ProjectionCacheKey) -> bool:
        """Forget one reconstructible baseline."""
        with self._lock:
            return self._remove(key)

    def clear(self) -> int:
        """Release all reconstructible state and return the entry count."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._charged_bytes = 0
            return count

    def stats(self) -> dict[str, int | float]:
        """Return bounded, content-free cache observability."""
        with self._lock:
            self._prune_expired(self._clock())
            return {
                **self._metrics,
                "entries": len(self._entries),
                "charged_bytes": self._charged_bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "max_idle_seconds": self.max_idle_seconds,
            }


__all__ = [
    "CachedTurnProjection",
    "PROJECTION_CACHE_CHARGE_MULTIPLIER",
    "ProjectionCacheKey",
    "TurnProjectionCache",
    "projection_cache_key",
    "projection_text_bytes",
    "text_update_charge_bytes",
]
