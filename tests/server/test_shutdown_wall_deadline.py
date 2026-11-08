"""Shutdown joins are bounded by wall time, not by a serving-loop timer.

2026-11-06: a synchronous settlement blocked the execution host's event loop
for ~18 s while its background services stopped in time; the asyncio deadline
then fired on the first iteration afterwards and failed the whole drain.
"""
import asyncio
import threading
import time

import pytest

from lib.server.server_shutdown import _call_within_wall_deadline

pytestmark = pytest.mark.unit


def _finish_while_the_loop_is_blocked(join):
    def stop():
        time.sleep(0.2)
        return 'stopped'

    async def scenario():
        waiting = asyncio.ensure_future(join(stop))
        await asyncio.sleep(0.05)
        # A synchronous callback holds the loop while the stop finishes (0.25 s)
        # and past the 0.5 s deadline.
        time.sleep(1.0)
        return await waiting

    return asyncio.run(scenario())


def test_blocked_serving_loop_cannot_expire_a_join_that_finished_in_time():
    # asyncio.wait_for(asyncio.to_thread(stop), timeout=0.5) raises a bare
    # TimeoutError here on CPython 3.12 although the stop finished at 0.25 s.
    assert _finish_while_the_loop_is_blocked(
        lambda stop: asyncio.to_thread(_call_within_wall_deadline, 0.5, stop)) == 'stopped'


def test_a_join_that_outlives_its_deadline_times_out_and_keeps_running():
    release = threading.Event()
    finished = threading.Event()

    def hang():
        release.wait(5)
        finished.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _call_within_wall_deadline(0.1, hang)
    assert time.monotonic() - started < 2
    assert not finished.is_set()
    release.set()
    assert finished.wait(5)


def test_the_joined_call_returns_its_result_and_raises_its_failure():
    assert _call_within_wall_deadline(5, lambda value, *, scale: value * scale, 3, scale=2) == 6

    def fail():
        raise RuntimeError('owned work has not quiesced')

    with pytest.raises(RuntimeError, match='not quiesced'):
        _call_within_wall_deadline(5, fail)
