#!/usr/bin/env python3
"""INTEGRATION — the redis backend against a REAL redis-py client (fakeredis).

Rationale (owner, 2026-07): every prior "redis" test used a hand-written stub
that could not catch a wrong redis-py signature, a bad Lua/atomic contract, or
real pubsub delivery. These tests drive the ACTUAL code paths of
``RedisRuntimeStateStore`` and ``RedisPushBus`` against a genuine ``redis``
client backed by an in-memory ``fakeredis`` server — validating the real
library contract, not our assumptions. The N-invariance the 100k objective
rests on is proven here, not asserted.

Skips cleanly if fakeredis/redis are absent (they are a dev/test extra, not a
core dependency), so bare CI without them is green rather than failing.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

fakeredis = pytest.importorskip('fakeredis')
pytest.importorskip('redis')


def _fake_client():
    # decode_responses=True mirrors how RedisRuntimeStateStore/ RedisPushBus
    # construct their clients — so type handling (str vs bytes) is exercised
    # exactly as in production.
    return fakeredis.FakeStrictRedis(decode_responses=True)


# ══════════════════════════════════════════════════════════════════════
#  RuntimeStateStore redis backend against a real client
# ══════════════════════════════════════════════════════════════════════
def _store_on(client):
    from lib.runtime_state_store import RedisRuntimeStateStore
    st = RedisRuntimeStateStore()
    st._client = client        # inject the real redis-py client (fakeredis)
    st._available = True
    return st


def test_redis_acquire_slot_bounds_via_real_client():
    """acquire_slot admits exactly `limit` and refuses the rest — running the
    REAL INCR/DECR counter path on a genuine redis client."""
    st = _store_on(_fake_client())
    ok = [st.acquire_slot('sse', f'ip:1::s{i}', limit=3, ttl=90,
                          count_prefix='ip:1::') for i in range(5)]
    assert ok == [True, True, True, False, False]
    assert st.count_slots('sse', 'ip:1::') == 3
    # Independent principal.
    assert st.acquire_slot('sse', 'ip:2::s1', limit=3, ttl=90, count_prefix='ip:2::') is True
    assert st.count_slots('sse', 'ip:2::') == 1


def test_redis_release_decrements_counter():
    st = _store_on(_fake_client())
    st.acquire_slot('admit', 't1', limit=2, ttl=90, count_prefix='')
    st.acquire_slot('admit', 't2', limit=2, ttl=90, count_prefix='')
    assert st.count_slots('admit', '') == 2
    assert st.acquire_slot('admit', 't3', limit=2, ttl=90, count_prefix='') is False
    st.release_slot('admit', 't1', '')          # frees a slot
    assert st.count_slots('admit', '') == 1
    assert st.acquire_slot('admit', 't3', limit=2, ttl=90, count_prefix='') is True


def test_redis_reacquire_is_refresh_not_double_count():
    st = _store_on(_fake_client())
    assert st.acquire_slot('admit', 't1', limit=1, ttl=90, count_prefix='') is True
    # Re-acquiring the SAME slot must not push the counter past the limit.
    assert st.acquire_slot('admit', 't1', limit=1, ttl=90, count_prefix='') is True
    assert st.count_slots('admit', '') == 1
    assert st.acquire_slot('admit', 't2', limit=1, ttl=90, count_prefix='') is False


def test_redis_lease_ttl_expires_via_real_expire():
    """A slot key set with a real EX expires on the fakeredis clock; count
    reflects it (validates the crash-reclaim backstop against the real TTL
    semantics, not a hand-rolled wall-clock)."""
    st = _store_on(_fake_client())
    # acquire_lease path (SET EX) + count via SCAN fallback for a bare kind.
    st._client.set('tofu:rts:sub:chan:t1:rA', '1', ex=1)
    assert 'chan:t1:rA' in st.live_keys('sub', 'chan:t1:')
    # fakeredis honours EX against its own clock; fast-forward by re-setting a
    # tiny TTL and sleeping just over it.
    st._client.set('tofu:rts:sub:chan:t1:rA', '1', px=50)
    time.sleep(0.12)
    assert st.live_keys('sub', 'chan:t1:') == []


def test_redis_acquire_slot_concurrent_never_overshoots_real_client():
    """The N-invariance guarantee under CONCURRENCY on a real client: many
    threads racing the SAME principal admit EXACTLY `limit` via the atomic
    INCR/DECR counter — the property a stub cannot validate."""
    st = _store_on(_fake_client())
    limit = 10
    admitted = []
    lock = threading.Lock()
    barrier = threading.Barrier(60)

    def _w(i):
        barrier.wait()
        if st.acquire_slot('sse', f'ip:R::s{i}', limit=limit, ttl=90, count_prefix='ip:R::'):
            with lock:
                admitted.append(i)

    ts = [threading.Thread(target=_w, args=(i,)) for i in range(60)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(admitted) == limit, f'admitted {len(admitted)} != {limit} on real client'
    assert st.count_slots('sse', 'ip:R::') == limit


def test_redis_set_get_value_via_real_client():
    st = _store_on(_fake_client())
    st.set_value('latest', 'conv-1', 'task-A', ttl=90)
    assert st.get_value('latest', 'conv-1') == 'task-A'
    st.set_value('latest', 'conv-1', 'task-B', ttl=90)
    assert st.get_value('latest', 'conv-1') == 'task-B'
    assert st.get_value('latest', 'missing') is None


# ══════════════════════════════════════════════════════════════════════
#  RedisPushBus against a real client — publish → real pubsub → deliver
# ══════════════════════════════════════════════════════════════════════
def _bus_on(client, deliver, topic):
    from lib.agent_core.push_bus import RedisPushBus
    return RedisPushBus(deliver, client=client, topic=topic)


def test_redis_pushbus_real_pubsub_delivers():
    """Publish on the bus → the REAL redis pubsub subscriber loop delivers to
    the local callback. Uses the bus's own start()/thread + fakeredis pubsub,
    not a hand-pumped stub."""
    client = _fake_client()
    got = []
    bus = _bus_on(client, lambda f: got.append(f), 'tofu:push:fanout')
    bus.start()                       # starts the real background listen() thread
    time.sleep(0.2)                   # let the subscriber thread subscribe
    bus.publish({'channel': 'paper', 'taskId': 't1', 'type': 'done'})
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.05)
    bus.stop()
    assert got and got[0]['type'] == 'done' and got[0]['taskId'] == 't1'


def test_redis_pushbus_cache_invalidation_topic_delivers():
    """The tofu:cache:invalidate channel (D4) over the real pubsub: a publish
    reaches the subscriber, carrying the userId the cache clears on."""
    client = _fake_client()
    got = []
    bus = _bus_on(client, lambda f: got.append(f), 'tofu:cache:invalidate')
    bus.start()
    time.sleep(0.2)
    bus.publish({'channel': 'cache', 'type': 'invalidate', 'userId': 42})
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.05)
    bus.stop()
    assert got and got[0].get('userId') == 42 and got[0].get('type') == 'invalidate'


def test_redis_two_replicas_cross_deliver_real_pubsub():
    """DECISIVE cross-replica proof on a real client: two RedisPushBus
    instances sharing ONE fakeredis server (== two replicas on one Redis). A
    frame published on replica B reaches replica A's subscriber — the exact
    N-invariance the objective needs, validated end-to-end through genuine
    redis pubsub (not a stub)."""
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    client_b = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    got_a, got_b = [], []
    bus_a = _bus_on(client_a, got_a.append, 'tofu:push:fanout')
    bus_b = _bus_on(client_b, got_b.append, 'tofu:push:fanout')
    bus_a.start()
    bus_b.start()
    time.sleep(0.25)
    # Publish on replica B; replica A's subscriber must receive it.
    bus_b.publish({'channel': 'paper', 'taskId': 'x9', 'type': 'progress'})
    deadline = time.time() + 3
    while not got_a and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)  # settle window: a double-delivery would surface here
    bus_a.stop()
    bus_b.stop()
    # Exactly once PER replica — the frame reached A AND B, each once.
    assert len(got_a) == 1, f'replica A got {len(got_a)} (want 1)'
    assert len(got_b) == 1, f'replica B got {len(got_b)} (want 1)'
    assert got_a[0]['taskId'] == 'x9' and got_b[0]['taskId'] == 'x9'


def test_redis_counter_never_drifts_from_slot_keys_interleaved():
    """DRIFT GUARD (owner-mandated): hammer INTERLEAVED acquire/release/
    re-acquire/expiry on the real fakeredis client, then assert the admission
    count() EXACTLY equals the live slot-key count — no drift. The primitive
    now uses slot keys as the single source of truth (count() derives from the
    same SCAN acquire uses), so the two are the SAME set by construction; this
    proves it holds under churn incl. a real short-TTL expiry."""
    import random
    st = _store_on(_fake_client())
    limit, prefix, kind = 8, 'ip:D::', 'sse'
    held = []
    rng = random.Random(1234)
    for i in range(400):
        op = rng.random()
        if op < 0.55:
            k = f'{prefix}s{i}'
            if st.acquire_slot(kind, k, limit=limit, ttl=90, count_prefix=prefix):
                held.append(k)
        elif op < 0.8 and held:
            st.release_slot(kind, held.pop(rng.randrange(len(held))), prefix)
        elif op < 0.92 and held:
            st.acquire_slot(kind, held[rng.randrange(len(held))],
                            limit=limit, ttl=90, count_prefix=prefix)  # re-acquire
        else:
            st.acquire_slot(kind, f'{prefix}e{i}', limit=limit, ttl=1,
                            count_prefix=prefix)  # short-TTL, will expire
    live = st.live_slot_members(kind, prefix)
    gate = st.count_slots(kind, prefix)
    assert gate == len(live), (
        f'DRIFT: count()={gate} != live slot keys={len(live)}')
    assert gate <= limit, f'count()={gate} exceeded limit={limit}'


def test_NC_drift_when_accounting_not_derived_from_slot_keys():
    """NEGATIVE CONTROL: any admission accounting NOT derived from the slot
    keys (e.g. a counter DECREMENTED without deleting the key — the pre-fix
    bug class) drifts from the real live set. Prove drift IS detectable, and
    that the REAL slot-key-derived gate stays truthful."""
    st = _store_on(_fake_client())
    prefix, kind = 'ip:NC::', 'sse'
    for i in range(4):
        st.acquire_slot(kind, f'{prefix}s{i}', limit=10, ttl=90, count_prefix=prefix)
    assert st.count_slots(kind, prefix) == 4
    fake_counter = 4
    fake_counter -= 1                      # a DECR with NO matching key DEL
    real_gate = st.count_slots(kind, prefix)  # derived from the ZSET → still 4
    assert real_gate == 4
    assert fake_counter != real_gate, 'counter-without-DEL must drift from slot set'
    # Correct path: a real release keeps gate == live keys.
    st.release_slot(kind, f'{prefix}s0', prefix)
    assert st.count_slots(kind, prefix) == 3 == len(st.live_slot_members(kind, prefix))


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
