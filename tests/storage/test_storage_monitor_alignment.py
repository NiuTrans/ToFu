"""Consecutive-failure threshold alignment across all three health monitors.

The managed-daemon monitor always required ``_liveness_failure_threshold``
consecutive failed probes before recycling a child; the embedded and
external co-container monitors fenced on a SINGLE failed probe, so one
saturated probe window could restart a healthy authority. All three now
share the ``_liveness_probe_config`` semantics, keeping each monitor's
distinct recovery action (embedded: full in-process restart; external:
detach/reattach). The embedded monitor additionally survives a wedged FFI
probe: the probe runs in a bounded worker, and a wedge fences the runtime
through the same crash-callback path a driver failure takes.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.supervisor import StorageSupervisor
import lib.storage.supervisor as supervisor_mod


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


def _wired_supervisor(*, interval=0.01, deadline=0.01, threshold=3, unresponsive=0.0):
    supervisor = StorageSupervisor()
    supervisor._liveness_interval_seconds = interval
    supervisor._liveness_probe_deadline_seconds = deadline
    supervisor._liveness_failure_threshold = threshold
    # Count-threshold mechanics by default; budget specs pass their own.
    supervisor._liveness_unresponsive_seconds = unresponsive
    return supervisor


class _ScriptedHealthClient:
    """Health answers follow a script: 'ready', 'unready', 'raise', 'hang'."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self._release = threading.Event()
        self.closed = False

    def health(self, deadline=None):
        self.calls += 1
        step = self._script.pop(0) if self._script else 'ready'
        if step == 'raise':
            raise StorageError('database_unavailable', 'transient')
        if step == 'hang':
            self._release.wait(30)
            return {'ready': True, 'backend': 'tofudb'}
        return {'ready': step == 'ready', 'backend': 'tofudb'}

    def close(self):
        self.closed = True

    def release_hang(self):
        self._release.set()


def _run_embedded_monitor(supervisor, client):
    thread = threading.Thread(
        target=supervisor._monitor_embedded, args=(client,), daemon=True)
    thread.start()
    return thread


def _run_external_monitor(supervisor, client):
    thread = threading.Thread(
        target=supervisor._monitor_external_sidecar, args=(client,),
        daemon=True)
    thread.start()
    return thread


# ── Embedded monitor threshold semantics ─────────────────────────────────


def test_embedded_single_transient_failure_does_not_fence():
    supervisor = _wired_supervisor(threshold=3)
    client = _ScriptedHealthClient(['raise', 'unready'] + ['ready'] * 50)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client
    supervisor._embedded_runtime = object()
    supervisor._embedded_lease = None

    thread = _run_embedded_monitor(supervisor, client)
    # Let several probes run; the monitor must keep going past the two
    # transient failures and must NOT fence below the threshold.
    deadline = threading.Event()
    assert not deadline.wait(0.5)
    assert crashes == []
    assert supervisor._client is client
    supervisor._embedded_monitor_stop.set()
    thread.join(timeout=5)
    assert crashes == []


def test_embedded_threshold_failures_fence_through_crash_path(caplog):
    supervisor = _wired_supervisor(threshold=3)
    client = _ScriptedHealthClient(['raise'] * 10)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client
    runtime_closed = []

    class _Runtime:
        def close(self):
            runtime_closed.append(True)

    lease_released = []

    class _Lease:
        def release(self):
            lease_released.append(True)

    supervisor._embedded_runtime = _Runtime()
    supervisor._embedded_lease = _Lease()

    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        thread = _run_embedded_monitor(supervisor, client)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert crashes == [-1]
    assert client.closed
    assert runtime_closed == [True]
    assert lease_released == [True]
    assert supervisor._client is None
    assert supervisor._embedded_runtime is None
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'liveness probe failed (1/3)' in text
    assert 'liveness probe failed (2/3)' in text
    assert 'failed 3 consecutive liveness probes' in text
    assert any(r.levelno == logging.CRITICAL and 'became unavailable' in
               r.getMessage() for r in caplog.records)


def test_embedded_recovery_between_failures_resets_the_run(caplog):
    supervisor = _wired_supervisor(threshold=3)
    # Two failures, a recovery, then two more: never three in a row.
    client = _ScriptedHealthClient(
        ['raise', 'unready', 'ready', 'raise', 'unready'] + ['ready'] * 50)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client
    supervisor._embedded_runtime = object()
    supervisor._embedded_lease = None

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _run_embedded_monitor(supervisor, client)
        deadline = threading.Event()
        assert not deadline.wait(0.5)
        supervisor._embedded_monitor_stop.set()
        thread.join(timeout=5)

    assert crashes == []
    assert any('liveness recovered after 2 failed probe(s)' in
               r.getMessage() for r in caplog.records)


# ── Embedded wedge defense (C8) ──────────────────────────────────────────


def test_embedded_wedged_probe_fences_within_budget_and_logs(monkeypatch,
                                                             caplog):
    monkeypatch.setattr(
        supervisor_mod, '_EMBEDDED_PROBE_JOIN_GRACE_SECONDS', 0.1)
    monkeypatch.setattr(
        supervisor_mod, '_EMBEDDED_WEDGED_CLOSE_BUDGET_SECONDS', 0.2)
    supervisor = _wired_supervisor(interval=0.01, deadline=0.05)
    client = _ScriptedHealthClient(['hang'])
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client
    close_started = threading.Event()

    class _WedgedRuntime:
        def close(self):
            close_started.set()
            threading.Event().wait(30)  # the engine never answers close

    supervisor._embedded_runtime = _WedgedRuntime()
    supervisor._embedded_lease = None

    started = threading.Event()
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        thread = _run_embedded_monitor(supervisor, client)
        started.set()
        assert thread.join(timeout=10) is None and not thread.is_alive()

    assert crashes == [-1]
    assert supervisor._client is None
    assert supervisor._embedded_runtime is None
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'health probe wedged' in text
    assert 'engine state is unknown' in text
    # The wedged engine close is bounded and reported, not hung on.
    assert 'did not return within' in text
    assert any(r.levelno == logging.CRITICAL and 'wedged' in r.getMessage()
               for r in caplog.records)
    # The monitor fenced and returned: exactly one wedged probe worker and
    # one wedged close worker remain — the documented bounded leak.
    assert close_started.is_set()
    client.release_hang()


def test_embedded_probe_worker_never_exceeds_one_outstanding(monkeypatch):
    monkeypatch.setattr(
        supervisor_mod, '_EMBEDDED_PROBE_JOIN_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor(interval=0.01, deadline=0.02)
    client = _ScriptedHealthClient(['hang'])
    supervisor.add_crash_callback(lambda _code: None)
    supervisor._client = client
    supervisor._embedded_runtime = None
    supervisor._embedded_lease = None

    thread = _run_embedded_monitor(supervisor, client)
    thread.join(timeout=10)
    assert not thread.is_alive()
    # One wedge → fence → return. No further probes were started after the
    # wedge, so at most one probe worker can ever be outstanding.
    assert client.calls == 1
    client.release_hang()


# ── External monitor threshold semantics ─────────────────────────────────


def _external_supervisor(**kwargs):
    supervisor = _wired_supervisor(**kwargs)
    supervisor._external_attached = True
    return supervisor


def test_external_single_transient_failure_does_not_detach():
    supervisor = _external_supervisor(threshold=3)
    client = _ScriptedHealthClient(['raise'] + ['ready'] * 50)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    thread = _run_external_monitor(supervisor, client)
    deadline = threading.Event()
    assert not deadline.wait(0.4)
    assert crashes == []
    assert supervisor._client is client
    assert supervisor._external_attached
    supervisor._external_monitor_stop.set()
    thread.join(timeout=5)
    assert crashes == []


def test_external_threshold_failures_detach_and_fire_callbacks(caplog):
    # No published PID/start identity (SQL co-container): exit cannot be told
    # from starvation, so the unresponsiveness budget does not apply.
    supervisor = _external_supervisor(threshold=3, unresponsive=3600.0)
    client = _ScriptedHealthClient(['unready'] * 10)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        thread = _run_external_monitor(supervisor, client)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert crashes == [-1]
    assert supervisor._client is None
    assert not supervisor._external_attached
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'liveness probe failed (1/3)' in text
    assert 'liveness probe failed (2/3)' in text
    assert 'failed 3 consecutive liveness probes; detaching' in text
    assert 'became unavailable' in text


# ── External attachment liveness policy (2026-10-27) ─────────────────────


@pytest.fixture
def healthy_host(monkeypatch):
    monkeypatch.setattr(supervisor_mod, '_host_overload_detail', lambda *args: None)


def _published_engine(supervisor, *, alive):
    """Attach to a published engine identity: this process, or a dead PID."""
    import os
    from server_manager import proc_start_ticks
    pid = os.getpid()
    supervisor._external_pid = pid
    supervisor._external_process_start = proc_start_ticks(pid) + (0 if alive else 1)


def test_external_starved_engine_keeps_its_attachment_and_recovers(healthy_host, caplog):
    """06:58:10-12 replay: three failed probes of a live engine are not death."""
    supervisor = _external_supervisor(threshold=3, unresponsive=30.0)
    _published_engine(supervisor, alive=True)
    client = _ScriptedHealthClient(['raise'] * 12 + ['ready'] * 200)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        thread = _run_external_monitor(supervisor, client)
        assert not threading.Event().wait(0.5)
        supervisor._external_monitor_stop.set()
        thread.join(timeout=5)

    assert crashes == []
    assert supervisor._client is client
    assert supervisor._external_attached
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'liveness probe failed (1/3)' in text
    assert 'detaching' not in text
    assert 'liveness recovered after 12 failed probe(s)' in text


def test_external_engine_exit_detaches_on_the_first_failed_probe(healthy_host, caplog):
    supervisor = _external_supervisor(threshold=3, unresponsive=3600.0)
    _published_engine(supervisor, alive=False)
    client = _ScriptedHealthClient(['raise'] * 10)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        thread = _run_external_monitor(supervisor, client)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls == 1
    assert crashes == [-1]
    assert supervisor._client is None
    assert not supervisor._external_attached
    assert 'exited; detaching' in '\n'.join(r.getMessage() for r in caplog.records)


def test_external_unresponsive_engine_detaches_after_the_budget(healthy_host, caplog):
    import time

    supervisor = _external_supervisor(threshold=3, unresponsive=0.3)
    _published_engine(supervisor, alive=True)
    client = _ScriptedHealthClient(['unready'] * 1000)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        thread = _run_external_monitor(supervisor, client)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - started >= 0.3
    assert crashes == [-1]
    assert 'consecutive liveness probes; detaching' in '\n'.join(
        r.getMessage() for r in caplog.records)


def test_external_attachment_survives_host_overload_beyond_the_budget(monkeypatch):
    """Starvation attribution matches the managed monitor's suppression."""
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', lambda *args: 'synthetic overload')
    supervisor = _external_supervisor(threshold=3, unresponsive=0.05)
    _published_engine(supervisor, alive=True)
    client = _ScriptedHealthClient(['raise'] * 1000)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._client = client

    thread = _run_external_monitor(supervisor, client)
    assert not threading.Event().wait(0.4)
    supervisor._external_monitor_stop.set()
    thread.join(timeout=5)

    assert client.calls > 10
    assert crashes == []
    assert supervisor._external_attached
