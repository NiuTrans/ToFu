"""Restart-loop circuit breaker: crash storms enter a terminal degraded state.

A single crash restarts fast; a storm of crashes or failed starts inside the
sliding window (TOFU_STORAGE_RESTART_CRASH_LIMIT per
TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS) means the authority cannot
currently stay up. The runtime then stops spawning, stays fenced, surfaces
``degraded`` in status(), logs CRITICAL once plus a periodic reminder, and
requires a process restart to recover — never a silent backend switch.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
import lib.storage.runtime as runtime_mod


pytestmark = pytest.mark.unit


class _ScriptedSupervisor:
    """Supervisor double: start() follows a fail/succeed script."""

    def __init__(self, *, always_fail=False):
        self.callback = None
        self.always_fail = always_fail
        self.starts = 0
        self.ready = False
        self.client = self

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.starts += 1
        if self.always_fail:
            raise RuntimeError('spawn refused')
        self.ready = True
        self.client = self
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {
            'ready': self.ready,
            'state': 'ready' if self.ready else 'exited',
            'backend': 'sqlite',
            'pid': None,
            'port': None,
            'last_exit_code': -9,
        }


@pytest.fixture
def _breaker_env(monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '3')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '600')
    yield


def test_single_crash_restarts_quickly(_breaker_env):
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)
    thread = runtime._restart_thread
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert supervisor.starts == 1  # fast first restart, no breaker delay
    assert runtime.ready
    status = runtime.status()
    assert status['state'] == 'ready'
    assert status['degraded'] is False
    runtime.stop(timeout=5.0)


def test_crash_storm_trips_degraded_and_stops_spawning(_breaker_env, caplog):
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        for _ in range(4):  # 4 crashes > limit 3 → degraded
            runtime._crashed(-9)
    thread = runtime._restart_thread
    if thread is not None:
        thread.join(timeout=10)

    status = runtime.status()
    assert status['state'] == 'degraded'
    assert status['degraded'] is True
    assert runtime.ready is False
    # The breaker tripped before any spawn: one crash scheduled a restart,
    # but the storm outran it and degraded state forbids further spawns.
    assert supervisor.starts <= 1
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1  # CRITICAL exactly once at entry
    assert 'circuit breaker tripped' in criticals[0].getMessage()
    assert 'process restart' in criticals[0].getMessage()
    # Reads and writes stay fenced with the typed availability error.
    with pytest.raises(StorageError) as excinfo:
        runtime.client(write=True)
    assert excinfo.value.code == 'database_unavailable'
    # Explicit start attempts fail closed too — no un-fence surface exists.
    with pytest.raises(StorageError):
        runtime.start()
    runtime.stop(timeout=5.0)


def test_failed_start_storm_also_trips_the_breaker(_breaker_env, caplog):
    supervisor = _ScriptedSupervisor(always_fail=True)
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        runtime._crashed(-9)  # one crash; the failed attempts do the rest
        thread = runtime._restart_thread
        assert thread is not None
        thread.join(timeout=30)
    assert not thread.is_alive()

    status = runtime.status()
    assert status['state'] == 'degraded'
    assert status['degraded'] is True
    starts_at_verdict = supervisor.starts
    assert starts_at_verdict >= 3  # the failed attempts fed the window
    # No further spawns after the verdict.
    threading.Event().wait(0.5)
    assert supervisor.starts == starts_at_verdict
    runtime.stop(timeout=5.0)


def test_degraded_reminder_heartbeats_until_stop(_breaker_env, monkeypatch,
                                                 caplog):
    monkeypatch.setattr(runtime_mod, '_DEGRADED_REMINDER_INTERVAL_S', 0.05)
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    with caplog.at_level(logging.WARNING, logger='tofu.storage.runtime'):
        for _ in range(4):
            runtime._crashed(-9)
        thread = runtime._restart_thread
        if thread is not None:
            thread.join(timeout=10)
        assert runtime.status()['degraded'] is True
        reminder = runtime._degraded_reminder_thread
        assert reminder is not None
        # Wait for at least one heartbeat reminder.
        deadline = threading.Event()
        deadline.wait(0.4)
    reminders = [r for r in caplog.records
                 if 'remains degraded' in r.getMessage()]
    assert reminders, [r.getMessage() for r in caplog.records]
    runtime.stop(timeout=5.0)
    reminder.join(timeout=5)
    assert not reminder.is_alive()


def test_client_waits_out_bounded_restart(_breaker_env):
    """A caller arriving mid-restart rides the window instead of 503ing.

    The 2026-09-24 incident: every in-flight request failed fast with
    database_unavailable during the ~10 s sidecar recycle even though the
    caller RPC deadline (20 s) would have carried it past the restart. The
    fence now parks the caller on readiness while a restart worker is
    alive, and the successful handshake releases it to a ready client.
    """
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)  # schedules the restart worker immediately
    result: dict[str, object] = {}

    def _blocked_caller():
        try:
            result['client'] = runtime.client(write=True)
        except StorageError as exc:
            result['error'] = exc

    caller = threading.Thread(target=_blocked_caller, daemon=True)
    caller.start()
    caller.join(timeout=10)
    assert not caller.is_alive()
    assert 'error' not in result, result.get('error')
    assert result.get('client') is supervisor
    assert runtime.ready
    runtime.stop(timeout=5.0)


def test_client_fails_fast_when_no_restart_in_flight(_breaker_env):
    """Not-ready without a live restart worker keeps fail-fast semantics.

    Degraded/stopped/never-started states must never park a request thread:
    waiting there hides a real outage behind handler latency. Only the
    bounded restart window waits.
    """
    supervisor = _ScriptedSupervisor()
    runtime = StorageRuntime(supervisor)
    # Never started, no restart scheduled: the fence raises immediately.
    started = runtime_mod.time.monotonic()
    with pytest.raises(StorageError) as excinfo:
        runtime.client(write=True)
    assert excinfo.value.code == 'database_unavailable'
    assert runtime_mod.time.monotonic() - started < 1.0
    runtime.stop(timeout=5.0)


def test_client_wait_releases_on_degraded_verdict(_breaker_env):
    """A parked caller wakes on the breaker verdict, not the poll cap.

    If the restart storm trips degraded mid-wait the caller must get the
    classified fence error promptly rather than sleeping the whole wait
    cap behind a terminal state that will never become ready.
    """
    supervisor = _ScriptedSupervisor(always_fail=True)
    runtime = StorageRuntime(supervisor)
    runtime._crashed(-9)  # failed starts feed the breaker storm
    result: dict[str, object] = {}

    def _blocked_caller():
        try:
            runtime.client(write=True)
        except StorageError as exc:
            result['error'] = exc

    caller = threading.Thread(target=_blocked_caller, daemon=True)
    started = runtime_mod.time.monotonic()
    caller.start()
    caller.join(timeout=30)
    assert not caller.is_alive()
    assert isinstance(result.get('error'), StorageError)
    assert result['error'].code == 'database_unavailable'
    # Well under the 30 s fence-wait cap: the degraded notify released it.
    assert runtime_mod.time.monotonic() - started < 25.0
    runtime.stop(timeout=5.0)

def test_breaker_knob_parsing_and_clamps(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', raising=False)
    monkeypatch.delenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS',
                       raising=False)
    assert runtime_mod._restart_breaker_config() == (600.0, 5)
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '0')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '10')
    window, limit = runtime_mod._restart_breaker_config()
    assert (window, limit) == (60.0, 1)  # clamped into hard bounds
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', 'bogus')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', 'bogus')
    assert runtime_mod._restart_breaker_config() == (600.0, 5)
