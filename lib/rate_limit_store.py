"""lib/rate_limit_store.py — Pluggable rate-limit counter store (PR3c / C7).

Two backends behind a single ``record_and_check`` entry point:

  * ``MemoryRateLimitStore`` (default, ``TOFU_RATE_LIMIT_BACKEND=memory``):
    in-process dict keyed by (endpoint, ip).  Identical to the legacy
    inline implementation in ``lib/rate_limiter.py`` — works for the
    ``flask --threaded`` single-process deployment that ships today.

  * ``DatabaseRateLimitStore`` (``TOFU_RATE_LIMIT_BACKEND=db``):
    INSERT-then-COUNT against the ``rate_limit_events`` table.  Survives
    restarts and works correctly across multi-worker WSGI (gunicorn,
    uWSGI).  Schema lives in ``lib/database/_schema_{pg,sqlite}.py``.

Failure mode is **fail-open**: if the DB write or count fails for any
reason, the request is allowed through with a WARNING log.  A rate
limiter must never take down the whole server.

The decorator in ``lib/rate_limiter.py`` calls ``get_store()`` once per
request — backend selection reads ``TOFU_RATE_LIMIT_BACKEND`` lazily,
matching the rest of the project's hot-reload conventions
(``import lib as _lib; _lib.X``).

See also:
  * ``docs/RATE_LIMITING_DOS_AUDIT_REPORT.md`` §Rec 1 — this closes the
    audit's "shared in-memory state" finding.
  * ``CLAUDE.md`` §10.3 — schema changes mirrored across both backends.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Tuple

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Memory backend (default)
# ═══════════════════════════════════════════════════════════════════════


class MemoryRateLimitStore:
    """In-process counter, identical semantics to the legacy implementation."""

    _CLEANUP_INTERVAL = 300  # purge stale entries every 5 minutes

    def __init__(self):
        # endpoint -> ip -> [timestamp, ...]
        self._counts: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list))
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def record_and_check(self, endpoint: str, ip: str,
                         limit: int, per_seconds: int) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            # Periodic full cleanup so the dict doesn't grow unbounded.
            if now - self._last_cleanup > self._CLEANUP_INTERVAL:
                self._last_cleanup = now
                for ep in list(self._counts.keys()):
                    for addr in list(self._counts[ep].keys()):
                        self._counts[ep][addr] = [
                            ts for ts in self._counts[ep][addr]
                            if now - ts < per_seconds
                        ]
                        if not self._counts[ep][addr]:
                            del self._counts[ep][addr]
                    if not self._counts[ep]:
                        del self._counts[ep]

            # Trim this bucket and check.
            self._counts[endpoint][ip] = [
                ts for ts in self._counts[endpoint][ip]
                if now - ts < per_seconds
            ]
            current = len(self._counts[endpoint][ip])
            if current >= limit:
                return False, current
            self._counts[endpoint][ip].append(now)
            return True, current + 1


# ═══════════════════════════════════════════════════════════════════════
#  Database backend
# ═══════════════════════════════════════════════════════════════════════


class DatabaseRateLimitStore:
    """SQLite/PG-backed counter via ``rate_limit_events`` table.

    Each gated request inserts one row; the count is a SELECT against
    the sliding window.  Opportunistic cleanup deletes rows older than
    ``per_seconds * 2`` for the same (endpoint, ip) — by piggybacking on
    the existing transaction we avoid a daemon thread.
    """

    def __init__(self):
        # Lazy-import lib.database so a test environment without a DB
        # can still construct this object and degrade gracefully on
        # first call.
        self._db_available = True

    def record_and_check(self, endpoint: str, ip: str,
                         limit: int, per_seconds: int) -> Tuple[bool, int]:
        if not self._db_available:
            # Permanently degraded — fail-open silently after first WARN.
            return True, 0

        now_ms = int(time.time() * 1000)
        window_start_ms = now_ms - (per_seconds * 1000)

        try:
            from lib.database import (
                DOMAIN_SYSTEM,
                db_execute_with_retry,
                get_thread_db,
            )
            db = get_thread_db(domain=DOMAIN_SYSTEM)
        except Exception as e:
            # DB unavailable (init failed, fixture not provisioned, etc.) —
            # log once and degrade to fail-open.
            self._db_available = False
            logger.warning(
                '[RateLimitStore] DB backend unavailable (%s) — falling '
                'back to fail-open; subsequent calls will not enforce '
                'limits until restart', e)
            return True, 0

        try:
            # Count existing events in the window for this (endpoint, ip).
            row = db.execute(
                'SELECT COUNT(*) FROM rate_limit_events '
                'WHERE endpoint = ? AND ip = ? AND ts_ms >= ?',
                (endpoint, ip, window_start_ms),
            ).fetchone()
            current = int(row[0]) if row else 0
            if current >= limit:
                # Don't INSERT a row when rejecting — keeps the table from
                # growing under attack.  The trade-off: the rejection
                # itself doesn't reset the window like a successful call
                # would.  That matches the in-memory behaviour.
                return False, current

            # Record this request.  Use db_execute_with_retry so SQLite
            # 'database is locked' under contention auto-retries.
            db_execute_with_retry(
                db,
                'INSERT INTO rate_limit_events (endpoint, ip, ts_ms) '
                'VALUES (?, ?, ?)',
                (endpoint, ip, now_ms),
                commit=True,
            )

            # Opportunistic cleanup — delete rows for THIS (endpoint, ip)
            # that fell out of the window twice over.  Keeps the table
            # bounded without a daemon thread.  Errors here are non-fatal.
            stale_cutoff_ms = now_ms - (per_seconds * 2 * 1000)
            try:
                db_execute_with_retry(
                    db,
                    'DELETE FROM rate_limit_events '
                    'WHERE endpoint = ? AND ip = ? AND ts_ms < ?',
                    (endpoint, ip, stale_cutoff_ms),
                    commit=True,
                )
            except Exception as ce:
                logger.debug(
                    '[RateLimitStore] cleanup non-fatal failure '
                    'endpoint=%s ip=%s: %s', endpoint, ip, ce)

            return True, current + 1

        except Exception as e:
            # Any other DB failure → fail-open with a WARN so the user
            # still gets service.  We re-mark unavailable only if it
            # looks like a structural failure (missing table, etc.) so a
            # transient glitch doesn't permanently disable enforcement.
            err_str = str(e)
            if 'no such table' in err_str.lower() or 'does not exist' in err_str.lower():
                self._db_available = False
                logger.warning(
                    '[RateLimitStore] rate_limit_events table missing '
                    '(%s) — degrading to fail-open until restart', err_str)
            else:
                logger.warning(
                    '[RateLimitStore] DB error during enforcement '
                    '(endpoint=%s ip=%s): %s — failing open this request',
                    endpoint, ip, err_str)
            return True, 0


# ═══════════════════════════════════════════════════════════════════════
#  Backend selection — read at call time so TOFU_RATE_LIMIT_BACKEND
#  is honored even if set after process boot (mirrors the project's
#  other env-var-driven knobs).
# ═══════════════════════════════════════════════════════════════════════


_store_lock = threading.Lock()
_store: object = None
_store_backend: str = ''


def get_store():
    """Return the active rate-limit store, building it lazily on first use.

    Backend is chosen from ``TOFU_RATE_LIMIT_BACKEND``:

      * ``memory`` (default) → ``MemoryRateLimitStore``
      * ``db`` → ``DatabaseRateLimitStore``

    An unrecognised value logs a WARN and falls back to memory.
    """
    global _store, _store_backend
    desired = (getenv_compat('TOFU_RATE_LIMIT_BACKEND')
               or 'memory').strip().lower()
    if desired not in ('memory', 'db'):
        logger.warning(
            '[RateLimitStore] Unknown backend %r — defaulting to memory',
            desired)
        desired = 'memory'

    with _store_lock:
        if _store is not None and _store_backend == desired:
            return _store
        if desired == 'db':
            _store = DatabaseRateLimitStore()
        else:
            _store = MemoryRateLimitStore()
        _store_backend = desired
        logger.info('[RateLimitStore] backend=%s active', desired)
    return _store


def reset_for_test():
    """Force the next ``get_store()`` call to rebuild — test-only helper.

    Pytest fixtures use this to ensure each test starts with a clean
    counter when monkeypatching ``TOFU_RATE_LIMIT_BACKEND``.
    """
    global _store, _store_backend
    with _store_lock:
        _store = None
        _store_backend = ''
