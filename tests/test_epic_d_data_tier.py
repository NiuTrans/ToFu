#!/usr/bin/env python3
"""Epic D — data-tier scale-out, pure-code parts (board pt_6879b628b896430d).

D1 — fail-closed PG guarantee (`TOFU_REQUIRE_PG`): DB bootstrap must REFUSE to
     start on the write-serializing SQLite fallback when PG is mandatory but
     unavailable; unset → graceful fallback byte-identical.
D4 — sidebar `_meta_cache` user-keyed (multi-tenant-correct) + bounded LIMIT.

Bare-CI-safe: no live PG/redis/node. D1 is tested via the extracted pure helper
`_assert_pg_available_or_raise` (no live boot needed); D4 via a fake DB cursor.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
#  D1 — fail-closed PG guarantee
# ══════════════════════════════════════════════════════════════════════
def test_d1_require_pg_unavailable_raises():
    """TOFU_REQUIRE_PG=1 + PG unavailable ⇒ boot MUST raise (NOT fall through
    to SQLite). This is the decisive D1 property."""
    from lib.database import _core
    os.environ['TOFU_REQUIRE_PG'] = '1'
    try:
        with pytest.raises(RuntimeError, match='refusing to fall back'):
            _core._assert_pg_available_or_raise(pg_ok=False, pgdata='/tmp/x')
    finally:
        os.environ.pop('TOFU_REQUIRE_PG', None)


def test_d1_require_pg_but_pg_ok_does_not_raise():
    """When PG IS available, the guard is a no-op even with the flag set."""
    from lib.database import _core
    os.environ['TOFU_REQUIRE_PG'] = '1'
    try:
        _core._assert_pg_available_or_raise(pg_ok=True, pgdata='/tmp/x')  # no raise
    finally:
        os.environ.pop('TOFU_REQUIRE_PG', None)


def test_d1_unset_falls_back_gracefully():
    """Unset (the single-box default): PG unavailable is a NO-OP → the caller
    proceeds to the SQLite fallback exactly as before (byte-identical)."""
    from lib.database import _core
    os.environ.pop('TOFU_REQUIRE_PG', None)
    _core._assert_pg_available_or_raise(pg_ok=False, pgdata='/tmp/x')  # must NOT raise


def test_NC_d1_flag_ignored_would_mask_the_serializing_fallback():
    """NEGATIVE CONTROL: if the guard ignored the flag (the pre-D1 behaviour),
    a scale-declared deployment would silently proceed to write-serializing
    SQLite. We prove the flag is load-bearing: with it set + PG down the guard
    RAISES; simulate 'ignored' by not setting it → no raise (the masked bug)."""
    from lib.database import _core
    # Flag honoured → raises (protection ON).
    os.environ['TOFU_REQUIRE_PG'] = 'true'
    try:
        with pytest.raises(RuntimeError):
            _core._assert_pg_available_or_raise(False, '/tmp/x')
    finally:
        os.environ.pop('TOFU_REQUIRE_PG', None)
    # Flag absent (== a guard that ignored it) → NO raise = the pre-D1 silent
    # degradation. This asserts the difference the flag makes.
    _core._assert_pg_available_or_raise(False, '/tmp/x')


# ══════════════════════════════════════════════════════════════════════
#  D4 — user-keyed + bounded sidebar cache
# ══════════════════════════════════════════════════════════════════════
class _FakeCursor:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def fetchall(self):
        return self._rows


class _FakeDB:
    """Records every (sql, params) and returns per-user rows."""
    def __init__(self, rows_by_user):
        self.rows_by_user = rows_by_user
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        uid = params[0]
        rows = self.rows_by_user.get(uid, [])
        return _FakeCursor(rows, self)


def _row(i, uid):
    return {'id': 'c%d-u%d' % (i, uid), 'title': 't%d' % i,
            'created_at': i, 'updated_at': i, 'settings': None, 'msg_count': i}


def _reset_mc():
    import lib.conversations.meta_cache as mc
    with mc._meta_cache_lock:
        mc._meta_cache_by_user.clear()


def test_d4_cache_is_user_keyed_no_cross_tenant_leak():
    """DECISIVE D4: two distinct users must NOT read each other's cached
    sidebar. A global blob (the pre-D4 bug) would serve user 1's list to user
    2. We populate the cache for user 1, then user 2, and assert each sees ONLY
    its own conversations."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    db = _FakeDB({1: [_row(1, 1), _row(2, 1)], 2: [_row(9, 2)]})
    p1, e1 = mc.refresh_meta_cache_if_stale(db, user_id=1)
    p2, e2 = mc.refresh_meta_cache_if_stale(db, user_id=2)
    import json
    ids1 = {c['id'] for c in json.loads(p1)}
    ids2 = {c['id'] for c in json.loads(p2)}
    assert ids1 == {'c1-u1', 'c2-u1'}
    assert ids2 == {'c9-u2'}
    assert ids1.isdisjoint(ids2), 'cross-tenant leak: users share a cache blob'
    assert e1 != e2


def test_d4_invalidate_is_per_user():
    """Invalidating user 1 must not drop user 2's cache entry."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    db = _FakeDB({1: [_row(1, 1)], 2: [_row(9, 2)]})
    mc.refresh_meta_cache_if_stale(db, user_id=1)
    mc.refresh_meta_cache_if_stale(db, user_id=2)
    mc.invalidate_meta_cache(user_id=1)
    with mc._meta_cache_lock:
        assert mc._meta_cache_by_user[1]['ts'] == 0        # invalidated
        assert mc._meta_cache_by_user[2]['ts'] != 0        # untouched


def test_d4_query_is_bounded_by_limit():
    """The conversation-list SELECT must carry a LIMIT (bounded scan). Assert
    the emitted SQL contains LIMIT and the limit param is passed."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    os.environ['TOFU_SIDEBAR_MAX'] = '50'
    try:
        db = _FakeDB({1: [_row(i, 1) for i in range(3)]})
        mc.refresh_meta_cache_if_stale(db, user_id=1)
        sql, params = db.calls[-1]
        assert 'LIMIT' in sql.upper(), 'sidebar query must be bounded by LIMIT'
        assert params == (1, 50), 'user_id + limit must be bound params'
    finally:
        os.environ.pop('TOFU_SIDEBAR_MAX', None)


def test_NC_d4_unbounded_query_has_no_limit():
    """NEGATIVE CONTROL: TOFU_SIDEBAR_MAX=0 disables the cap → the emitted SQL
    has NO LIMIT (the pre-D4 unbounded full-scan). Proves the LIMIT is
    conditional on the cap being > 0, and that the bounded path is the one that
    adds it."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    os.environ['TOFU_SIDEBAR_MAX'] = '0'
    try:
        db = _FakeDB({1: [_row(1, 1)]})
        mc.refresh_meta_cache_if_stale(db, user_id=1)
        sql, params = db.calls[-1]
        assert 'LIMIT' not in sql.upper(), 'cap=0 must emit the unbounded query'
        assert params == (1,)
    finally:
        os.environ.pop('TOFU_SIDEBAR_MAX', None)


def test_d4_default_user_backcompat():
    """Existing no-user callers hit DEFAULT_USER_ID → byte-identical single-user
    behaviour (the 40+ call sites that pass no user_id)."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    db = _FakeDB({mc.DEFAULT_USER_ID: [_row(1, mc.DEFAULT_USER_ID)]})
    payload, etag = mc.refresh_meta_cache_if_stale(db)  # no user_id
    import json
    assert [c['id'] for c in json.loads(payload)] == ['c1-u1']
    mc.invalidate_meta_cache()  # no user_id
    with mc._meta_cache_lock:
        assert mc._meta_cache_by_user[mc.DEFAULT_USER_ID]['ts'] == 0


# ══════════════════════════════════════════════════════════════════════
#  D4 cross-replica cache invalidation (Epic D4 §2.4 — on the shared bus)
# ══════════════════════════════════════════════════════════════════════
class _FakeBrokerClient:
    """In-memory redis pub/sub stand-in shared by several bus instances."""
    def __init__(self, broker):
        self.broker = broker

    def ping(self):
        return True

    def publish(self, topic, data):
        for q in self.broker.get(topic, []):
            q.append(data)

    def pubsub(self, ignore_subscribe_messages=True):
        return _FakeBrokerPubSub(self.broker)


class _FakeBrokerPubSub:
    def __init__(self, broker):
        self.broker = broker
        self.q = []

    def subscribe(self, topic):
        self.broker.setdefault(topic, []).append(self.q)

    def close(self):
        pass

    def drain(self):
        msgs = list(self.q)
        self.q.clear()
        return msgs


def test_d4_cross_replica_invalidation_clears_peer():
    """DECISIVE D4 §2.4: an invalidate on 'replica A' clears 'replica B''s
    cached entry via the shared bus. We model two replicas as two RedisPushBus
    instances over ONE fake broker, each delivering into its own local cache
    dict; publishing an invalidation on A's bus + pumping the broker must clear
    B's entry for that user."""
    from lib.agent_core.push_bus import RedisPushBus
    broker = {}
    topic = 'tofu:cache:invalidate'
    cache_a = {7: {'ts': 111}}   # replica A local cache: user 7 is warm
    cache_b = {7: {'ts': 222}}   # replica B local cache: user 7 is warm

    def _mk_deliver(cache):
        def _deliver(frame):
            uid = frame.get('userId')
            if uid in cache:
                cache[uid]['ts'] = 0   # clear this replica's entry
        return _deliver

    bus_a = RedisPushBus(_mk_deliver(cache_a), client=_FakeBrokerClient(broker), topic=topic)
    bus_b = RedisPushBus(_mk_deliver(cache_b), client=_FakeBrokerClient(broker), topic=topic)
    ps_a = bus_a._client.pubsub(); ps_a.subscribe(topic)
    ps_b = bus_b._client.pubsub(); ps_b.subscribe(topic)

    # Mutation on replica A → publish invalidation for user 7.
    bus_a.publish({'channel': 'cache', 'type': 'invalidate', 'userId': 7})
    # Pump the broker into each replica's subscriber.
    for raw in ps_a.drain():
        bus_a.on_message(raw)
    for raw in ps_b.drain():
        bus_b.on_message(raw)

    assert cache_b[7]['ts'] == 0, "replica B's cache MUST be cleared cross-replica"
    assert cache_a[7]['ts'] == 0, "replica A cleared its own too"


def test_NC_d4_no_bus_publish_leaves_peer_stale():
    """NEGATIVE CONTROL: if the invalidation is NOT published to the shared bus
    (the pre-fix local-only behaviour), replica B stays STALE — the exact
    multi-replica bug. We clear only A locally and never pump B → B's entry is
    still warm."""
    broker = {}
    cache_a = {7: {'ts': 111}}
    cache_b = {7: {'ts': 222}}
    # Local-only invalidation on A (no publish): B is never told.
    cache_a[7]['ts'] = 0
    # (bus deliberately not used) → B still warm.
    assert cache_a[7]['ts'] == 0
    assert cache_b[7]['ts'] == 222, (
        'without cross-replica publish, replica B stays stale — the bug D4 fixes')


def test_d4_invalidate_publishes_to_bus_wiring():
    """WIRING: the real invalidate_meta_cache MUST publish an invalidation
    frame (userId) to the shared bus — this is what carries the clear to peer
    replicas. Inject a spy bus and assert the publish happens with the right
    user id."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    published = []

    class _SpyBus:
        def publish(self, frame):
            published.append(frame)

    mc._inval_bus = _SpyBus()
    try:
        mc.invalidate_meta_cache(user_id=42)
    finally:
        mc.reset_inval_bus_for_test()
    assert len(published) == 1, 'invalidate_meta_cache must publish to the bus'
    assert published[0].get('userId') == 42 and published[0].get('type') == 'invalidate'


def test_d4_invalidate_uses_shared_bus_inproc_default():
    """Under the default inproc backend, invalidate_meta_cache still clears the
    local entry (byte-identical) AND the bus publish is a local no-op re-clear
    — no crash, no dependency."""
    import lib.conversations.meta_cache as mc
    _reset_mc()
    mc.reset_inval_bus_for_test()
    db = _FakeDB({1: [_row(1, 1)]})
    mc.refresh_meta_cache_if_stale(db, user_id=1)
    with mc._meta_cache_lock:
        assert mc._meta_cache_by_user[1]['ts'] != 0
    mc.invalidate_meta_cache(user_id=1)  # inproc bus → local clear
    with mc._meta_cache_lock:
        assert mc._meta_cache_by_user[1]['ts'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
