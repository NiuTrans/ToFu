"""A dead WebSocket half must not strand its sibling or hub membership."""

import asyncio

import pytest

pytestmark = pytest.mark.unit


def test_first_completed_socket_half_cancels_and_drains_sibling():
    from routes.push import _join_socket_halves

    async def scenario():
        sibling_cancelled = asyncio.Event()

        async def ended():
            await asyncio.sleep(0)

        async def stranded_receiver():
            try:
                await asyncio.Future()
            finally:
                sibling_cancelled.set()

        first = asyncio.create_task(ended())
        sibling = asyncio.create_task(stranded_receiver())
        await asyncio.wait_for(
            _join_socket_halves(first, sibling), timeout=1)
        return sibling, sibling_cancelled.is_set()

    sibling, cancelled = asyncio.run(scenario())
    assert sibling.cancelled()
    assert cancelled


def test_push_client_drain_cancellation_reaps_queue_getter():
    """Cancelling a socket sender must not orphan its Queue.get task."""
    from lib.agent_core.push import PushClient

    async def scenario():
        client = PushClient(user_id=1)
        baseline = asyncio.all_tasks()
        drain_task = asyncio.create_task(client.drain())
        await asyncio.sleep(0)
        spawned = asyncio.all_tasks() - baseline - {drain_task}
        assert len(spawned) == 1, 'drain must be waiting through one queue getter'

        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)
        await asyncio.sleep(0)
        return [task for task in spawned if not task.done()]

    assert asyncio.run(scenario()) == []


def test_push_client_control_wakeup_reaps_queue_getter():
    """The priority control lane must also drain the losing data waiter."""
    from lib.agent_core.push import PushClient

    async def scenario():
        client = PushClient(user_id=1)
        baseline = asyncio.all_tasks()
        drain_task = asyncio.create_task(client.drain())
        await asyncio.sleep(0)
        spawned = asyncio.all_tasks() - baseline - {drain_task}
        assert len(spawned) == 1, 'drain must be waiting through one queue getter'

        client.enqueue_control({'channel': 'system', 'type': 'pong'})
        frame = await asyncio.wait_for(drain_task, timeout=1)
        await asyncio.sleep(0)
        return frame, [task for task in spawned if not task.done()]

    frame, pending = asyncio.run(scenario())
    assert frame == {'channel': 'system', 'type': 'pong'}
    assert pending == []
