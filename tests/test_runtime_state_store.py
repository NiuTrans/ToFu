#!/usr/bin/env python3
"""§0 step 1 — the shared lease/counter/heartbeat store.

Acceptance criteria (owner, 2026-07-02):
  1. `get_store()` factory: inproc default | redis under the flag, memoized,
     reset_for_test().
  2. inproc backend byte-equivalent to today (in-proc dict + wall-clock TTL,
     no dependency).
  3. personal fail-open and distributed fail-closed admission are explicit.
  4. redis is an OPTIONAL dep — module imports and inproc works with no redis
     package installed (bare CI); redis path is mock-driven here.
  5. NC-biting tests + the lease-TTL benchmark hook (living lease refreshed
     under heartbeat never expires; post-expiry reclaim ≤ ~ttl).

Bare-CI-safe: no DB, no node, no live redis server, no network.
"""
import importlib
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _fresh():
    import lib.runtime_state_store as m
    m.reset_for_test()
    return m


# ══════════════════════════════════════════════════════════════════════
#  Criterion 2 — inproc TTL lease semantics
# ══════════════════════════════════════════════════════════════════════
def test_inproc_lease_expires_after_ttl():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    s.acquire_lease('admit', 't1', ttl=0.15)
    assert s.count('admit') == 1          # live immediately after acquire
    time.sleep(0.2)
    assert s.count('admit') == 0          # expired after ttl — the reclaim


def test_inproc_release_is_eager():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    s.acquire_lease('sse', 'p1', ttl=100)
    assert s.count('sse') == 1
    s.release_lease('sse', 'p1')          # normal (non-crash) path
    assert s.count('sse') == 0


def test_inproc_count_prefix_and_kind_isolation():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    s.acquire_lease('sse', 'ip:1.1.1.1:rA:s1', ttl=100)
    s.acquire_lease('sse', 'ip:1.1.1.1:rA:s2', ttl=100)
    s.acquire_lease('sse', 'ip:2.2.2.2:rA:s1', ttl=100)
    s.acquire_lease('admit', 'ip:1.1.1.1:x', ttl=100)   # different kind
    # per-principal count via prefix; the admit kind must not leak in.
    assert s.count('sse', 'ip:1.1.1.1:') == 2
    assert s.count('sse', 'ip:2.2.2.2:') == 1
    assert s.count('sse') == 3
    assert s.count('admit') == 1


def test_inproc_live_keys_enumerates_unexpired():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    s.acquire_lease('sub', 'chat:t1:rA', ttl=100)
    s.acquire_lease('sub', 'chat:t1:rB', ttl=0.1)
    time.sleep(0.15)
    live = s.live_keys('sub', 'chat:t1:')
    assert live == ['chat:t1:rA']          # expired rB dropped


# ══════════════════════════════════════════════════════════════════════
#  Criterion 5 — heartbeat keeps a LIVING lease alive; lease-TTL benchmark
# ══════════════════════════════════════════════════════════════════════
def test_heartbeat_keeps_living_lease_alive_past_ttl():
    """The design's core invariant (§5.2): a lease refreshed by the heartbeat
    every ttl/3 NEVER expires while the owner is alive — so a long task is
    safe. Here ttl=0.15s, heartbeat every 0.05s, run for 0.3s (2× ttl): the
    lease must still be live at the end BECAUSE we kept refreshing it."""
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    ttl = 0.15
    s.acquire_lease('admit', 'living', ttl=ttl)
    t_end = time.time() + 0.3
    while time.time() < t_end:
        s.heartbeat('admit', ['living'], ttl=ttl)   # the ttl/3 refresh loop
        time.sleep(ttl / 3)
    assert s.count('admit') == 1, 'a heartbeated living lease must not expire'


def test_lease_ttl_benchmark_hook_post_crash_reclaim_within_ttl():
    """Benchmark hook ruling 2 requires: after heartbeats STOP (crash), the
    lease is reclaimed within ~ttl. Acquire, then STOP heartbeating (simulated
    crash) and assert reclaim happens by ttl + slack, not before ~ttl."""
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    ttl = 0.2
    s.acquire_lease('admit', 'crashed', ttl=ttl)
    # No heartbeat (crash). Just before ttl it must still be held...
    time.sleep(ttl * 0.5)
    assert s.count('admit') == 1, 'lease must survive until ~ttl'
    # ...and reclaimed by ttl + slack.
    time.sleep(ttl * 0.75)
    assert s.count('admit') == 0, 'crashed lease must reclaim within ~ttl'


def test_NC_never_expiring_lease_leaks_capacity():
    """NEGATIVE CONTROL for the reclaim contract: a store whose leases NEVER
    expire (ttl ignored — the dead-replica capacity-leak bug) keeps the slot
    forever, so `count` never drops and global capacity monotonically shrinks.
    We simulate the bug with an infinite ttl and prove the slot is still held
    long after a real ttl would have reclaimed it — the exact failure mode the
    lease-TTL primitive exists to prevent."""
    from lib.runtime_state_store import InProcRuntimeStateStore
    good = InProcRuntimeStateStore()
    good.acquire_lease('admit', 'x', ttl=0.1)
    time.sleep(0.15)
    assert good.count('admit') == 0        # correct: reclaimed

    buggy = InProcRuntimeStateStore()
    buggy.acquire_lease('admit', 'x', ttl=10_000)   # "never expires"
    time.sleep(0.15)
    assert buggy.count('admit') == 1       # LEAK: slot still held — the bug


# ══════════════════════════════════════════════════════════════════════
#  ATOMIC bounded acquire — never overshoots `limit`, even under a race
# ══════════════════════════════════════════════════════════════════════
def test_acquire_slot_bounds_count():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    # limit=3 under the principal prefix 'ip:5:'
    ok = [s.acquire_slot('sse', f'ip:5:rA:s{i}', limit=3, ttl=100,
                          count_prefix='ip:5:') for i in range(5)]
    assert ok == [True, True, True, False, False]
    assert s.count('sse', 'ip:5:') == 3
    # A different principal is independent.
    assert s.acquire_slot('sse', 'ip:6:rA:s1', limit=3, ttl=100,
                          count_prefix='ip:6:') is True


def test_acquire_slot_reacquire_is_refresh_not_new_admit():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    assert s.acquire_slot('admit', 't1', limit=1, ttl=100, count_prefix='') is True
    # Re-acquiring the SAME slot must not be blocked by the limit (it's held).
    assert s.acquire_slot('admit', 't1', limit=1, ttl=100, count_prefix='') is True
    assert s.count('admit') == 1
    # A DIFFERENT slot at limit=1 is refused.
    assert s.acquire_slot('admit', 't2', limit=1, ttl=100, count_prefix='') is False


def test_acquire_slot_limit_zero_unbounded():
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    for i in range(1000):
        assert s.acquire_slot('admit', f't{i}', limit=0, ttl=100,
                              count_prefix='') is True


def test_acquire_slot_concurrent_never_overshoots():
    """The strictness guarantee under CONCURRENCY: many threads racing to
    acquire the SAME principal's slots must admit EXACTLY `limit`, never more.
    This is what a count-then-acquire (check-then-act) cannot guarantee and
    the atomic primitive must."""
    import threading
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    limit = 10
    n_threads = 100
    admitted = []
    admitted_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _worker(i):
        barrier.wait()  # maximize the race — all fire together
        ok = s.acquire_slot('sse', f'ip:7:s{i}', limit=limit, ttl=100,
                            count_prefix='ip:7:')
        if ok:
            with admitted_lock:
                admitted.append(i)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == limit, (
        f'admitted {len(admitted)} != limit {limit} — the bounded acquire '
        f'overshot under concurrency')
    assert s.count('sse', 'ip:7:') == limit


def test_NC_check_then_act_overshoots_under_concurrency():
    """NEGATIVE CONTROL for atomicity: a NON-atomic check-then-act
    implementation (count(), release lock, then acquire_lease()) admits MORE
    than `limit` when threads race in the gap — the exact bug the atomic
    primitive prevents. We build that racy shape explicitly and show it
    overshoots; the atomic acquire_slot above does not."""
    import threading
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    limit = 10
    n_threads = 100
    admitted = []
    admitted_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _racy_acquire(slot_key):
        # check-then-act: count is a separate lock hold from acquire → the gap
        # between them is the race window.
        if s.count('sse', 'ip:8:') >= limit:
            return False
        time.sleep(0.0005)          # widen the window so the race is reliable
        s.acquire_lease('sse', slot_key, ttl=100)
        return True

    def _worker(i):
        barrier.wait()
        if _racy_acquire(f'ip:8:s{i}'):
            with admitted_lock:
                admitted.append(i)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The racy path OVERSHOOTS — this is the failure mode. If this assertion
    # ever flips (racy path stayed within limit), the test lost its bite.
    assert len(admitted) > limit, (
        'check-then-act did not overshoot — the atomicity NC lost its bite; '
        'widen the race window')


# ══════════════════════════════════════════════════════════════════════
#  Criterion 1 — backend factory selection + memoization
# ══════════════════════════════════════════════════════════════════════
def test_default_backend_is_inproc():
    m = _fresh()
    old = os.environ.pop('TOFU_RUNTIME_STATE_BACKEND', None)
    try:
        importlib.reload(m)
        m.reset_for_test()
        assert type(m.get_store()).__name__ == 'InProcRuntimeStateStore'
    finally:
        if old is not None:
            os.environ['TOFU_RUNTIME_STATE_BACKEND'] = old
        importlib.reload(m)
        m.reset_for_test()


def test_redis_backend_selected_under_flag():
    m = _fresh()
    os.environ['TOFU_RUNTIME_STATE_BACKEND'] = 'redis'
    try:
        importlib.reload(m)
        m.reset_for_test()
        # Constructing the redis store must NOT require the redis package or a
        # live server (lazy connect) — the class is selected, connection is
        # deferred to first use.
        assert type(m.get_store()).__name__ == 'RedisRuntimeStateStore'
    finally:
        os.environ.pop('TOFU_RUNTIME_STATE_BACKEND', None)
        importlib.reload(m)
        m.reset_for_test()


def test_unknown_backend_falls_back_to_inproc():
    m = _fresh()
    os.environ['TOFU_RUNTIME_STATE_BACKEND'] = 'bogus'
    try:
        importlib.reload(m)
        m.reset_for_test()
        assert type(m.get_store()).__name__ == 'InProcRuntimeStateStore'
    finally:
        os.environ.pop('TOFU_RUNTIME_STATE_BACKEND', None)
        importlib.reload(m)
        m.reset_for_test()


def test_memoized_and_swap_rebuilds():
    m = _fresh()
    try:
        os.environ.pop('TOFU_RUNTIME_STATE_BACKEND', None)
        importlib.reload(m)
        m.reset_for_test()
        s1 = m.get_store()
        s2 = m.get_store()
        assert s1 is s2                    # memoized within a backend
        os.environ['TOFU_RUNTIME_STATE_BACKEND'] = 'redis'
        s3 = m.get_store()
        assert s3 is not s1                # backend swap rebuilds
        assert type(s3).__name__ == 'RedisRuntimeStateStore'
    finally:
        os.environ.pop('TOFU_RUNTIME_STATE_BACKEND', None)
        importlib.reload(m)
        m.reset_for_test()


# ══════════════════════════════════════════════════════════════════════
#  Criterion 3 + 4 — redis fail-open (mocked; no live server / package)
# ══════════════════════════════════════════════════════════════════════
def test_redis_unreachable_fails_open(monkeypatch):
    """Criterion 3: when the redis backend can't connect, acquire returns True
    (fail-open, cap stops enforcing rather than crashing) and count returns 0.
    We force _redis() to report unavailable — no live server involved."""
    from lib.runtime_state_store import RedisRuntimeStateStore
    s = RedisRuntimeStateStore()
    # Simulate connect failure by monkeypatching the lazy connector.
    monkeypatch.setattr(s, '_redis', lambda: None)
    assert s.acquire_lease('admit', 't1', ttl=90) is True   # fail-open
    assert s.count('admit') == 0                            # fail-open
    s.release_lease('admit', 't1')                          # no crash
    s.heartbeat('admit', ['t1'], ttl=90)                    # no crash
    assert s.live_keys('admit') == []


def test_distributed_redis_outage_refuses_new_admission(monkeypatch):
    """Accepted work may continue, but new slots fail closed for 503 callers."""
    from lib.runtime_state_store import RedisRuntimeStateStore

    store = RedisRuntimeStateStore(fail_open=False)
    monkeypatch.setattr(store, '_redis', lambda: None)

    assert store.acquire_lease('admit', 'new-task', ttl=90) is False
    assert store.acquire_slot(
        'admit', 'new-slot', limit=10, ttl=90, count_prefix='') is False
    assert store.health()['admission_failure_policy'] == 'closed'


def test_redis_retries_after_initial_connect_failure(monkeypatch):
    """A Redis restart must heal without restarting the application process."""
    from lib.runtime_state_store import RedisRuntimeStateStore

    class _RecoveredClient:
        def __init__(self):
            self.values = {}

        def ping(self):
            return True

        def set(self, key, value, **_kwargs):
            self.values[key] = value
            return True

        def close(self):
            pass

    recovered = _RecoveredClient()
    attempts = iter([RuntimeError('redis booting'), recovered])
    s = RedisRuntimeStateStore()
    monkeypatch.setattr(s, '_new_client', lambda: next(attempts))

    # First call fails open, but does not permanently disable the backend.
    assert s.acquire_lease('admit', 'long-task', ttl=90) is True
    assert s.health()['available'] is False

    # Move past the backoff without sleeping; the next operation reconnects.
    s._next_retry_at = 0.0
    assert s.acquire_lease('admit', 'long-task', ttl=90) is True
    assert s.health()['available'] is True
    assert recovered.values['tofu:rts:admit:long-task'] == '1'


def test_refresh_slots_reconstructs_living_work_after_store_recovery():
    """Lease expiry/recovery changes accounting only; it never kills long work."""
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    assert s.acquire_slot('admit', 'task-a', limit=1, ttl=0.05,
                          count_prefix='') is True
    time.sleep(0.07)
    assert s.count_slots('admit', '') == 0
    s.refresh_slots('admit', ['task-a'], ttl=0.2, count_prefix='')
    assert s.count_slots('admit', '') == 1
    assert s.acquire_slot('admit', 'task-b', limit=1, ttl=0.2,
                          count_prefix='') is False


def test_redis_backend_functional_with_real_fakeredis():
    """Criterion 4, done RIGHT: drive the RedisRuntimeStateStore against a REAL
    redis-py client backed by an in-memory fakeredis server — exercising the
    genuine INCR/DECR/SET/GET/DEL/SCAN contract, NOT a hand-written stub that
    encodes our own assumptions. (Deep end-to-end coverage — incl. concurrency
    and real pubsub — lives in tests/test_redis_backend_integration.py.) Skips
    cleanly when fakeredis is not installed (dev/test extra)."""
    fakeredis = pytest.importorskip('fakeredis')
    pytest.importorskip('redis')
    from lib.runtime_state_store import RedisRuntimeStateStore
    s = RedisRuntimeStateStore()
    s._client = fakeredis.FakeStrictRedis(decode_responses=True)
    s._available = True
    # acquire_slot bounds via the real client (ZSET slot primitive).
    got = [s.acquire_slot('sse', f'ip:7::s{i}', limit=2, ttl=90,
                          count_prefix='ip:7::') for i in range(4)]
    assert got == [True, True, False, False]
    assert s.count_slots('sse', 'ip:7::') == 2
    # acquire_lease + SCAN-fallback count for a non-counted kind.
    s.acquire_lease('sub', 'ip:9:rA:s1', ttl=90)
    s.acquire_lease('sub', 'ip:9:rA:s2', ttl=90)
    assert s.count('sub', 'ip:9:') == 2
    s.release_lease('sub', 'ip:9:rA:s1')
    assert sorted(s.live_keys('sub', 'ip:9:')) == ['ip:9:rA:s2']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
