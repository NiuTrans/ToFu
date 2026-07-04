#!/usr/bin/env python3
"""Epic B — cross-replica push fan-out (board pt_823ff5a3bf004c40).

The bug: PushHub fan-out is process-local, so a frame published on the replica
that owns a task is silently dropped for a subscriber whose /api/push WS lives
on a DIFFERENT replica. The fix: publish → shared bus → every replica
re-delivers to its own local subscribers.

Tests (bare-CI-safe: no live redis/DB/node; a FAKE in-memory broker stands in
for redis pub/sub):
  * inproc default: publish delivers locally, byte-identical to before.
  * NC (the exact Epic B bug): two INPROC hubs (separate processes) — a frame
    published on hub B does NOT reach hub A's subscriber (drop). This is what
    the redis bus fixes.
  * FIX: two hubs sharing a fake redis broker — a frame published on hub B
    DOES reach hub A's subscriber (cross-replica delivery).
  * webhook listeners fire once on the PUBLISHING hub only.
  * fail-open: bus publish error → local delivery, no crash.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


class _FakeClient:
    """A tiny in-memory redis stand-in shared by several hubs = one broker.

    Supports publish + a synchronous pubsub whose messages we pump by hand so
    tests are deterministic (no background thread / no real socket)."""

    def __init__(self, broker):
        self.broker = broker  # shared dict: topic -> list[subscriber_queue]

    def ping(self):
        return True

    def publish(self, topic, data):
        for q in self.broker.get(topic, []):
            q.append(data)

    def pubsub(self, ignore_subscribe_messages=True):
        return _FakePubSub(self.broker)


class _FakePubSub:
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


def _make_hub(bus=None):
    """Build a fresh PushHub with an injected bus (so no module-global reuse)."""
    from lib.agent_core.push import PushHub
    h = PushHub()
    if bus is not None:
        h._bus = bus
        h._bus_started = True
    return h


class _SyncClient:
    """A PushClient stand-in that records enqueued frames synchronously."""

    def __init__(self):
        self.frames = []
        self._connected = True

    def enqueue(self, frame):
        self.frames.append(frame)


def test_inproc_publish_delivers_locally():
    """Default inproc: publish → local subscriber gets the frame (byte-identical
    to the pre-Epic-B path). No loop → direct enqueue."""
    h = _make_hub()  # inproc bus (no TOFU_RUNTIME_STATE_BACKEND)
    c = _SyncClient()
    h.subscribe(c, 'paper', 't1')
    h.push_event('paper', 't1', {'type': 'progress', 'pct': 42})
    assert len(c.frames) == 1
    assert c.frames[0]['type'] == 'progress' and c.frames[0]['taskId'] == 't1'


def test_NC_two_inproc_hubs_drop_cross_replica_frame():
    """NEGATIVE CONTROL = the exact Epic B bug. Two INPROC hubs model two
    replicas with SEPARATE in-process state. A subscriber on hub A; the event
    published on hub B. Because inproc has no shared bus, hub A's client gets
    NOTHING — the silent cross-replica drop. This is what redis fixes."""
    hub_a = _make_hub()
    hub_b = _make_hub()
    client_on_a = _SyncClient()
    hub_a.subscribe(client_on_a, 'paper', 't9')
    # Event happens on replica B (owns the task); A holds the WS.
    hub_b.push_event('paper', 't9', {'type': 'done'})
    assert client_on_a.frames == [], (
        'inproc: cross-replica frame must NOT arrive (the bug the bus fixes)')


def test_FIX_shared_bus_delivers_cross_replica():
    """With a shared bus, a frame published on hub B reaches the subscriber on
    hub A — cross-replica delivery, the Epic B fix. We pump the fake broker's
    queues into each hub's bus.on_message to simulate the subscriber loops."""
    from lib.agent_core.push_bus import RedisPushBus
    broker = {}
    # Each hub gets its OWN RedisPushBus over the SAME broker + client.
    bus_a = RedisPushBus(deliver_fn=None, client=_FakeClient(broker))
    bus_b = RedisPushBus(deliver_fn=None, client=_FakeClient(broker))
    hub_a = _make_hub(bus_a)
    hub_b = _make_hub(bus_b)
    bus_a._deliver = hub_a._deliver_frame
    bus_b._deliver = hub_b._deliver_frame
    # Each hub's subscriber loop = a fake pubsub subscribed to the topic.
    ps_a = bus_a._client.pubsub()
    ps_a.subscribe('tofu:push:fanout')
    ps_b = bus_b._client.pubsub()
    ps_b.subscribe('tofu:push:fanout')

    client_on_a = _SyncClient()
    hub_a.subscribe(client_on_a, 'paper', 't9')

    # Publish on replica B.
    hub_b.push_event('paper', 't9', {'type': 'done', 'ok': True})

    # Pump the broker → each replica's subscriber loop delivers locally.
    for raw in ps_a.drain():
        bus_a.on_message(raw)
    for raw in ps_b.drain():
        bus_b.on_message(raw)

    assert len(client_on_a.frames) == 1, 'cross-replica frame must arrive via bus'
    f = client_on_a.frames[0]
    assert f['type'] == 'done' and f['ok'] is True and f['taskId'] == 't9'
    assert '_bcast' not in f


def test_broadcast_reaches_all_local_clients_via_bus():
    from lib.agent_core.push_bus import RedisPushBus
    broker = {}
    bus_a = RedisPushBus(deliver_fn=None, client=_FakeClient(broker))
    hub_a = _make_hub(bus_a)
    bus_a._deliver = hub_a._deliver_frame
    ps_a = bus_a._client.pubsub()
    ps_a.subscribe('tofu:push:fanout')
    c1, c2 = _SyncClient(), _SyncClient()
    hub_a.register(c1)
    hub_a.register(c2)
    hub_a.broadcast('notify', {'type': 'config_change'})
    for raw in ps_a.drain():
        bus_a.on_message(raw)
    assert len(c1.frames) == 1 and len(c2.frames) == 1
    # The internal routing marker must be stripped before the client sees it.
    assert '_bcast' not in c1.frames[0]


def test_webhook_listener_fires_once_on_publishing_hub():
    """In-process listeners (webhooks) run on the PUBLISHING replica only, so
    they fire once fleet-wide — not once per replica that re-delivers."""
    from lib.agent_core.push_bus import RedisPushBus
    broker = {}
    bus_a = RedisPushBus(deliver_fn=None, client=_FakeClient(broker))
    bus_b = RedisPushBus(deliver_fn=None, client=_FakeClient(broker))
    hub_a = _make_hub(bus_a)
    hub_b = _make_hub(bus_b)
    bus_a._deliver = hub_a._deliver_frame
    bus_b._deliver = hub_b._deliver_frame
    ps_a = bus_a._client.pubsub(); ps_a.subscribe('tofu:push:fanout')
    ps_b = bus_b._client.pubsub(); ps_b.subscribe('tofu:push:fanout')

    fired = []
    # A listener registered on BOTH replicas (as in a real fleet).
    hub_a.add_listener(lambda ch, tid, pl: fired.append(('a', ch)))
    hub_b.add_listener(lambda ch, tid, pl: fired.append(('b', ch)))

    hub_b.push_event('paper', 't1', {'type': 'progress'})  # publish on B
    for raw in ps_a.drain():
        bus_a.on_message(raw)
    for raw in ps_b.drain():
        bus_b.on_message(raw)
    # Only B (the publisher) fired its listener; A did NOT (delivery ≠ listener).
    assert fired == [('b', 'paper')], f'listener fired wrong: {fired}'


def test_publish_fails_open_to_local_on_bus_error():
    """If the bus publish raises, the frame is still delivered locally (fail-
    open) — never a crash / lost request path."""
    from lib.agent_core.push_bus import RedisPushBus

    class _BoomClient(_FakeClient):
        def publish(self, topic, data):
            raise RuntimeError('redis down mid-publish')

    broker = {}
    bus = RedisPushBus(deliver_fn=None, client=_BoomClient(broker))
    hub = _make_hub(bus)
    bus._deliver = hub._deliver_frame
    c = _SyncClient()
    hub.subscribe(c, 'paper', 't1')
    hub.push_event('paper', 't1', {'type': 'progress'})  # publish raises
    assert len(c.frames) == 1, 'fail-open: local delivery must still happen'


# ══════════════════════════════════════════════════════════════════════
#  Subscription-registry lease lifecycle (Epic B B.5.1 — acquire/release/refresh)
# ══════════════════════════════════════════════════════════════════════
def _reset_state_store():
    import lib.runtime_state_store as rss
    rss.reset_for_test()


def test_registry_acquires_on_subscribe():
    _reset_state_store()
    import lib.runtime_state_store as rss
    h = _make_hub()
    c = _SyncClient()
    h.subscribe(c, 'paper', 't1')
    # A sub:* lease exists for this replica under (channel, task).
    assert rss.get_store().count('sub', 'paper:t1:') == 1


def test_registry_releases_on_last_unsubscribe():
    _reset_state_store()
    import lib.runtime_state_store as rss
    h = _make_hub()
    c1, c2 = _SyncClient(), _SyncClient()
    h.subscribe(c1, 'paper', 't1')
    h.subscribe(c2, 'paper', 't1')
    # Two local subscribers → one replica lease (keyed by replica, not client).
    assert rss.get_store().count('sub', 'paper:t1:') == 1
    # First unsubscribe: still a local subscriber → lease STAYS.
    h.unsubscribe(c1, 'paper', 't1')
    assert rss.get_store().count('sub', 'paper:t1:') == 1
    # Last unsubscribe: no local subscriber → lease RELEASED.
    h.unsubscribe(c2, 'paper', 't1')
    assert rss.get_store().count('sub', 'paper:t1:') == 0


def test_registry_releases_on_unregister():
    _reset_state_store()
    import lib.runtime_state_store as rss
    h = _make_hub()
    c = _SyncClient()
    h.register(c)
    h.subscribe(c, 'paper', 't1')
    assert rss.get_store().count('sub', 'paper:t1:') == 1
    h.unregister(c)  # disconnect drops the last subscriber → release
    assert rss.get_store().count('sub', 'paper:t1:') == 0


def test_NC_registry_acquire_without_release_leaks():
    """NEGATIVE CONTROL for the lease lifecycle the owner flagged: an acquire
    with NO matching release leaves the registry entry lingering after the
    client is gone. We prove the CORRECT path releases; and that skipping the
    release (simulated) leaves a stale entry — the latent bug."""
    _reset_state_store()
    import lib.runtime_state_store as rss
    h = _make_hub()
    c = _SyncClient()
    h.subscribe(c, 'paper', 't1')
    # Correct path: unsubscribe releases.
    h.unsubscribe(c, 'paper', 't1')
    assert rss.get_store().count('sub', 'paper:t1:') == 0
    # Simulate the pre-fix bug: acquire again but DON'T release → it lingers.
    h._register_subscription('paper', 't1')
    assert rss.get_store().count('sub', 'paper:t1:') == 1  # stale entry = the leak


def test_registry_refresh_keeps_living_lease_alive():
    """refresh_subscriptions re-arms a living subscriber's lease so it does not
    expire under a short TTL. We shrink the TTL and prove refresh keeps it
    live past that window while the subscriber is still connected."""
    _reset_state_store()
    import lib.runtime_state_store as rss
    import time as _t
    h = _make_hub()
    h._SUB_TTL = 0.15  # shrink for the test
    c = _SyncClient()
    h.subscribe(c, 'paper', 't1')
    t_end = _t.time() + 0.3  # 2x TTL
    while _t.time() < t_end:
        h.refresh_subscriptions()
        _t.sleep(0.05)
    assert rss.get_store().count('sub', 'paper:t1:') == 1, (
        'a refreshed living subscription lease must not expire')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
