"""Cancellation cannot abandon or duplicate a coroutine-backed native cleanup."""
import asyncio

import pytest
from lib.agent_core.native_process import _joined

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('prestarted', [False, True])
def test_join_starts_one_cleanup_and_survives_repeated_cancellation(prestarted):
    async def exercise():
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        starts = []
        token = object()
        async def cleanup():
            starts.append(True)
            entered.set()
            await release.wait()
            completed.set()
            return token
        owned = asyncio.create_task(cleanup()) if prestarted else cleanup()
        joining = asyncio.create_task(_joined(owned))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(3):
                joining.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                if joining.done():
                    joining.result()  # Surface the primary cleanup failure.
                assert not completed.is_set() and not joining.done()
            release.set()
            assert await asyncio.wait_for(joining, 1) is token
            assert starts == [True] and completed.is_set()
        finally:
            release.set()
            await asyncio.gather(joining, return_exceptions=True)
    asyncio.run(exercise())
