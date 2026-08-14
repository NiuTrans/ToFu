"""Lifecycle and rollback contracts for the extracted LoopWatch owner."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lib.server_loop_watchdog import LoopWatchdog


pytestmark = pytest.mark.unit


class _Hooks:
    heartbeats = 0

    @staticmethod
    def _should_arm_ctimer(_threshold, _sink):
        return False

    @staticmethod
    def _fault_dump_limits():
        return {'active_bytes': 1024}

    @classmethod
    def _write_heartbeat(cls):
        cls.heartbeats += 1

    @staticmethod
    def _port_bound(_port, _host):
        return True

    @staticmethod
    def _listener_death_decide(_was_bound, _bound, _misses, _limit):
        return True, 0, False

    @staticmethod
    def _loop_stall_decide(_age, _threshold, latched):
        return False, latched

    @staticmethod
    def _extract_loop_top_frame(_frame):
        return ''

    @staticmethod
    def _stall_pressure_context():
        return ''

    @staticmethod
    def _trim_fault_sink_if_oversize(*_args, **_kwargs):
        return False

    @staticmethod
    def _reset_fault_sink(*_args, **_kwargs):
        return False


class _CountingHooks(_Hooks):
    stall_decisions = 0

    @classmethod
    def _loop_stall_decide(cls, _age, _threshold, latched):
        cls.stall_decisions += 1
        return False, latched


def test_stop_wakes_watch_thread_without_process_shutdown_signal():
    async def exercise():
        shutdown_requested = threading.Event()
        runtime = LoopWatchdog(
            asyncio.get_running_loop(),
            shutdown_requested,
            host='127.0.0.1',
            port=15000,
            hooks=_Hooks,
            fault_shm_log=None,
            fault_log=None,
            environ={
                'TOFU_LOOP_STALL_SECS': '60',
                'TOFU_LOOP_HEARTBEAT_SECS': '60',
            },
        ).start()
        heartbeat = runtime.heartbeat_task
        watcher = runtime.watcher_thread
        await asyncio.sleep(0)

        assert shutdown_requested.is_set() is False
        assert watcher is not None and watcher.is_alive()
        assert heartbeat is not None
        assert await runtime.stop(timeout=0.5) is True
        assert await runtime.stop(timeout=0.5) is True
        assert heartbeat.cancelled()
        assert not watcher.is_alive()

    asyncio.run(exercise())


def test_disabled_watchdog_allocates_no_task_or_thread():
    async def exercise():
        runtime = LoopWatchdog(
            asyncio.get_running_loop(),
            threading.Event(),
            host='0.0.0.0',
            port=15000,
            hooks=_Hooks,
            fault_shm_log=None,
            fault_log=None,
            environ={'TOFU_LOOP_STALL_SECS': '0'},
        ).start()

        assert runtime.heartbeat_task is None
        assert runtime.watcher_thread is None
        assert await runtime.stop() is True

    asyncio.run(exercise())


def test_startup_readiness_gate_suppresses_boot_stall_observation():
    async def exercise():
        _CountingHooks.stall_decisions = 0
        ready = threading.Event()
        runtime = LoopWatchdog(
            asyncio.get_running_loop(),
            threading.Event(),
            host='127.0.0.1',
            port=15000,
            hooks=_CountingHooks,
            fault_shm_log=None,
            fault_log=None,
            ready_event=ready,
            environ={
                'TOFU_LOOP_STALL_SECS': '0.1',
                'TOFU_LOOP_HEARTBEAT_SECS': '0.05',
            },
        ).start()
        try:
            # Deliberately block the loop longer than the threshold while the
            # application is still in Quart startup. No stall decision/audit.
            time.sleep(0.7)
            assert _CountingHooks.stall_decisions == 0

            ready.set()
            await asyncio.sleep(0.1)
            # The same blockage after readiness is observable again.
            time.sleep(1.2)
            assert _CountingHooks.stall_decisions >= 1
        finally:
            await runtime.stop(timeout=1.0)

    import time
    asyncio.run(exercise())


def test_production_entry_registers_watchdog_shutdown_owner():
    source = (Path(__file__).resolve().parents[1]
              / 'lib/serving_loop_lifecycle.py').read_text()

    assert 'LoopWatchdog(' in source
    assert 'ready_event=gate' in source
    assert 'await watchdog.stop(timeout=2.0)' in source
    assert "name='tofu.serving-loop.shutdown'" in source
    assert 'async def _loop_heartbeat_task' not in source
    assert 'def _loop_stall_watch' not in source
