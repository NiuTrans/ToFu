"""lib/runtime_state_store.py — Shared lease / counter / heartbeat primitive.

The foundation of the horizontal scale-out work (board epics
``pt_96b80d88c8d54b71`` Epic C + ``pt_823ff5a3bf004c40`` Epic B). See the
ratified design in ``docs/EPIC_B_PUSH_FANOUT_DESIGN.md`` §0 (Build Order) /
§5 (lease-TTL primitive) and ``docs/EPIC_C_RUNTIME_STATE_DESIGN.md`` §4.2.

**Why this module exists.** Every per-process runtime cap (the admission
ceiling in ``lib/agent_core/admission.py`` and the per-principal SSE semaphore
in ``lib/agent_core/sse_limit.py``) and the push subscription registry
(``lib/agent_core/push.py``) are TODAY in-process — so behind an N-replica
load balancer a ``cap`` silently becomes ``cap × N`` and a crashed replica
holds its slots forever. This module is the single, pluggable lease/counter
substrate they will all re-key onto (steps 2–4 of the Build Order), so the
TTL + heartbeat + fail-open logic is written ONCE with its own tests instead
of three times.

**Two interchangeable backends behind one interface** (mirrors
``lib/rate_limit_store.py`` exactly):

  * ``InProcRuntimeStateStore`` (default, ``TOFU_RUNTIME_STATE_BACKEND=inproc``):
    an in-process dict with wall-clock TTL expiry. ZERO new dependency; a
    single-box install behaves byte-equivalently to today (the cap is the same
    process's authoritative count).
  * ``RedisRuntimeStateStore`` (``TOFU_RUNTIME_STATE_BACKEND=redis``):
    ``SET key val EX ttl`` leases + keyspace ``count`` under a prefix, so the
    cap/registry is authoritative ACROSS replicas (bounded acquire uses SLOT
    KEYS as the single source of truth via SET NX + SCAN + deterministic rank
    — NOT Lua and NOT a standalone counter, so it works on managed/cluster
    Redis and cannot drift) and a crashed replica's leases reclaim by native
    key expiry within one ``ttl`` window. ``redis`` is
    an OPTIONAL dependency — the import is guarded and this backend is only
    constructed under the env flag; the ``inproc`` path never imports it.

**Lease model** (design §5):
  * ``acquire_lease(kind, key, ttl)`` → True if the lease is now held (idempotent
    re-acquire of an already-held key just refreshes it), or the caller may use
    it as a counter-slot claim. Returns False only when a backend error makes
    the outcome unknown AND we choose to fail closed — but the default is
    fail-OPEN (see below), so acquire returns True on backend error.
  * ``acquire_slot(kind, slot_key, limit, ttl, count_prefix)`` → the ATOMIC
    bounded acquire: admit iff the live count under ``count_prefix`` is below
    ``limit``, then lease ``slot_key`` — count-and-insert are one atomic step
    (inproc: under the lock; redis: a Lua script), so concurrent acquires can
    NEVER overshoot ``limit``. Both Epic A caps (global admission ceiling +
    per-principal SSE) re-key onto THIS one primitive. ``limit<=0`` = unbounded.
  * ``refresh_lease(kind, key, ttl)`` → re-arm the TTL (the heartbeat path).
  * ``release_lease(kind, key)`` → eager release (normal, non-crash path).
  * ``count(kind, key_prefix='')`` → number of LIVE (unexpired) leases whose key
    starts with ``key_prefix`` — the per-principal / global-inflight count.
  * ``heartbeat(kind, keys, ttl)`` → bulk refresh a replica's held leases; the
    per-replica loop calls this every ``ttl/3`` so a LIVING task's lease never
    expires (design §5.2) — TTL only bites when heartbeats STOP (crash).

**Fail-open** (design §4, same discipline as ``rate_limit_store``): if the
Redis backend cannot connect / errors, it degrades — ``acquire`` returns True,
``count`` returns 0 — and schedules a jittered exponential reconnect. Calls
skip the known-broken pool only until that backoff expires; recovery replaces
the client and living admission slots are reasserted by their heartbeat. A cap
substrate must never take down the request path; the worst case is that the cap
temporarily stops enforcing (today's behaviour), never a crash or task kill.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Iterable, List, Tuple

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  In-process backend (default) — wall-clock TTL, no dependency
# ═══════════════════════════════════════════════════════════════════════


class InProcRuntimeStateStore:
    """In-process lease store with wall-clock TTL expiry.

    Byte-equivalent to the current single-process posture: the count this
    store reports IS the authoritative count for the one process. A lease is a
    ``(kind, key) → expires_at`` entry; a key is LIVE while ``now < expires_at``.
    """

    _CLEANUP_INTERVAL = 300  # opportunistic full purge every 5 min

    def __init__(self):
        # (kind, key) -> expires_at (monotonic-ish wall clock)
        self._leases: dict[Tuple[str, str], float] = {}
        self._values: dict[Tuple[str, str], Tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def _purge_locked(self, now: float) -> None:
        if now - self._last_cleanup <= self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        dead = [k for k, exp in self._leases.items() if exp <= now]
        for k in dead:
            del self._leases[k]
        dead_v = [k for k, (_v, exp) in self._values.items() if exp <= now]
        for k in dead_v:
            del self._values[k]

    def acquire_lease(self, kind: str, key: str, ttl: float) -> bool:
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            self._leases[(kind, key)] = now + ttl
        return True

    def acquire_slot(self, kind: str, slot_key: str, limit: int, ttl: float,
                     count_prefix: str) -> bool:
        """Atomically admit a slot iff the LIVE count under ``count_prefix`` is
        below ``limit``, then lease ``slot_key``. Returns True on admit.

        The count-and-insert happen under ONE lock hold, so there is no
        check-then-act race: two concurrent acquires can never both observe
        ``count == limit-1`` and both admit. ``limit <= 0`` means unbounded
        (always admit). This is the strict primitive both Epic A caps re-key
        onto — identical semantics across inproc and redis.
        """
        if limit <= 0:
            return self.acquire_lease(kind, slot_key, ttl)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            live = sum(
                1 for (k, key), exp in self._leases.items()
                if k == kind and exp > now and key.startswith(count_prefix)
            )
            # Re-acquiring an already-held slot is a refresh, not a new admit.
            already_held = (self._leases.get((kind, slot_key), 0.0) > now)
            if not already_held and live >= limit:
                return False
            self._leases[(kind, slot_key)] = now + ttl
            return True

    def release_slot(self, kind: str, slot_key: str, count_prefix: str) -> None:
        """Release a slot (mirror of the redis ZSET release). count_prefix is
        accepted for interface parity; inproc keys the slot directly."""
        with self._lock:
            self._leases.pop((kind, slot_key), None)

    def refresh_slots(self, kind: str, slot_keys: Iterable[str], ttl: float,
                      count_prefix: str) -> None:
        """Refresh already-admitted work without applying admission again.

        A living task must keep its crash-reclaim lease for however long the
        task actually runs.  This is deliberately not ``acquire_slot``: after a
        transient store outage, truthful recovery may temporarily restore more
        live work than the configured cap.  Those tasks already exist and must
        remain counted; the cap then refuses *new* work until they drain.
        """
        now = time.time()
        with self._lock:
            for slot_key in slot_keys:
                self._leases[(kind, slot_key)] = now + ttl

    def count_slots(self, kind: str, count_prefix: str) -> int:
        """Live slot count under a prefix — the inproc admission gate. Same
        semantics as the redis ZCOUNT: derived from the live lease set."""
        return self.count(kind, count_prefix)

    def live_slot_members(self, kind: str, count_prefix: str) -> List[str]:
        return self.live_keys(kind, count_prefix)

    def refresh_lease(self, kind: str, key: str, ttl: float) -> bool:
        now = time.time()
        with self._lock:
            # Refresh only if still live (a crashed-then-revived key should
            # re-acquire, not silently refresh a lease that already expired).
            exp = self._leases.get((kind, key))
            if exp is None or exp <= now:
                # Not currently held — re-arm anyway (idempotent heartbeat).
                self._leases[(kind, key)] = now + ttl
                return exp is not None and exp > now
            self._leases[(kind, key)] = now + ttl
            return True

    def release_lease(self, kind: str, key: str) -> None:
        with self._lock:
            self._leases.pop((kind, key), None)

    def count(self, kind: str, key_prefix: str = '') -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for (k, key), exp in self._leases.items()
                if k == kind and exp > now and key.startswith(key_prefix)
            )

    def heartbeat(self, kind: str, keys: Iterable[str], ttl: float) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                self._leases[(kind, key)] = now + ttl

    def set_value(self, kind: str, key: str, value: str, ttl: float) -> None:
        """Store a single string VALUE under (kind, key) with a TTL. Used by
        the supersede index (conv -> latest task_id). Overwrites atomically
        under the lock."""
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            self._values[(kind, key)] = (value, now + ttl)

    def get_value(self, kind: str, key: str):
        """Return the live value for (kind, key), or None if absent/expired."""
        now = time.time()
        with self._lock:
            v = self._values.get((kind, key))
            if v is None or v[1] <= now:
                return None
            return v[0]

    def delete_value(self, kind: str, key: str) -> None:
        """Eagerly drop a value (mirror of the redis DEL). Idempotent."""
        with self._lock:
            self._values.pop((kind, key), None)

    def live_keys(self, kind: str, key_prefix: str = '') -> List[str]:
        """Live keys under a prefix — used by the push subscription registry
        to enumerate which replicas/streams are currently subscribed."""
        now = time.time()
        with self._lock:
            return [key for (k, key), exp in self._leases.items()
                    if k == kind and exp > now and key.startswith(key_prefix)]


# ═══════════════════════════════════════════════════════════════════════
#  Redis backend (opt-in) — native EX leases, cross-replica authoritative
# ═══════════════════════════════════════════════════════════════════════


class RedisRuntimeStateStore:
    """Redis-backed lease store: ``SET … EX ttl`` + keyspace counting.

    Authoritative across replicas; a crashed replica's leases reclaim by
    native key expiry (design §5.3). ``redis`` is imported lazily and this
    class is only constructed under ``TOFU_RUNTIME_STATE_BACKEND=redis`` — the
    ``inproc`` default never touches it, so bare CI without the ``redis``
    package works. Fail-open on any connection/command error.
    """

    _KEY_NS = 'tofu:rts'  # keyspace namespace so counts don't collide

    _RETRY_MIN = 0.5
    _RETRY_MAX = 30.0
    _WARN_INTERVAL = 30.0

    def __init__(self):
        # ``_available`` is diagnostic state only.  It must never become a
        # permanent circuit breaker: Redis can be restarted independently of
        # this process and the next request after the backoff must reconnect.
        self._available = False
        self._client = None
        self._lock = threading.Lock()
        self._next_retry_at = 0.0
        self._retry_delay = self._RETRY_MIN
        self._last_warn_at = 0.0
        self._last_error = ''

    def _new_client(self):
        """Construct a client. Split out so reconnect tests need no live Redis."""
        import redis  # optional dependency — guarded by the caller
        url = (getenv_compat('TOFU_REDIS_URL')
               or 'redis://127.0.0.1:6379/0')
        return redis.Redis.from_url(
            url, socket_connect_timeout=1.0, socket_timeout=1.0,
            health_check_interval=15, decode_responses=True)

    def _close_client(self, client) -> None:
        if client is None:
            return
        try:
            close = getattr(client, 'close', None)
            if callable(close):
                close()
            else:
                pool = getattr(client, 'connection_pool', None)
                if pool is not None:
                    pool.disconnect()
        except Exception as e:
            logger.debug('[RuntimeStateStore] redis client close failed: %s', e)

    def _schedule_retry_locked(self, error, operation: str) -> None:
        """Open the circuit briefly, then allow a later call to reconnect."""
        now = time.monotonic()
        delay = self._retry_delay * random.uniform(0.8, 1.2)
        self._next_retry_at = now + max(0.05, delay)
        self._retry_delay = min(self._RETRY_MAX,
                                max(self._RETRY_MIN, self._retry_delay * 2.0))
        self._available = False
        self._last_error = '%s: %s' % (operation, error)
        if now - self._last_warn_at >= self._WARN_INTERVAL:
            self._last_warn_at = now
            logger.warning(
                '[RuntimeStateStore] redis %s failed (%s) — failing open; '
                'automatic reconnect in %.1fs', operation, error,
                max(0.0, self._next_retry_at - now))

    def _command_failed(self, error, operation: str) -> None:
        """Invalidate a possibly-stale pool so the next retry is a clean dial."""
        with self._lock:
            old = self._client
            self._client = None
            self._schedule_retry_locked(error, operation)
        self._close_client(old)

    def _redis(self):
        """Lazily connect, with bounded exponential reconnect backoff."""
        if self._client is not None:
            return self._client
        if time.monotonic() < self._next_retry_at:
            return None
        with self._lock:
            if self._client is not None:
                return self._client
            if time.monotonic() < self._next_retry_at:
                return None
            client = None
            try:
                client = self._new_client()
                client.ping()
                self._client = client
                recovered = bool(self._last_error)
                self._available = True
                self._next_retry_at = 0.0
                self._retry_delay = self._RETRY_MIN
                self._last_error = ''
                logger.info('[RuntimeStateStore] redis backend %s',
                            'reconnected' if recovered else 'connected')
                return self._client
            except Exception as e:
                self._schedule_retry_locked(e, 'connect')
                self._close_client(client)
                return None

    def health(self) -> dict:
        """Non-blocking diagnostic snapshot for metrics/readiness surfaces."""
        now = time.monotonic()
        with self._lock:
            return {
                'backend': 'redis',
                'available': bool(self._client is not None and self._available),
                'reconnect_in_s': max(0.0, self._next_retry_at - now),
                'retry_delay_s': self._retry_delay,
                'last_error': self._last_error,
            }

    def _k(self, kind: str, key: str) -> str:
        return f'{self._KEY_NS}:{kind}:{key}'

    def acquire_lease(self, kind: str, key: str, ttl: float) -> bool:
        r = self._redis()
        if r is None:
            return True  # fail-open
        try:
            r.set(self._k(kind, key), '1', ex=max(1, int(ttl)))
            return True
        except Exception as e:
            self._command_failed(e, 'acquire')
            return True

    def _zcap_k(self, kind: str, count_prefix: str) -> str:
        # One sorted-set per (kind, count_prefix) holding the live slots.
        return f'{self._KEY_NS}:zcap:{kind}:{count_prefix}'

    def acquire_slot(self, kind: str, slot_key: str, limit: int, ttl: float,
                     count_prefix: str) -> bool:
        """Bounded acquire via a Redis SORTED SET (score = expiry deadline) —
        the single source of truth for the cap. Uses only WATCH/MULTI/EXEC +
        ``ZADD`` / ``ZSCORE`` / ``ZCOUNT`` / ``ZREMRANGEBYSCORE`` — guaranteed
        by fakeredis AND managed/cluster Redis (NO Lua EVAL / SCAN-in-Lua).

        Algorithm (optimistic-transaction admission — WATCH on the cap key,
        count-check and claim commit ATOMICALLY via MULTI/EXEC; a concurrent
        writer aborts the txn with WatchError and we retry against the fresh
        count):
          1. ``ZREMRANGEBYSCORE cap 0 now`` — evict members whose expiry
             deadline passed (crash-reclaim, no per-member TTL needed).
          2. ``ZSCORE`` hit = a live re-acquire → refresh in one txn (never
             a second count).
          3. ``ZCOUNT`` of live members ≥ limit → refuse WITHOUT inserting;
             else ZADD+EXPIRE in the same txn. Because the check and the
             insert commit atomically, two racers can never both observe
             ``count == limit-1`` and both admit.

        Why not ZADD-then-ZRANK (the previous design): the score was captured
        from the wall clock BEFORE the insert, so a racer descheduled between
        capture and ZADD could land an EARLIER-scored member after another
        racer's rank check — both then passed the gate. Measured 11 admits on
        a 10-cap (CI 3.12 leg, de81786): a starved box widens the
        capture→insert gap until the overshoot is deterministic.

        Consistency: ``count`` is ``ZCOUNT`` over the SAME set, so the
        admission gate and the reported count can never drift. A crash leaves
        members that expire by score (reclaimed on the next op's
        ZREMRANGEBYSCORE); a whole-key ``EXPIRE`` backstops an idle set.
        ``limit <= 0`` = unbounded. Fail-OPEN on backend error (admit)."""
        if limit <= 0:
            return self.acquire_lease(kind, slot_key, ttl)
        r = self._redis()
        if r is None:
            return True  # fail-open
        zk = self._zcap_k(kind, count_prefix)
        ttl_i = max(1, int(ttl))
        try:
            from redis.exceptions import WatchError
            for _attempt in range(64):
                now = time.time()
                try:
                    with r.pipeline() as p:
                        p.watch(zk)
                        p.zremrangebyscore(zk, 0, now)  # evict expired members
                        if p.zscore(zk, slot_key) is not None:
                            # Live re-acquire → refresh only, never a count.
                            p.multi()
                            p.zadd(zk, {slot_key: now + ttl_i})
                            p.expire(zk, ttl_i * 2)
                            p.execute()
                            return True
                        if p.zcount(zk, now, '+inf') >= limit:
                            p.unwatch()
                            return False
                        p.multi()
                        p.zadd(zk, {slot_key: now + ttl_i})
                        p.expire(zk, ttl_i * 2)  # whole-key idle backstop
                        p.execute()
                        return True
                except WatchError:
                    # A concurrent racer touched the set — re-read and retry.
                    # Bounded by the 64-attempt loop; logged so the code-quality
                    # silent-catch guard sees the intent (and operators can
                    # count real contention storms).
                    logger.debug('[RuntimeStateStore] acquire_slot WATCH retry '
                                 'for %s (attempt %d)', slot_key, _attempt)
                    continue
            # Far past any real burst; fail-open like a backend error rather
            # than refuse a legitimate caller forever.
            logger.warning('[RuntimeStateStore] acquire_slot watch retries '
                           'exhausted for %s — fail-open', slot_key)
            return True
        except Exception as e:
            self._command_failed(e, 'acquire_slot')
            return True

    def release_slot(self, kind: str, slot_key: str, count_prefix: str) -> None:
        """Release a slot acquired via :meth:`acquire_slot` — ``ZREM`` the
        member from its (kind, count_prefix) sorted set. Idempotent."""
        r = self._redis()
        if r is None:
            return
        try:
            r.zrem(self._zcap_k(kind, count_prefix), slot_key)
        except Exception as e:
            self._command_failed(e, 'release_slot')

    def refresh_slots(self, kind: str, slot_keys: Iterable[str], ttl: float,
                      count_prefix: str) -> None:
        """Heartbeat live admission slots in one pipeline.

        Refresh is unconditional: if Redis restarted and lost volatile state,
        the living replica reconstructs the truthful set of its still-running
        tasks.  A recovered count above the cap is safe and intentional — it
        blocks new admissions but never kills existing long work.
        """
        keys = list(slot_keys)
        if not keys:
            return
        r = self._redis()
        if r is None:
            return
        zk = self._zcap_k(kind, count_prefix)
        ttl_i = max(1, int(ttl))
        now = time.time()
        try:
            pipe = r.pipeline()
            pipe.zadd(zk, {key: now + ttl_i for key in keys})
            pipe.expire(zk, ttl_i * 2)
            pipe.execute()
        except Exception as e:
            self._command_failed(e, 'refresh_slots')

    def count_slots(self, kind: str, count_prefix: str) -> int:
        """Live slot count for a (kind, count_prefix) cap — ``ZCOUNT`` of
        members whose expiry deadline is still in the future. Derived from the
        SAME sorted set acquire_slot enforces on, so it cannot drift."""
        r = self._redis()
        if r is None:
            return 0  # fail-open
        try:
            zk = self._zcap_k(kind, count_prefix)
            now = time.time()
            r.zremrangebyscore(zk, 0, now)
            return int(r.zcount(zk, now, '+inf'))
        except Exception as e:
            self._command_failed(e, 'count_slots')
            return 0

    def live_slot_members(self, kind: str, count_prefix: str) -> List[str]:
        """The live slot members of a cap (for drift assertions/diagnostics)."""
        r = self._redis()
        if r is None:
            return []
        try:
            zk = self._zcap_k(kind, count_prefix)
            now = time.time()
            r.zremrangebyscore(zk, 0, now)
            return list(r.zrangebyscore(zk, now, '+inf'))
        except Exception as e:
            self._command_failed(e, 'live_slot_members')
            return []

    def refresh_lease(self, kind: str, key: str, ttl: float) -> bool:
        r = self._redis()
        if r is None:
            return True
        try:
            # Only refresh a key that still exists; re-create if gone.
            r.set(self._k(kind, key), '1', ex=max(1, int(ttl)))
            return True
        except Exception as e:
            self._command_failed(e, 'refresh')
            return True

    def release_lease(self, kind: str, key: str) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            # Slot keys are the single source of truth — releasing is just a
            # DEL. Idempotent (deleting a missing / already-expired key is a
            # no-op), so no double-decrement / drift is possible.
            r.delete(self._k(kind, key))
        except Exception as e:
            self._command_failed(e, 'release')

    def _scan(self, kind: str, key_prefix: str):
        r = self._redis()
        if r is None:
            return None
        match = f'{self._k(kind, key_prefix)}*'
        try:
            return list(r.scan_iter(match=match, count=500))
        except Exception as e:
            self._command_failed(e, 'scan')
            return None

    def count(self, kind: str, key_prefix: str = '') -> int:
        # Derived PURELY from the live slot keys (the single source of truth) —
        # there is no standalone counter to diverge from. Excludes any legacy
        # bookkeeping keys defensively.
        keys = self._scan(kind, key_prefix)
        if keys is None:
            return 0  # fail-open
        return sum(1 for k in keys if ':cnt:' not in k and ':meta:' not in k)

    def heartbeat(self, kind: str, keys: Iterable[str], ttl: float) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            pipe = r.pipeline()
            for key in keys:
                pipe.expire(self._k(kind, key), max(1, int(ttl)))
            pipe.execute()
        except Exception as e:
            self._command_failed(e, 'heartbeat')

    def set_value(self, kind: str, key: str, value: str, ttl: float) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            r.set(self._k(kind, key), value, ex=max(1, int(ttl)))
        except Exception as e:
            self._command_failed(e, 'set_value')

    def get_value(self, kind: str, key: str):
        r = self._redis()
        if r is None:
            return None
        try:
            return r.get(self._k(kind, key))
        except Exception as e:
            self._command_failed(e, 'get_value')
            return None

    def delete_value(self, kind: str, key: str) -> None:
        r = self._redis()
        if r is None:
            return
        try:
            r.delete(self._k(kind, key))
        except Exception as e:
            self._command_failed(e, 'delete_value')

    def close(self) -> None:
        """Close the pool. Safe to call repeatedly during server shutdown."""
        with self._lock:
            old = self._client
            self._client = None
            self._available = False
        self._close_client(old)

    def live_keys(self, kind: str, key_prefix: str = '') -> List[str]:
        keys = self._scan(kind, key_prefix)
        if keys is None:
            return []
        prefix = self._k(kind, '')
        return [k[len(prefix):] for k in keys]


# ═══════════════════════════════════════════════════════════════════════
#  Backend selection — read at call time (mirrors rate_limit_store)
# ═══════════════════════════════════════════════════════════════════════


_store_lock = threading.Lock()
_store: object = None
_store_backend: str = ''


def get_store():
    """Return the active runtime-state store, building it lazily.

    Backend from ``TOFU_RUNTIME_STATE_BACKEND``:
      * ``inproc`` (default) → :class:`InProcRuntimeStateStore`
      * ``redis`` → :class:`RedisRuntimeStateStore`
    An unrecognised value logs a WARN and falls back to inproc.
    """
    global _store, _store_backend
    desired = (getenv_compat('TOFU_RUNTIME_STATE_BACKEND')
               or 'inproc').strip().lower()
    if desired not in ('inproc', 'redis'):
        logger.warning('[RuntimeStateStore] unknown backend %r — defaulting '
                       'to inproc', desired)
        desired = 'inproc'
    with _store_lock:
        if _store is not None and _store_backend == desired:
            return _store
        _store = (RedisRuntimeStateStore() if desired == 'redis'
                  else InProcRuntimeStateStore())
        _store_backend = desired
        logger.info('[RuntimeStateStore] backend=%s active', desired)
    return _store


def reset_for_test():
    """Force the next ``get_store()`` to rebuild — test-only."""
    global _store, _store_backend
    with _store_lock:
        old = _store
        _store = None
        _store_backend = ''
    close = getattr(old, 'close', None)
    if callable(close):
        close()


__all__ = [
    'InProcRuntimeStateStore', 'RedisRuntimeStateStore',
    'get_store', 'reset_for_test',
]
