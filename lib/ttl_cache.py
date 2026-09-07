"""Generic in-memory TTL cache.

Unifies the simple TTL-cache pattern that appears throughout the
codebase (get-with-expiry, set-with-timestamp, eviction). Provides:

  - ``TTLCache(ttl, max_size=None)`` — base class, dict-like API
  - ``cache.get(key, default=None)`` — returns default if expired/missing
  - ``cache.set(key, value)`` — record with current timestamp
  - ``cache.get_or_compute(key, fn)`` — fetch on miss; serialised per key
  - ``cache.invalidate(key)`` — explicit drop
  - ``cache.clear()`` — drop everything
  - ``cache.cleanup_stale()`` — eagerly purge expired entries.
    TTL expiry is otherwise lazy (entries are evicted on the next ``get()``
    or ``has()`` call). Size-based LRU eviction DOES run automatically on
    every ``set()`` when ``max_size`` is configured.
  - ``cache.stats()`` — returns hit/miss/evict counters

When NOT to use this
--------------------
``token_counter.usage_cache`` is intentionally kept separate — it has
rich domain-specific semantics (structured ``_UsageEntry`` dataclass,
signature-based staleness, model-family tokenizer validation) that
wouldn't benefit from a generic wrapper.

For anything that just needs "remember a value for N seconds, recompute
on miss", use ``TTLCache``. The trading caches
(``nav._nav_cache``, ``market._market_cache``, ``screening._screen_cache``)
and ``database._sql_translate._translate_sql_cache`` and
``feishu.events._processed_msgs`` all use this class.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional

import weakref

from lib.log import get_logger
from lib.weak_lock_pool import WeakLockPool

logger = get_logger(__name__)

# Registry of all live TTLCache instances so a memory-pressure relief pass
# (lib.cgroup_guard) can drop our own reclaimable caches. WeakSet so a cache
# that goes out of scope is auto-removed and never keeps itself alive.
_ALL_CACHES: 'weakref.WeakSet[TTLCache]' = weakref.WeakSet()
_ALL_CACHES_LOCK = threading.Lock()


def clear_all_caches() -> int:
    """Drop every entry from every live TTLCache. Returns total entries removed.

    Used by the cgroup pressure monitor to shrink our RSS under memory
    pressure. Best-effort and thread-safe; a per-cache clear() failure is
    logged and skipped, never fatal.
    """
    with _ALL_CACHES_LOCK:
        caches = list(_ALL_CACHES)
    total = 0
    for c in caches:
        try:
            total += c.clear()
        except Exception as e:
            logger.warning('[ttl_cache] clear_all_caches: %s failed: %s',
                           getattr(c, 'name', '?'), e)
    return total


class TTLCache:
    """Thread-safe TTL cache with optional size-bounded LRU eviction.

    Parameters
    ----------
    ttl : float
        Per-entry time-to-live in seconds. Entries older than ``ttl``
        are returned as misses (and lazily evicted on access). Use
        ``ttl=0`` or a negative value to disable expiry — entries live
        forever (or until ``max_size`` evicts them).
    max_size : int | None
        If set, the cache evicts the least-recently-used entries when
        ``set()`` would push it past the limit. ``None`` disables size
        bounding (only TTL governs eviction).
    name : str
        Logical name for logging / stats. Defaults to ``'ttl_cache'``.
    """

    def __init__(self, ttl: float, *, max_size: Optional[int] = None,
                 name: str = 'ttl_cache') -> None:
        self.ttl = ttl
        self.max_size = max_size
        self.name = name
        # OrderedDict preserves insertion order — used for LRU eviction.
        # Each value is (timestamp, payload).
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        # Active holders/waiters retain their per-key lock; historical keys
        # do not. A strong dict needed delicate eviction cleanup and could
        # permanently retain a lock when eviction crossed an active holder.
        self._key_locks = WeakLockPool(threading.Lock)
        # Stats
        self._hits = 0
        self._misses = 0
        self._expired_evicts = 0
        self._size_evicts = 0
        with _ALL_CACHES_LOCK:
            _ALL_CACHES.add(self)

    # ── Internal helpers ──────────────────────────────────────────

    def _is_expired(self, ts: float, now: float) -> bool:
        if self.ttl is None or self.ttl <= 0:
            return False
        return (now - ts) > self.ttl

    def _evict_if_oversized(self) -> None:
        """Caller must hold self._lock."""
        if self.max_size is None:
            return
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)  # LRU drop
            self._size_evicts += 1

    def _key_lock(self, key: Hashable) -> threading.Lock:
        """Return the live lock serialising one cache-key computation."""
        return self._key_locks.lock_for(key)

    # ── Public API ────────────────────────────────────────────────

    def get(self, key: Hashable, default: Any = None) -> Any:
        """Return the cached value for ``key``, or ``default`` if missing/expired."""
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return default
            ts, payload = entry
            if self._is_expired(ts, now):
                # Lazy eviction
                self._data.pop(key, None)
                self._expired_evicts += 1
                self._misses += 1
            else:
                # Touch for LRU
                self._data.move_to_end(key)
                self._hits += 1
                return payload
        return default

    def set(self, key: Hashable, value: Any) -> None:
        """Store ``value`` under ``key`` with the current timestamp."""
        now = time.time()
        with self._lock:
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            self._evict_if_oversized()

    def has(self, key: Hashable) -> bool:
        """True if ``key`` is present AND not expired. Lazily evicts expired."""
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            if self._is_expired(entry[0], now):
                self._data.pop(key, None)
                self._expired_evicts += 1
            else:
                return True
        return False

    def invalidate(self, key: Hashable) -> bool:
        """Drop the entry for ``key``. Returns True if it existed."""
        with self._lock:
            existed = self._data.pop(key, None) is not None
        return existed

    def clear(self) -> int:
        """Drop all entries. Returns the count removed."""
        with self._lock:
            n = len(self._data)
            self._data.clear()
        return n

    def cleanup_stale(self) -> int:
        """Eagerly evict all expired entries. Returns the count removed."""
        if self.ttl is None or self.ttl <= 0:
            return 0
        now = time.time()
        stale_keys: list = []
        with self._lock:
            stale_keys = [k for k, (ts, _) in self._data.items()
                           if self._is_expired(ts, now)]
            for k in stale_keys:
                self._data.pop(k, None)
                self._expired_evicts += 1
        return len(stale_keys)

    def get_or_compute(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        """Fetch ``key`` or call ``fn()`` to compute + cache it.

        Per-key serialisation: if two threads miss on the same key,
        only one calls ``fn()`` — the second waits and then sees the
        cached value the first inserted. ``fn`` MUST NOT itself call
        ``get_or_compute`` for the same key (would deadlock).

        If ``fn`` raises, the exception propagates; nothing is cached.
        """
        # Fast path: cache hit
        sentinel = object()
        cached = self.get(key, default=sentinel)
        if cached is not sentinel:
            return cached

        # Slow path: serialise per-key so concurrent missers don't
        # all call fn() in parallel
        klock = self._key_lock(key)
        with klock:
            # Re-check after acquiring the lock
            cached = self.get(key, default=sentinel)
            if cached is not sentinel:
                return cached
            value = fn()
            self.set(key, value)
            return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: Hashable) -> bool:
        return self.has(key)

    def stats(self) -> dict:
        """Return a snapshot of cache stats for monitoring."""
        with self._lock:
            return {
                'name': self.name,
                'size': len(self._data),
                'max_size': self.max_size,
                'ttl': self.ttl,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': (self._hits / max(self._hits + self._misses, 1)),
                'expired_evicts': self._expired_evicts,
                'size_evicts': self._size_evicts,
            }


__all__ = ['TTLCache', 'clear_all_caches']
