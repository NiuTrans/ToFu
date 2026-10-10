"""Demand connection ownership preserves per-owner routing and eager release."""
import asyncio
import gc
import weakref

import pytest

from lib.agent_core.push import PushClient, PushHub

pytestmark = pytest.mark.unit


def test_large_connection_set_routes_only_to_authenticated_owner(monkeypatch):
    hub = PushHub()
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    owners = [PushClient(user_id=1) for _ in range(513)]
    foreign = [PushClient(user_id=index + 2) for index in range(513)]
    clients = owners + foreign
    try:
        for client in clients:
            assert hub.register(client)
            hub.subscribe(client, 'chat', 'fixture-task')
        hub._deliver_frame({'channel':'chat', 'taskId':'fixture-task',
                            '_ownerUserId':'1', 'type':'delta', 'text':'fixture'})
        async def consume():
            for client in owners:
                frame = await client.drain()
                assert frame['text'] == 'fixture'
                assert '_ownerUserId' not in frame
                assert client.event_retained_bytes == 0
        asyncio.run(consume())
        assert all(client._queue.empty() for client in foreign)
        assert hub.close_clients_for_shutdown() == 1026
        assert not hub.register(PushClient(user_id=1))
    finally:
        for client in clients:
            hub.unregister(client)
            client.disconnect()
    assert hub.client_count == 0
    assert not hub._subscriptions
    assert hub.bus_health()['event_retained_bytes'] == 0


def test_unsubscribed_registration_is_weak_and_requires_no_expiry():
    hub = PushHub()
    client = PushClient(user_id=9)
    reference = weakref.ref(client)
    assert hub.register(client)
    del client
    gc.collect()
    assert reference() is None
    assert hub.client_count == 0
