"""Lifecycle and rollback contracts for the extracted LoopWatch owner."""

from __future__ import annotations

import asyncio
import errno
import faulthandler
import io
import time
import threading
from contextlib import nullcontext

import pytest

from lib.server.server_loop_watchdog import LoopWatchdog


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
    def _write_heartbeat(cls, **kwargs):
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


@pytest.mark.parametrize('local_errno', [
    errno.EADDRNOTAVAIL, errno.EADDRINUSE, errno.EMFILE, errno.ENFILE,
    errno.ENOBUFS, errno.ENOMEM,
])
@pytest.mark.parametrize('initial_success', [True, False])
def test_probe_resource_failure_does_not_exit_a_previously_live_listener(
        monkeypatch, local_errno, initial_success):
    """Inject an undispatched socket failure into the real heartbeat owner."""
    import socket
    from lib.server import server_fault_watchdog as decisions

    async def exercise():
        observations = []; attempts = [0]
        shutdown = threading.Event()

        def connect(*_args, **_kwargs):
            attempts[0] += 1
            if initial_success and attempts[0] == 1:
                return nullcontext()  # One proven connection before pressure.
            raise OSError(local_errno, 'injected local probe resource failure')

        class Hooks(_Hooks):
            _port_bound = staticmethod(decisions._port_bound)

            @staticmethod
            def _listener_death_decide(*args):
                answer = decisions._listener_death_decide(*args)
                observations.append((args, answer))
                return answer

        monkeypatch.setattr(socket, 'create_connection', connect)
        runtime = LoopWatchdog(asyncio.get_running_loop(), shutdown,
            host='127.0.0.1', port=15000, hooks=Hooks,
            fault_shm_log=None, fault_log=None,
            environ={'TOFU_LOOP_STALL_SECS':'60',
                     'TOFU_LOOP_HEARTBEAT_SECS':'0.005'}).start()
        try:
            async def observed():
                while len(observations) < 8:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(observed(), timeout=2)
            assert not shutdown.is_set()
            unknown = observations[1:] if initial_success else observations
            assert all(args[1] is None for args, _ in unknown)
            assert runtime.listen_state == {
                'was_bound':initial_success, 'misses':0}
            assert runtime.heartbeat_task is not None
            assert not runtime.heartbeat_task.done()
        finally:
            watcher = runtime.watcher_thread
            assert await runtime.stop(timeout=1)
            assert watcher is not None and not watcher.is_alive()

    asyncio.run(exercise())


@pytest.mark.parametrize('error', [
    ConnectionRefusedError(errno.ECONNREFUSED, 'listener closed'),
    TimeoutError('listener connection timed out'),
])
def test_listener_outages_recover_without_exiting_or_flooding_diagnostics(
        monkeypatch, caplog, error):
    import socket
    from lib.server import server_fault_watchdog as decisions

    async def exercise():
        attempts = [0]; shutdown = threading.Event()
        def connect(*_args, **_kwargs):
            attempts[0] += 1
            if attempts[0] not in (*range(2, 10), *range(11, 19)):
                return nullcontext()
            raise error
        class Hooks(_Hooks):
            _port_bound = staticmethod(decisions._port_bound)
            _listener_death_decide = staticmethod(decisions._listener_death_decide)
        monkeypatch.setattr(socket, 'create_connection', connect)
        runtime = LoopWatchdog(asyncio.get_running_loop(), shutdown,
            host='127.0.0.1', port=15000, hooks=Hooks,
            fault_shm_log=None, fault_log=None,
            environ={'TOFU_LOOP_STALL_SECS':'60',
                     'TOFU_LOOP_HEARTBEAT_SECS':'0.005'}).start()
        try:
            async def recovered():
                while attempts[0] < 20: await asyncio.sleep(0.005)
            with caplog.at_level('INFO', logger='lib.server.server_loop_watchdog'):
                await asyncio.wait_for(recovered(), timeout=2)
            assert not shutdown.is_set()
            assert runtime.heartbeat_task is not None
            assert not runtime.heartbeat_task.done()
            assert runtime.listen_state == {'was_bound': True, 'misses': 0}
            messages = [record.getMessage() for record in caplog.records
                        if record.name == 'lib.server.server_loop_watchdog']
            assert sum('preserving tasks' in message for message in messages) == 2
            assert sum('recovered in place' in message for message in messages) == 2
        finally:
            assert await runtime.stop(timeout=1)
    asyncio.run(exercise())


@pytest.mark.parametrize('was_bound,misses', [(False,0),(True,0),(True,4)])
def test_unobserved_listener_does_not_arm_or_complete_a_loss_streak(
        was_bound, misses):
    from lib.server.server_fault_watchdog import _listener_death_decide
    assert _listener_death_decide(was_bound, None, misses, 5) == (
        was_bound, 0, False)


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


def test_stall_diagnostics_never_traverse_raw_native_frames(monkeypatch):
    """A real blocked loop yields Python snapshots without arming a C timer."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError('raw native thread traversal is unsafe')
    monkeypatch.setattr(faulthandler, 'dump_traceback_later', forbidden)
    monkeypatch.setattr(faulthandler, 'dump_traceback', forbidden)

    class Hooks(_Hooks):
        @staticmethod
        def _loop_stall_decide(age, threshold, latched):
            return age > threshold and not latched, age > threshold
        @staticmethod
        def _fault_dump_limits():
            return {'active_bytes': 128 * 1024}
        @staticmethod
        def _reset_fault_sink(sink, **_kwargs):
            sink.seek(0)
            sink.truncate()

    async def exercise():
        durable, mirror = io.StringIO(), io.StringIO()
        runtime = LoopWatchdog(asyncio.get_running_loop(), threading.Event(),
            host='127.0.0.1', port=15000, hooks=Hooks,
            fault_shm_log=mirror, fault_log=durable,
            environ={'TOFU_LOOP_STALL_SECS': '0.1',
                     'TOFU_LOOP_HEARTBEAT_SECS': '0.05'}).start()
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.8)
            assert 'LOOP STALL' in mirror.getvalue()
            assert 'exercise' in mirror.getvalue()
            assert durable.getvalue() == mirror.getvalue()
            assert len(mirror.getvalue().encode()) <= 128 * 1024
        finally:
            assert await runtime.stop(timeout=1)
    asyncio.run(exercise())
