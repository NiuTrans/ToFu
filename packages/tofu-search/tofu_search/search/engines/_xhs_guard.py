"""Legacy Xiaohongshu transport pacing, result cache and empty backoff.

RiskGuard owns only compatibility browser and cookie-replay state. Native
SiteSearchProvider operations own their own authorization and pacing; their
results never read or update this state. No browser or host imports live here.
"""
from __future__ import annotations

import random
import threading
import time

from tofu_search.log import get_logger

logger = get_logger(__name__)

# Consecutive empty scrapes that trip the risk-control backoff.
_BACKOFF_AFTER_EMPTY = 3
# A pacing wait longer than this would push the engine past the orchestrator's
# per-engine timeout once the page load itself is added — skip instead.
_MAX_THROTTLE_WAIT_S = 6.0
# Same-keyword cache ceiling (FIFO eviction; entries are tiny).
_CACHE_MAX_ENTRIES = 64


class RiskGuard:
    """One captured transport's pacing, query cache, and backoff state.

    The captured legacy provider/source owns this guard; native providers never
    borrow it. The lock paces admission to page loads. In-flight page execution
    and provider callbacks run outside it. Only replay outcomes cache content.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._consecutive_empty = 0
        self._cooldown_until = 0.0
        self._cache: dict = {}  # (normalized query, result count) -> (timestamp, rows)

    def in_cooldown(self) -> bool:
        with self._lock:
            return time.time() < self._cooldown_until

    def cached_results(self, query: str, ttl_s: int, *, max_results: int):
        """Fresh cached results for ``query``, or None. Defensive copies."""
        if ttl_s <= 0:
            return None
        key = (query.strip().lower(), max_results)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            ts, results = hit
            if now - ts > ttl_s:
                self._cache.pop(key, None)
                return None
            return [dict(r) for r in results]

    def wait_turn(self, min_interval_s: float) -> bool:
        """Block until this thread may fire the next page load.

        Returns False when the required wait would exceed
        ``_MAX_THROTTLE_WAIT_S`` — the caller then skips this round rather
        than blowing the engine's latency budget.
        """
        if min_interval_s <= 0:
            return True
        with self._lock:
            now = time.time()
            wait = (self._last_request_ts + min_interval_s
                    + random.uniform(0.0, min(3.0, min_interval_s))) - now
            if wait > _MAX_THROTTLE_WAIT_S:
                return False
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.time()
            return True

    def report_outcome(self, query: str, results: list, *, max_results: int, ttl_s: int,
                       cooldown_s: int) -> None:
        """Fold one real page load's outcome into cache + backoff state."""
        key = (query.strip().lower(), max_results)
        with self._lock:
            if results:
                self._consecutive_empty = 0
                if ttl_s > 0:
                    self._cache[key] = (time.time(), [dict(row) for row in results])
                    while len(self._cache) > _CACHE_MAX_ENTRIES:
                        self._cache.pop(next(iter(self._cache)))
                return
            self._consecutive_empty += 1
            if self._consecutive_empty >= _BACKOFF_AFTER_EMPTY and cooldown_s > 0:
                self._cooldown_until = time.time() + cooldown_s
                self._consecutive_empty = 0
                logger.warning(
                    '[Search] XHS: %d consecutive empty scrapes — entering %ds '
                    'cooldown. A risk-control wall (安全验证/滑块) or an expired '
                    'cookie scrapes as zero note cards; continuing to hit a '
                    'flagged account escalates the flag. Re-check the account '
                    'in Settings → 需要登录的来源.',
                    _BACKOFF_AFTER_EMPTY, cooldown_s)
