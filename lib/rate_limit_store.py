"""lib/rate_limit_store.py — Pluggable rate-limit counter store (PR3c / C7).

Two backends behind a single ``record_and_check`` entry point:

  * ``MemoryRateLimitStore`` (default, ``TOFU_RATE_LIMIT_BACKEND=memory``):
    in-process dict keyed by (endpoint, ip).  Identical to the legacy
    inline implementation in ``lib/rate_limiter.py`` — works for the
    ``flask --threaded`` single-process deployment that ships today.

  * ``DatabaseRateLimitStore`` (``TOFU_RATE_LIMIT_BACKEND=db``):
    one atomic ``rate_limit.record_and_check`` semantic Sidecar command.
    It survives restarts and is consistent across application workers while
    keeping drivers, SQL, locks and the transaction inside the Sidecar.

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

import os
import threading
import time
import uuid
from collections import defaultdict
from typing import Tuple

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
    """Sidecar-backed counter via one atomic semantic command.

    The Sidecar serializes each bucket, admits and records in one transaction,
    and opportunistically deletes rows older than ``per_seconds * 2``.
    """

    def __init__(self, client_provider=None):
        if client_provider is None:
            from lib.storage import get_storage_client
            client_provider = lambda: get_storage_client(write=True)
        self._client_provider = client_provider
        self._db_available = True

    def record_and_check(self, endpoint: str, ip: str,
                         limit: int, per_seconds: int) -> Tuple[bool, int]:
        if not self._db_available:
            # Permanently degraded — fail-open silently after first WARN.
            return True, 0

        try:
            command_id = uuid.uuid4().hex
            result = self._client_provider().command(
                'rate_limit.record_and_check',
                {
                    'endpoint': endpoint,
                    'client_key': ip,
                    'event_id': command_id,
                    'limit': limit,
                    'per_seconds': per_seconds,
                },
                command_id,
                deadline=2.0,
            )
            return bool(result['allowed']), int(result['count'])
        except Exception as exc:
            from lib.storage.errors import StorageError
            if isinstance(exc, StorageError) and exc.code in {
                    'database_integrity', 'database_protocol_error'}:
                self._db_available = False
                logger.warning(
                    '[RateLimitStore] storage contract unavailable code=%s; '
                    'failing open until restart', exc.code)
            else:
                logger.warning(
                    '[RateLimitStore] storage request failed type=%s; '
                    'failing open this request', type(exc).__name__)
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
    desired = (os.environ.get('TOFU_RATE_LIMIT_BACKEND')
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
