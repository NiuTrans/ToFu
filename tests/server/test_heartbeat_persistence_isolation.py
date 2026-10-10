"""Heartbeat filesystem stalls cannot freeze HTTP loop progress or fake liveness."""
import asyncio
import threading
import time

import pytest

from lib.server_loop_watchdog import LoopWatchdog
from tests.server.test_server_loop_watchdog_lifecycle import _Hooks

pytestmark = pytest.mark.unit


def test_blocked_heartbeat_write_keeps_serving_loop_ticking_and_joins_on_stop():
    entered = threading.Event()
    release = threading.Event()
    writer_threads = []

    class Hooks(_Hooks):
        @staticmethod
        def _write_heartbeat(*, ts):
            writer_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(3)
            return True

    async def exercise():
        runtime = LoopWatchdog(asyncio.get_running_loop(), threading.Event(),
            host='127.0.0.1', port=15000, hooks=Hooks,
            fault_shm_log=None, fault_log=None,
            environ={'TOFU_LOOP_STALL_SECS': '60',
                     'TOFU_LOOP_HEARTBEAT_SECS': '0.005'}).start()
        watcher = runtime.watcher_thread
        try:
            async def writer_entered():
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(writer_entered(), timeout=2)
            first_tick = runtime.heartbeat['ts']
            await asyncio.sleep(0.03)
            assert runtime.heartbeat['ts'] > first_tick
            assert writer_threads == [watcher.ident]
            assert threading.get_ident() != writer_threads[0]
        finally:
            release.set()
            assert await runtime.stop(timeout=1)
            assert not watcher.is_alive()

    asyncio.run(exercise())


def test_persisted_timestamp_tracks_last_loop_tick_instead_of_watchdog_activity():
    timestamps = []

    class Hooks(_Hooks):
        @staticmethod
        def _write_heartbeat(*, ts):
            timestamps.append(ts)
            return True

    loop = asyncio.new_event_loop()
    try:
        runtime = LoopWatchdog(loop, threading.Event(), host='127.0.0.1',
            port=15000, hooks=Hooks, fault_shm_log=None, fault_log=None)
        runtime.heartbeat['ts'] = time.monotonic() - 45
        runtime._persist_loop_heartbeat()
        runtime._persist_loop_heartbeat()
        assert len(timestamps) == 1
        assert 44 <= time.time() - timestamps[0] <= 46
        runtime.heartbeat['ts'] = time.monotonic()
        runtime._persist_loop_heartbeat()
        assert len(timestamps) == 2
        assert time.time() - timestamps[1] < 1
    finally:
        loop.close()


def test_failed_heartbeat_persistence_retries_the_same_observed_tick():
    from unittest.mock import Mock

    loop = asyncio.new_event_loop()
    try:
        writer = Mock(side_effect=[False, True])
        class Hooks(_Hooks):
            _write_heartbeat = staticmethod(writer)
        runtime = LoopWatchdog(loop, threading.Event(), host='127.0.0.1',
            port=15000, hooks=Hooks, fault_shm_log=None, fault_log=None)
        runtime._persist_loop_heartbeat()
        runtime._persist_loop_heartbeat()
        assert writer.call_count == 2
    finally:
        loop.close()
