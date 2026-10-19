"""Event-compatible abort observation has no lost wakes or retained observers."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from lib.agent_core.abort_signal import AbortSignal

pytestmark=pytest.mark.unit


def test_threading_event_compatibility_and_immediate_observation():
    signal=AbortSignal();assert isinstance(signal,threading.Event)
    assert not signal.wait(.001)
    signal.set();assert signal.is_set() and signal.wait(0)
    async def run():
        subscription=signal.subscribe_async();await subscription.future;subscription.close();subscription.close()
        assert signal._observers is None
    asyncio.run(run());signal.clear();assert not signal.is_set()


def test_subscription_and_set_races_never_lose_wakes():
    async def run():
        with ThreadPoolExecutor(max_workers=4) as executor:
            for _ in range(100):
                signal=AbortSignal();worker=executor.submit(signal.set)
                subscription=signal.subscribe_async()
                await asyncio.wait_for(subscription.future,1)
                worker.result();subscription.close();assert signal._observers is None
    asyncio.run(run())


def test_detached_and_closed_loop_observers_do_not_break_set():
    signal=AbortSignal()
    async def detached():
        subscription=signal.subscribe_async();subscription.close()
        assert signal._observers is None
    asyncio.run(detached());signal.set();signal.clear()
    async def abandoned():return signal.subscribe_async()
    subscription=asyncio.run(abandoned())
    signal.set();subscription.close();assert signal._observers is None


def test_multiple_observers_wake_on_their_own_loops():
    signal=AbortSignal();ready=[threading.Event(),threading.Event()];done=[]
    def observe(index):
        async def run():
            subscription=signal.subscribe_async();ready[index].set()
            await asyncio.wait_for(subscription.future,2);subscription.close();done.append(index)
        asyncio.run(run())
    threads=[threading.Thread(target=observe,args=(i,)) for i in range(2)]
    for thread in threads:thread.start()
    try:
        assert all(event.wait(1) for event in ready)
        signal.set()
    finally:
        signal.set()
        for thread in threads:thread.join(3)
    assert sorted(done)==[0,1] and signal._observers is None
