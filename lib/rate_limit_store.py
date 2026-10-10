"""lib/rate_limit_store.py — Pluggable rate-limit counter store (PR3c / C7).

Two backends behind a single ``record_and_check`` entry point:

  * ``MemoryRateLimitStore`` (default, ``TOFU_RATE_LIMIT_BACKEND=memory``):
    process-local exact sliding windows keyed by (endpoint, client). Resident
    buckets and timestamps are launch-probed, hard-bounded LRU working sets.

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

See ``docs/modules/auth_providers_billing.md`` for the owning security and
operations contract.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Tuple

from lib.log import get_logger
from lib.rate_limit_policy import (
    RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY,
    RATE_LIMIT_MEMORY_EVENT_HARD_CAPACITY,
    rate_limit_memory_bucket_capacity,
    rate_limit_memory_event_capacity,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Memory backend (default)
# ═══════════════════════════════════════════════════════════════════════


_MAX_ENDPOINT_KEY_CHARS = 256
_MAX_CLIENT_KEY_CHARS = 512


def _bounded_identity_part(value: object, *, maximum_chars: int) -> str:
    """Retain a short identity verbatim or a collision-resistant digest."""
    text = str(value)
    if len(text) <= maximum_chars:
        return f'r{len(text)}:{text}'
    digest = hashlib.sha256(
        text.encode('utf-8', errors='surrogatepass')).hexdigest()
    return f'h:{digest}'


@dataclass(slots=True)
class _MemoryBucket:
    events: deque[float]
    window_seconds: int


class MemoryRateLimitStore:
    """Finite process-local exact sliding-window counters.

    Capacity pressure evicts the least-recently-used bucket instead of making
    an attacker-created identity set deny every new peer. This preserves the
    store's documented fail-open posture; distributed enforcement uses the
    Sidecar backend. A separate total-event ceiling can only make one unusually
    large hot bucket stricter, never allocate past the resident budget.
    """

    _CLEANUP_INTERVAL = 300  # purge stale entries every 5 minutes

    def __init__(
        self,
        *,
        bucket_capacity: int | None = None,
        event_capacity: int | None = None,
        clock: Callable[[], float] | None = None,
    ):
        resolved_bucket_capacity = (
            rate_limit_memory_bucket_capacity()
            if bucket_capacity is None else int(bucket_capacity)
        )
        if not 1 <= resolved_bucket_capacity \
                <= RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY:
            raise ValueError('bucket_capacity is outside the hard bounds')
        resolved_event_capacity = (
            rate_limit_memory_event_capacity(resolved_bucket_capacity)
            if event_capacity is None else int(event_capacity)
        )
        if not 1 <= resolved_event_capacity \
                <= RATE_LIMIT_MEMORY_EVENT_HARD_CAPACITY:
            raise ValueError('event_capacity is outside the hard bounds')
        self._bucket_capacity = resolved_bucket_capacity
        self._event_capacity = resolved_event_capacity
        self._clock = clock or time.monotonic
        self._buckets: OrderedDict[
            tuple[str, str], _MemoryBucket] = OrderedDict()
        self._event_count = 0
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        self._expired_bucket_evictions = 0
        self._bucket_capacity_evictions = 0
        self._event_capacity_evictions = 0
        self._event_capacity_rejections = 0

    @staticmethod
    def _key(endpoint: str, client_key: str) -> tuple[str, str]:
        return (
            _bounded_identity_part(
                endpoint, maximum_chars=_MAX_ENDPOINT_KEY_CHARS),
            _bounded_identity_part(
                client_key, maximum_chars=_MAX_CLIENT_KEY_CHARS),
        )

    def _trim_bucket(
        self,
        bucket: _MemoryBucket,
        *,
        now: float,
        window_seconds: int,
    ) -> None:
        cutoff = now - window_seconds
        removed = 0
        while bucket.events and bucket.events[0] <= cutoff:
            bucket.events.popleft()
            removed += 1
        self._event_count -= removed
        bucket.window_seconds = window_seconds

    def _remove_bucket(
        self,
        key: tuple[str, str],
        *,
        reason: str,
    ) -> None:
        bucket = self._buckets.pop(key)
        self._event_count -= len(bucket.events)
        if reason == 'expired':
            self._expired_bucket_evictions += 1
        elif reason == 'bucket_capacity':
            self._bucket_capacity_evictions += 1
        elif reason == 'event_capacity':
            self._event_capacity_evictions += 1

    def _cleanup_stale(self, now: float) -> None:
        self._last_cleanup = now
        for key, bucket in list(self._buckets.items()):
            self._trim_bucket(
                bucket,
                now=now,
                window_seconds=bucket.window_seconds,
            )
            if not bucket.events:
                self._remove_bucket(key, reason='expired')

    def _admit_bucket(self, key: tuple[str, str],
                      window_seconds: int) -> _MemoryBucket:
        while len(self._buckets) >= self._bucket_capacity:
            oldest_key = next(iter(self._buckets))
            self._remove_bucket(oldest_key, reason='bucket_capacity')
        bucket = _MemoryBucket(deque(), window_seconds)
        self._buckets[key] = bucket
        return bucket

    def _make_event_room(self, protected_key: tuple[str, str]) -> bool:
        while self._event_count >= self._event_capacity \
                and len(self._buckets) > 1:
            oldest_key = next(iter(self._buckets))
            if oldest_key == protected_key:
                self._buckets.move_to_end(oldest_key)
                continue
            self._remove_bucket(oldest_key, reason='event_capacity')
        return self._event_count < self._event_capacity

    def record_and_check(self, endpoint: str, ip: str,
                         limit: int, per_seconds: int) -> Tuple[bool, int]:
        if int(limit) <= 0:
            raise ValueError('limit must be positive')
        if int(per_seconds) <= 0:
            raise ValueError('per_seconds must be positive')
        limit = int(limit)
        per_seconds = int(per_seconds)
        now = self._clock()
        key = self._key(endpoint, ip)
        with self._lock:
            if now - self._last_cleanup > self._CLEANUP_INTERVAL:
                self._cleanup_stale(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._admit_bucket(key, per_seconds)
            else:
                self._buckets.move_to_end(key)
            self._trim_bucket(
                bucket, now=now, window_seconds=per_seconds)
            current = len(bucket.events)
            if current >= limit:
                return False, current
            if not self._make_event_room(key):
                self._event_capacity_rejections += 1
                return False, current
            bucket.events.append(now)
            self._event_count += 1
            return True, current + 1

    def stats(self) -> dict[str, int | str]:
        """Return a payload-free resource snapshot for tests and metrics."""
        with self._lock:
            return {
                'backend': 'memory',
                'buckets': len(self._buckets),
                'bucket_capacity': self._bucket_capacity,
                'events': self._event_count,
                'event_capacity': self._event_capacity,
                'expired_bucket_evictions': self._expired_bucket_evictions,
                'bucket_capacity_evictions': self._bucket_capacity_evictions,
                'event_capacity_evictions': self._event_capacity_evictions,
                'event_capacity_rejections': self._event_capacity_rejections,
            }


# ═══════════════════════════════════════════════════════════════════════
#  Database backend
# ═══════════════════════════════════════════════════════════════════════


class DatabaseRateLimitStore:
    """Sidecar-backed counter via one atomic semantic command.

    The Sidecar serializes each bucket, admits and records in one transaction,
    stamps every event with its exact window expiry, and removes a bounded
    global expiry batch on every check through an age-leading index.
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
#  Token-bucket backend (API quotas) — Redis, cross-replica authoritative
# ═══════════════════════════════════════════════════════════════════════


_TOKEN_BUCKET_KEY_TTL = 7 * 86400  # idle bucket backstop (tpd refills over 1d)


class RedisTokenBucketStore:
    """Redis-backed token bucket for per-principal API quotas.

    One HASH per bucket (fields ``cap``/``rate``/``tok``/``ts``) mutated under
    WATCH/MULTI/EXEC — refill is computed client-side and check-and-decrement
    is one optimistic transaction, so concurrent replicas can never both
    consume the same token. NO Lua: works on managed/cluster Redis and on
    fakeredis. ``redis`` is imported lazily and this class is only constructed
    under the redis selection in :func:`get_token_bucket_store` — the personal
    in-process path never imports it.

    Failure policy mirrors ``RedisRuntimeStateStore``: personal mode
    (``fail_open=True``) admits on backend error; distributed mode
    (``fail_open=False``) refuses NEW admission so an outage surfaces as a
    throttle instead of silently lifting every quota.
    """

    _KEY_NS = 'tofu:rlb'
    _WATCH_RETRIES = 8
    _RETRY_MIN = 0.5
    _RETRY_MAX = 30.0
    _WARN_INTERVAL = 30.0

    def __init__(self, *, fail_open: bool = True, redis_url: str = ''):
        self._client = None
        self._available = False
        self._lock = threading.Lock()
        self._next_retry_at = 0.0
        self._retry_delay = self._RETRY_MIN
        self._last_warn_at = 0.0
        self._fail_open = bool(fail_open)
        self._redis_url = str(redis_url or '').strip()

    @property
    def fail_open(self) -> bool:
        return self._fail_open

    def _new_client(self):
        import redis  # optional dependency — guarded by the caller
        url = (self._redis_url or os.environ.get('TOFU_REDIS_URL')
               or 'redis://127.0.0.1:6379/0')
        return redis.Redis.from_url(
            url, socket_connect_timeout=1.0, socket_timeout=1.0,
            decode_responses=True)

    def _redis(self):
        with self._lock:
            if self._client is not None and self._available:
                return self._client
            if time.time() < self._next_retry_at:
                return None
            try:
                self._client = self._new_client()
                self._available = True
                self._retry_delay = self._RETRY_MIN
                return self._client
            except Exception as e:
                self._schedule_retry_locked(e, 'connect')
                return None

    def _schedule_retry_locked(self, error, operation: str) -> None:
        now = time.time()
        self._available = False
        self._next_retry_at = now + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, self._RETRY_MAX)
        if now - self._last_warn_at >= self._WARN_INTERVAL:
            self._last_warn_at = now
            logger.warning(
                '[RateLimitStore] redis token-bucket %s failed (%s) — '
                'admission policy=%s; automatic reconnect in %.1fs',
                operation, error, 'open' if self._fail_open else 'closed',
                max(0.0, self._next_retry_at - now))

    def _command_failed(self, error, operation: str) -> None:
        with self._lock:
            old = self._client
            self._client = None
            self._schedule_retry_locked(error, operation)
        if old is not None:
            try:
                pool = getattr(old, 'connection_pool', None)
                if pool is not None:
                    pool.disconnect()
            except Exception as e:
                logger.debug('[RateLimitStore] redis client close failed: %s', e)

    def _k(self, name: str) -> str:
        return f"{self._KEY_NS}:{_bounded_identity_part(name, maximum_chars=_MAX_CLIENT_KEY_CHARS)}"

    _SKIP = object()  # no stored bucket and caller supplied no config

    def _bucket_op(self, name: str, capacity, refill_rate, now: float, apply):
        """One optimistic WATCH/MULTI/EXEC bucket mutation.

        ``capacity``/``refill_rate`` may be ``None`` to keep the stored
        configuration; a missing key with no supplied config returns
        ``_SKIP``. ``apply(tokens_refilled) -> (result, new_tokens)`` runs
        under the transaction. Returns ``(result, tokens_after)``,
        ``_SKIP``, or ``None`` on backend failure (callers apply the
        deployment's open/closed admission policy).
        """
        from redis import WatchError  # optional dep — redis backend only
        r = self._redis()
        if r is None:
            return None
        key = self._k(name)
        try:
            for _attempt in range(self._WATCH_RETRIES):
                with r.pipeline() as pipe:
                    try:
                        pipe.watch(key)
                        data = pipe.hgetall(key)
                        if data:
                            try:
                                tokens = float(data.get('tok', '0') or 0)
                                last = float(data.get('ts', '0') or 0)
                            except (TypeError, ValueError):
                                tokens, last = None, None
                            if capacity is None:
                                try:
                                    cap = float(data.get('cap', '0') or 0)
                                except (TypeError, ValueError):
                                    cap = 0.0
                            else:
                                cap = float(capacity)
                            if refill_rate is None:
                                try:
                                    rate = float(data.get('rate', '0') or 0)
                                except (TypeError, ValueError):
                                    rate = 0.0
                            else:
                                rate = float(refill_rate)
                            if tokens is None:
                                tokens, last = cap, now
                        else:
                            if capacity is None:
                                pipe.unwatch()
                                return self._SKIP
                            cap = float(capacity)
                            rate = float(refill_rate or 0.0)
                            tokens, last = cap, now
                        # Same refill math as the in-process _Bucket.
                        tokens = min(cap, tokens + max(0.0, now - last) * rate)
                        result, tokens = apply(tokens, cap)
                        pipe.multi()
                        pipe.hset(key, mapping={
                            'cap': repr(float(cap)),
                            'rate': repr(float(rate)),
                            'tok': repr(float(tokens)),
                            'ts': repr(float(now)),
                        })
                        pipe.expire(key, _TOKEN_BUCKET_KEY_TTL)
                        pipe.execute()
                        return result, tokens
                    except WatchError:
                        continue
            logger.warning('[RateLimitStore] token-bucket watch retries '
                           'exhausted for %s', name)
            return None
        except Exception as e:
            self._command_failed(e, 'bucket_op')
            return None

    def bucket_try_consume(self, name: str, capacity: float,
                           refill_rate: float, n: float, now: float):
        """Atomically refill and consume ``n`` tokens.

        Returns ``(allowed, tokens_after)`` or ``None`` on backend failure.
        ``n == 0`` is a pure peek (refill only, always allowed).
        """
        def apply(tokens, _cap):
            if tokens >= n:
                return True, tokens - n
            return False, tokens
        return self._bucket_op(name, capacity, refill_rate, now, apply)

    def bucket_refund(self, name: str, capacity: float, refill_rate: float,
                      n: float, now: float):
        """Return ``n`` tokens, clamped to capacity (mirrors the in-process
        RPM refund on a TPD denial). Returns tokens_after or None."""
        def apply(tokens, cap):
            return None, min(cap, tokens + n)
        out = self._bucket_op(name, capacity, refill_rate, now, apply)
        if out in (None, self._SKIP):
            return None
        return out[1]

    def bucket_consume_force(self, name: str, n: float, now: float, *,
                             capacity=None, refill_rate=None):
        """Consume even below zero (post-hoc actual usage).

        ``capacity=None`` keeps the stored configuration; a missing key with
        no supplied config is a no-op (mirrors the in-process path that
        returns early when nothing was ever recorded). Returns tokens_after
        or None on backend failure.
        """
        def apply(tokens, _cap):
            return None, tokens - n
        out = self._bucket_op(name, capacity, refill_rate, now, apply)
        if out in (None, self._SKIP):
            return None
        return out[1]


# ═══════════════════════════════════════════════════════════════════════
#  Backend selection — read at call time so TOFU_RATE_LIMIT_BACKEND
#  is honored even if set after process boot (mirrors the project's
#  other env-var-driven knobs).
# ═══════════════════════════════════════════════════════════════════════


_store_lock = threading.Lock()
_store: object = None
_store_backend: str = ''


def _deployment_mode() -> str:
    """The launch deployment mode ('personal'|'distributed'), best-effort."""
    try:
        from runtime_guards import load_deployment_configuration
        deployment = load_deployment_configuration(
            allow_test_backend_override=(
                os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1'))
        return deployment.mode
    except Exception as e:
        logger.debug('[RateLimitStore] deployment config unavailable: %s', e)
        return 'personal'


def get_store():
    """Return the active rate-limit store, building it lazily on first use.

    Backend is chosen from ``TOFU_RATE_LIMIT_BACKEND``:

      * ``memory`` (default in personal mode) → ``MemoryRateLimitStore``
      * ``db`` → ``DatabaseRateLimitStore``

    When the variable is UNSET the default follows the deployment contract
    (mirrors ``lib.runtime_state_store.get_store``): distributed mode
    defaults to ``db`` so event-count caps hold across replicas, personal
    mode to ``memory``. An unrecognised value logs a WARN and falls back to
    memory.
    """
    global _store, _store_backend
    explicit = (os.environ.get('TOFU_RATE_LIMIT_BACKEND')
                or '').strip().lower()
    defaulted = not explicit
    desired = explicit or (
        'db' if _deployment_mode() == 'distributed' else 'memory')
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
        logger.info('[RateLimitStore] backend=%s active%s', desired,
                    ' (deployment-mode default)' if defaulted else '')
    return _store


def rate_limit_store_stats() -> dict[str, int | str | bool]:
    """Return the active backend's payload-free resource snapshot.

    Metrics collection never creates a limiter that no request has used.
    """
    with _store_lock:
        active_store = _store
        active_backend = _store_backend
    if isinstance(active_store, MemoryRateLimitStore):
        return active_store.stats()
    if isinstance(active_store, DatabaseRateLimitStore):
        return {
            'backend': 'db',
            'available': active_store._db_available,
        }
    return {'backend': active_backend or 'uninitialized'}


# ── Token-bucket store selection (API quotas) — same env/mode contract as
# lib.runtime_state_store.get_store: distributed mode is always redis and
# fail-closed for NEW admission; personal mode defaults to the in-process
# path (None → lib.rate_limit_api's module-local buckets, byte-equivalent
# to the historical behavior) with an explicit redis opt-in that fails open.


_token_bucket_lock = threading.Lock()
_token_bucket_store = None
_token_bucket_cache_key: str | None = None  # None = not yet resolved


def get_token_bucket_store():
    """Return the shared API-quota token-bucket store, or None.

    ``None`` means the caller uses its in-process buckets (personal mode,
    zero threads/network/dependencies). Distributed mode
    (``TOFU_DEPLOYMENT_MODE=distributed``) always selects
    :class:`RedisTokenBucketStore` from ``TOFU_REDIS_URL_FILE`` and fails
    NEW admission closed when Redis is unavailable; personal mode honors an
    explicit ``TOFU_API_BUCKET_BACKEND=redis`` opt-in (fail-open).
    """
    global _token_bucket_store, _token_bucket_cache_key
    if _deployment_mode() == 'distributed':
        from runtime_guards import load_deployment_configuration
        deployment = load_deployment_configuration(
            allow_test_backend_override=(
                os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') == '1'))
        desired, fail_open, redis_url = 'redis', False, deployment.redis_url
    else:
        desired = (os.environ.get('TOFU_API_BUCKET_BACKEND')
                   or 'memory').strip().lower()
        fail_open, redis_url = True, ''
    if desired not in ('memory', 'redis'):
        logger.warning('[RateLimitStore] unknown token-bucket backend %r — '
                       'defaulting to memory', desired)
        desired = 'memory'
    cache_key = f'{desired}:{"open" if fail_open else "closed"}'
    with _token_bucket_lock:
        if _token_bucket_cache_key == cache_key:
            return _token_bucket_store
        _token_bucket_store = (
            RedisTokenBucketStore(fail_open=fail_open, redis_url=redis_url)
            if desired == 'redis' else None)
        _token_bucket_cache_key = cache_key
        logger.info(
            '[RateLimitStore] token_bucket backend=%s '
            'admission_failure_policy=%s active',
            desired, 'open' if fail_open else 'closed')
    return _token_bucket_store


def reset_for_test():
    """Force the next ``get_store()`` call to rebuild — test-only helper.

    Pytest fixtures use this to ensure each test starts with a clean
    counter when monkeypatching ``TOFU_RATE_LIMIT_BACKEND``.
    """
    global _store, _store_backend
    global _token_bucket_store, _token_bucket_cache_key
    with _store_lock:
        _store = None
        _store_backend = ''
    with _token_bucket_lock:
        _token_bucket_store = None
        _token_bucket_cache_key = None
