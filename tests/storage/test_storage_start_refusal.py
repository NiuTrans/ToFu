"""Start refusals and attachments never latch storage behind a live engine.

2026-10-27 06:58-12:21: the application's attachment to the resident native
engine detached after three slow probes, then every re-attachment was refused
with the same deterministic admission error. Five refusals inside three
seconds counted as crash pressure, the restart breaker latched ``degraded``,
and storage stayed fenced for 5 h 23 min while the engine was alive and
healthy. These specifications pin the replacement contract: a deterministic
refusal is typed, visible and rechecked slowly without crash pressure; an
attachment spawns nothing and is therefore never latched; a managed engine
that keeps crashing still trips the breaker.
"""
from __future__ import annotations

import threading
import time

import pytest

import lib.storage.runtime as runtime_mod
from lib.storage.errors import StorageError, StorageStartRefused
from lib.storage.runtime import StorageRuntime


pytestmark = pytest.mark.unit


class _AttachmentSupervisor:
    """Supervisor double whose start() follows a script of outcomes."""

    def __init__(self, script, *, owns_engine_process):
        self.owns_engine_process = owns_engine_process
        self.script = list(script)
        self.callback = None
        self.ready = False
        self.client = self
        self.starts = []

    def add_crash_callback(self, callback):
        self.callback = callback

    def start(self):
        self.starts.append(time.monotonic())
        outcome = self.script.pop(0) if self.script else 'ready'
        if isinstance(outcome, BaseException):
            raise outcome
        self.ready = True
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0):
        self.ready = False

    def status(self):
        return {'ready': self.ready, 'state': 'ready' if self.ready else 'exited',
                'backend': 'tofudb', 'pid': None, 'port': None, 'last_exit_code': -1}


def _identity_refusal():
    return StorageStartRefused(
        'engine_identity',
        'native storage service engine upgrade requires a drained replacement',
        'restart the server so the native storage service is admitted again')


@pytest.fixture
def tight_breaker(monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '2')
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS', '600')


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_outage_replay_refusals_never_latch_and_recover_when_cleared(
        tight_breaker, monkeypatch, caplog):
    monkeypatch.setattr(runtime_mod, '_REFUSAL_RECHECK_SECONDS', 0.05)
    supervisor = _AttachmentSupervisor(
        [_identity_refusal() for _ in range(6)], owns_engine_process=False)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-1)  # the external monitor detached
        assert _wait(lambda: runtime.status()['state'] == 'refused')
        status = runtime.status()
        assert status['degraded'] is False
        assert status['refusal']['reason'] == 'engine_identity'
        assert 'restart the server' in status['refusal']['remedy']
        assert _wait(lambda: runtime.ready)
        assert runtime.status()['refusal'] is None
        assert runtime.status()['degraded'] is False
        assert len(supervisor.starts) == 7
        critical = [record for record in caplog.records
                    if record.levelname == 'CRITICAL' and 'refused' in record.getMessage()]
        assert len(critical) == 1  # one alert per distinct refusal, not per recheck
    finally:
        runtime.stop(timeout=5.0)


def test_a_refusal_is_rechecked_at_the_slow_cadence_not_in_a_tight_loop(tight_breaker):
    supervisor = _AttachmentSupervisor(
        [_identity_refusal() for _ in range(100)], owns_engine_process=True)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-9)
        assert _wait(lambda: supervisor.starts)
        time.sleep(1.0)
        # One attempt, then the 30 s recheck; never the 0.1 s doubling loop.
        assert len(supervisor.starts) == 1
        assert runtime.status()['state'] == 'refused'
        assert runtime.status()['degraded'] is False
    finally:
        runtime.stop(timeout=5.0)


def test_a_managed_refusal_is_not_crash_pressure(tight_breaker, monkeypatch):
    monkeypatch.setattr(runtime_mod, '_REFUSAL_RECHECK_SECONDS', 0.01)
    supervisor = _AttachmentSupervisor(
        [_identity_refusal() for _ in range(20)], owns_engine_process=True)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-9)
        assert _wait(lambda: runtime.ready)
        assert len(supervisor.starts) == 21
        assert runtime.status()['degraded'] is False
    finally:
        runtime.stop(timeout=5.0)


def test_an_attachment_never_latches_degraded(tight_breaker, monkeypatch):
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.02)
    failures = [RuntimeError('external storage sidecar startup timed out') for _ in range(8)]
    supervisor = _AttachmentSupervisor(failures, owns_engine_process=False)
    runtime = StorageRuntime(supervisor)
    try:
        for _ in range(3):
            runtime._crashed(-1)
        assert _wait(lambda: runtime.ready)
        status = runtime.status()
        assert status['degraded'] is False
        assert len(supervisor.starts) == 9
    finally:
        runtime.stop(timeout=5.0)


def test_a_managed_crash_storm_still_trips_the_breaker(tight_breaker):
    failures = [RuntimeError('spawn refused') for _ in range(10)]
    supervisor = _AttachmentSupervisor(failures, owns_engine_process=True)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-9)
        assert _wait(lambda: runtime.status()['degraded'])
        assert runtime.status()['state'] == 'degraded'
        with pytest.raises(StorageError):
            runtime.client()
    finally:
        runtime.stop(timeout=5.0)


def _assert_refusal_error(error):
    assert error.code == 'database_unavailable'
    assert error.retryable is False
    assert 'engine_identity' in error.message
    assert 'restart the server' in error.message
    # The refusal detail may name host paths; callers get reason and remedy.
    assert 'drained replacement' not in error.message


def test_a_refused_runtime_fails_every_caller_fast_with_the_remedy(tight_breaker):
    supervisor = _AttachmentSupervisor(
        [_identity_refusal() for _ in range(100)], owns_engine_process=False)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-1)
        assert _wait(lambda: runtime.status()['state'] == 'refused')
        outcomes = []

        def call(write):
            started = time.monotonic()
            try:
                runtime.client(write=write)
            except StorageError as error:
                outcomes.append((time.monotonic() - started, error))

        callers = [threading.Thread(target=call, args=(index % 2 == 0,))
                   for index in range(4)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=5.0)
        assert len(outcomes) == 4
        for elapsed, error in outcomes:
            # Not the fence-wait cap: the next recheck is 30 s away.
            assert elapsed < 2.0
            _assert_refusal_error(error)
    finally:
        runtime.stop(timeout=5.0)


def test_a_refusal_wakes_callers_already_waiting_on_the_restart(tight_breaker):
    gate = threading.Event()

    class _HeldStart(_AttachmentSupervisor):
        def start(self):
            gate.wait(5)
            return super().start()

    supervisor = _HeldStart(
        [_identity_refusal() for _ in range(100)], owns_engine_process=False)
    runtime = StorageRuntime(supervisor)
    outcome = []
    finished = threading.Event()

    def call():
        try:
            runtime.client()
        except StorageError as error:
            outcome.append(error)
        finished.set()

    try:
        runtime._crashed(-1)
        assert _wait(lambda: runtime.status()['restarting'])
        caller = threading.Thread(target=call)
        caller.start()
        assert not finished.wait(0.3)  # a live restart attempt is worth waiting for
        released = time.monotonic()
        gate.set()
        assert finished.wait(5.0)
        assert time.monotonic() - released < 2.0
        _assert_refusal_error(outcome[0])
    finally:
        gate.set()
        runtime.stop(timeout=5.0)


def test_a_different_failure_supersedes_a_refusal(tight_breaker, monkeypatch):
    monkeypatch.setattr(runtime_mod, '_REFUSAL_RECHECK_SECONDS', 0.01)
    monkeypatch.setattr(runtime_mod, '_RESTART_MAX_DELAY_SECONDS', 0.02)
    gate = threading.Event()
    script = [_identity_refusal(), RuntimeError('external storage sidecar startup timed out')]

    class _Gated(_AttachmentSupervisor):
        def start(self):
            if len(self.starts) == 2:
                gate.wait(5)
            return super().start()

    supervisor = _Gated(script, owns_engine_process=False)
    runtime = StorageRuntime(supervisor)
    try:
        runtime._crashed(-1)
        assert _wait(lambda: len(supervisor.starts) == 2)
        assert _wait(lambda: runtime.status()['refusal'] is None)
        assert runtime.status()['state'] != 'refused'
        gate.set()
        assert _wait(lambda: runtime.ready)
    finally:
        gate.set()
        runtime.stop(timeout=5.0)
