"""Committed wake hints stay bounded even before the observer loop runs.

The durable log remains authority; a pending hint covers every commit until
the reader consumes it and probes that log again.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.conversation_sync.broker import ConversationWakeBroker

pytestmark = pytest.mark.unit


def test_blocked_observer_coalesces_concurrent_commits_before_scheduling(monkeypatch):
    async def scenario():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        subscription = broker.subscribe(1, "wake-budget")
        loop = asyncio.get_running_loop()
        schedule = loop.call_soon_threadsafe
        scheduled = []

        def counted_schedule(callback, *args, **kwargs):
            scheduled.append(callback)
            return schedule(callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_soon_threadsafe", counted_schedule)
        try:
            # Deliberately keep the observer loop from running while producers
            # commit. A bound applied only inside that loop is too late.
            with ThreadPoolExecutor(max_workers=8) as producers:
                list(producers.map(lambda _: subscription.wake(), range(10_000)))
            assert len(scheduled) == 1
            await asyncio.sleep(0)
            # The queued token must also suppress additional scheduling until
            # consumed, even when the loop can now run.
            for _ in range(10_000):
                subscription.wake()
            assert len(scheduled) == 1
            assert await subscription.wait(0.2)
            subscription.wake()
            assert len(scheduled) == 2
            assert await subscription.wait(0.2)
            assert not await subscription.wait(0.001)
        finally:
            subscription.close()
        assert broker.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_close_interrupts_wait_with_a_pending_commit_and_releases_once():
    async def scenario():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        subscription = broker.subscribe(1, "wake-close")
        released = []
        subscription.add_close_callback(lambda: released.append("released"))
        subscription.wake()
        assert subscription.close("observer-gone")
        assert not subscription.close()
        for _ in range(100):
            subscription.wake()
        assert not await subscription.wait(0.2)
        await asyncio.sleep(0)
        assert released == ["released"]
        assert broker.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_wake_after_heartbeat_timeout_remains_deliverable():
    async def scenario():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        subscription = broker.subscribe(1, "wake-timeout")
        try:
            assert not await subscription.wait(0.001)
            subscription.wake()
            assert await subscription.wait(0.2)
            assert not await subscription.wait(0.001)
            subscription.wake()
            assert await subscription.wait(0.2)
        finally:
            subscription.close()

    asyncio.run(scenario())


def test_cancelled_observer_wait_does_not_poison_future_wakes():
    async def scenario():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        subscription = broker.subscribe(1, "wake-cancel")
        try:
            for _ in range(20):
                waiting = asyncio.create_task(subscription.wait(30))
                await asyncio.sleep(0)
                subscription.wake()
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
                # A cancelled wait leaves its undelivered hint intact. The
                # next reader consumes it, then new commits must wake again.
                assert await subscription.wait(0.2)
                subscription.wake()
                assert await subscription.wait(0.2)
        finally:
            subscription.close()

    asyncio.run(scenario())
