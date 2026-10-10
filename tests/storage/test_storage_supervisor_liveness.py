"""Managed-child liveness supervision for the storage sidecar.

The managed monitor must detect a live-but-unresponsive child (wedged
daemon: process alive, listener dead) and recycle it through the ordinary
exit path — fence readiness, fire crash callbacks, let the runtime restart
loop spawn a replacement. Waiting on process exit alone missed that mode
and kept storage down until an operator restarted the application.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.supervisor import (
    StorageSupervisor,
    _liveness_probe_config,
)
import lib.storage.supervisor as supervisor_mod


pytestmark = pytest.mark.unit

# Captured at import time, before the autouse healthy-host fixture patches
# the module attribute; lets the signal test restore the real implementation.
_REAL_HOST_OVERLOAD_DETAIL = supervisor_mod._host_overload_detail


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


@pytest.fixture(autouse=True)
def _healthy_host_by_default(monkeypatch):
    """Pin the host-overload seam healthy; overload-specific tests re-patch it.

    The gate reads real host pressure, and CI/dev machines can genuinely be
    overloaded — recycle behavior must stay deterministic under test.
    """
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', lambda *args: None)


class _OwnerChannel:
    """stdin-pipe double: close() delivers the parent-lease EOF."""

    def __init__(self, on_close):
        self._on_close = on_close
        self.closed = False

    def close(self):
        self.closed = True
        self._on_close()


class _FakeChild:
    """Popen double: alive until told to exit; EOF/SIGTERM/SIGKILL visible."""

    def __init__(self, *, die_on_eof=False, die_on_term=True, exit_code=0,
                 exit_delay=0.0):
        self._die_on_eof = die_on_eof
        self.stdin = _OwnerChannel(self._eof)
        self.pid = 424242
        self.terminate_calls = 0
        self.kill_calls = 0
        self._die_on_term = die_on_term
        self._exit_code = exit_code
        self._returncode = None
        self._exited = threading.Event()
        if exit_delay:
            threading.Timer(exit_delay, self._exit).start()

    def _eof(self):
        if self._die_on_eof:
            self._exit(self._exit_code)

    def _exit(self, code=None):
        self._returncode = self._exit_code if code is None else code
        self._exited.set()

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._exited.wait(timeout):
            return self._returncode
        raise subprocess.TimeoutExpired(cmd='fake-child', timeout=timeout)

    def terminate(self):
        self.terminate_calls += 1
        if self._die_on_term:
            self._exit(self._exit_code)

    def kill(self):
        self.kill_calls += 1
        self._exit(-9)


class _FailingHealthClient:
    def __init__(self):
        self.calls = 0

    def health(self, deadline=None):
        self.calls += 1
        raise StorageError('database_unavailable', 'authority is unavailable')


class _HealthyThenExitClient:
    def __init__(self, fail_first=0):
        self.calls = 0
        self._fail_first = fail_first

    def health(self, deadline=None):
        self.calls += 1
        if self.calls <= self._fail_first:
            raise StorageError('database_unavailable', 'transient')
        return {'ready': True, 'backend': 'sqlite'}


def _wired_supervisor(*, interval=0.01, deadline=0.01, threshold=3,
                      max_suppress=None, overload_cooldown=None):
    supervisor = StorageSupervisor()
    supervisor._liveness_interval_seconds = interval
    supervisor._liveness_probe_deadline_seconds = deadline
    supervisor._liveness_failure_threshold = threshold
    if max_suppress is not None:
        supervisor._liveness_overload_max_suppress = max_suppress
    if overload_cooldown is not None:
        supervisor._liveness_overload_recycle_cooldown = overload_cooldown
    return supervisor


def _start_monitor(supervisor, child):
    thread = threading.Thread(
        target=supervisor._monitor_process, args=(child,),
        name='test-storage-supervisor', daemon=True)
    thread.start()
    return thread


def test_wedged_child_is_killed_and_fired_through_the_crash_path(monkeypatch):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_LIVENESS_TERMINATE_GRACE_SECONDS', 0.05)
    # A wedged child on a healthy host is recycled; keep the test hermetic
    # on hosts that are themselves overloaded (recycle would be withheld).
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', lambda *args: None)
    supervisor = _wired_supervisor()
    # Wedged daemon: ignores EOF and SIGTERM, answers nothing.
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert crashes == [-9]
    assert supervisor._last_exit_code == -9
    assert supervisor._client is None
    assert supervisor._process is None
    assert not supervisor.ready


def test_wedged_child_answering_eof_needs_no_signal(monkeypatch):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 1.0)
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', lambda *args: None)
    supervisor = _wired_supervisor()
    child = _FakeChild(die_on_eof=True, exit_code=-15)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _FailingHealthClient()

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [-15]
    assert not supervisor.ready


def test_healthy_child_exits_without_any_recycle_signal():
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=1, exit_delay=0.15)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _HealthyThenExitClient()

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert crashes == [1]
    assert supervisor._last_exit_code == 1


def test_transient_probe_failures_below_threshold_do_not_recycle():
    supervisor = _wired_supervisor(threshold=3)
    child = _FakeChild(exit_code=0, exit_delay=0.4)
    client = _HealthyThenExitClient(fail_first=2)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls >= 2
    assert child.terminate_calls == 0
    assert child.kill_calls == 0


def test_intentional_stop_never_probes_or_recycles():
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=0, exit_delay=0.2)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client
    supervisor._intentional_stop = True

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls == 0
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    # Intentional stop is not a crash: no callbacks, exit code recorded.
    assert crashes == []
    assert supervisor._last_exit_code == 0


def test_liveness_probe_config_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS',
                       raising=False)
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS',
                       raising=False)
    monkeypatch.delenv('TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD',
                       raising=False)
    assert _liveness_probe_config({}) == (1.0, 1.5, 3)

    assert _liveness_probe_config({
        'TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS': '0.5',
        'TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS': '2.5',
        'TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD': '5',
    }) == (0.5, 2.5, 5)

    # Invalid values fall back; out-of-range values clamp into bounds.
    interval, deadline, threshold = _liveness_probe_config({
        'TOFU_STORAGE_LIVENESS_INTERVAL_SECONDS': 'bogus',
        'TOFU_STORAGE_LIVENESS_PROBE_DEADLINE_SECONDS': '999',
        'TOFU_STORAGE_LIVENESS_FAILURE_THRESHOLD': '0',
    })
    assert interval == 1.0
    assert deadline == 10.0
    assert threshold == 1


def test_liveness_overload_config_defaults_and_bounds():
    from lib.storage.supervisor import _liveness_overload_config

    assert _liveness_overload_config({}) == (0.92, 2.0)

    assert _liveness_overload_config({
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MEM_RATIO': '0.85',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_LOAD_FACTOR': '4',
    }) == (0.85, 4.0)

    # Invalid values fall back; out-of-range values clamp into bounds.
    mem_ratio, load_factor = _liveness_overload_config({
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MEM_RATIO': 'bogus',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_LOAD_FACTOR': '0.1',
    })
    assert mem_ratio == 0.92
    assert load_factor == 1.0
    mem_ratio, load_factor = _liveness_overload_config({
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MEM_RATIO': '1.5',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_LOAD_FACTOR': '999',
    })
    assert mem_ratio == 1.0
    assert load_factor == 32.0


def test_host_overload_detail_mem_and_load_signals(monkeypatch):
    # The autouse fixture stubs this function out; exercise the real one.
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', _REAL_HOST_OVERLOAD_DETAIL)
    readings = {}

    def fake_read(paths):
        return readings.get(paths[0])

    monkeypatch.setattr(supervisor_mod, '_read_cgroup_int', fake_read)
    monkeypatch.setattr(
        supervisor_mod.os, 'getloadavg', lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(
        supervisor_mod.os, 'sched_getaffinity', lambda pid: {0, 1, 2, 3})

    # Neither signal: healthy.
    readings['/sys/fs/cgroup/memory.current'] = 50
    readings['/sys/fs/cgroup/memory.max'] = 100
    assert supervisor_mod._host_overload_detail(0.92, 2.0) is None

    # Memory pressure alone flags overload.
    readings['/sys/fs/cgroup/memory.current'] = 99
    detail = supervisor_mod._host_overload_detail(0.92, 2.0)
    assert detail is not None and 'cgroup memory' in detail

    # Load alone flags overload even with comfortable memory.
    readings['/sys/fs/cgroup/memory.current'] = 10
    monkeypatch.setattr(
        supervisor_mod.os, 'getloadavg', lambda: (12.0, 9.0, 4.0))
    detail = supervisor_mod._host_overload_detail(0.92, 2.0)
    assert detail is not None and 'loadavg1' in detail

    # Unobservable host falls back to healthy (fail-fast behavior kept).
    readings.clear()

    def raise_oserror():
        raise OSError('no getloadavg')

    monkeypatch.setattr(supervisor_mod.os, 'getloadavg', raise_oserror)
    assert supervisor_mod._host_overload_detail(0.92, 2.0) is None


def test_overloaded_host_short_starvation_window_never_recycles(monkeypatch):
    """A short run of failing probes under overload must not terminate it.

    The suppression bound is pinned far above the probes this child can
    accrue before it exits, isolating the withhold behavior from the
    separately tested bounded-recovery behavior.
    """
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail',
        lambda *args: 'synthetic overload')
    supervisor = _wired_supervisor(max_suppress=1_000_000)
    child = _FakeChild(exit_code=0, exit_delay=0.4)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    # Far more probes than the kill threshold ran; none recycled the child.
    assert client.calls >= supervisor._liveness_failure_threshold + 1
    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    # The eventual natural exit still fires the ordinary crash path.
    assert crashes == [0]


def test_recycle_resumes_once_overload_clears(monkeypatch):
    """Suppression is not a latch: a wedged child is recycled after relief."""
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    state = {'overloaded': True}

    def fake_overload(*args):
        return 'synthetic overload' if state['overloaded'] else None

    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail', fake_overload)
    supervisor = _wired_supervisor()
    child = _FakeChild(die_on_eof=False, die_on_term=True, exit_code=-15)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    # Let several suppressed probes accumulate, then relieve the host.
    deadline = threading.Event()

    def relieve_after_suppression():
        while client.calls < supervisor._liveness_failure_threshold + 2:
            if deadline.wait(0.005):
                return
        state['overloaded'] = False

    reliever = threading.Thread(
        target=relieve_after_suppression, daemon=True)
    reliever.start()
    thread.join(timeout=10)
    deadline.set()
    reliever.join(timeout=5)

    assert not thread.is_alive()
    assert child.terminate_calls == 1
    assert child.kill_calls == 0
    assert crashes == [-15]


def test_overloaded_host_forces_bounded_recycle_after_suppression_bound(
        monkeypatch):
    """A child unresponsive through a whole suppression window is wedged.

    On a shared cgroup the overload never clears, so withholding forever is a
    permanent outage; the monitor must force exactly one bounded recycle once
    the window is spent, and set the rate-limit floor against a storm.
    """
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_LIVENESS_TERMINATE_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail',
        lambda *args: 'synthetic overload')
    supervisor = _wired_supervisor(max_suppress=5, overload_cooldown=300.0)
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    client = _FailingHealthClient()
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = client

    thread = _start_monitor(supervisor, child)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert client.calls >= 5
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert crashes == [-9]
    # The forced recycle set (and escalated) the rate-limit floor.
    assert supervisor._overload_recycle_backoff == 300.0
    assert supervisor._overload_recycle_not_before > 0.0


def test_overload_recycle_backoff_escalates_then_caps():
    supervisor = _wired_supervisor(overload_cooldown=300.0)
    assert supervisor._overload_recycle_backoff == 0.0
    supervisor._escalate_overload_recycle_backoff()
    assert supervisor._overload_recycle_backoff == 300.0
    supervisor._escalate_overload_recycle_backoff()
    assert supervisor._overload_recycle_backoff == 600.0
    for _ in range(10):
        supervisor._escalate_overload_recycle_backoff()
    assert (supervisor._overload_recycle_backoff
            == supervisor_mod._MAX_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_S)


def test_liveness_overload_suppression_config_defaults_and_bounds():
    from lib.storage.supervisor import _liveness_overload_suppression_config

    assert _liveness_overload_suppression_config({}) == (60, 300.0, 30.0)

    assert _liveness_overload_suppression_config({
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MAX_SUPPRESS': '5',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_SECONDS': '10',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_STICKY_SECONDS': '7',
    }) == (5, 10.0, 7.0)

    # Invalid values fall back; out-of-range values clamp into bounds.
    max_suppress, cooldown, sticky = _liveness_overload_suppression_config({
        'TOFU_STORAGE_LIVENESS_OVERLOAD_MAX_SUPPRESS': 'bogus',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_SECONDS': '999999',
        'TOFU_STORAGE_LIVENESS_OVERLOAD_STICKY_SECONDS': '-1',
    })
    assert max_suppress == 60
    assert sticky == 0.0


def test_host_overload_sticky_latches_across_reclaim_dip(monkeypatch):
    """A momentary healthy reading mid-overload must not clear the gate.

    On cgroup v1 page-cache reclaim flickers the usage ratio across the
    watermark between probes; without stickiness a 3-probe streak of lucky
    dips reads 'healthy' and a starved-but-alive child is killed (the 29
    self-inflicted SIGTERMs of 2026-09-23/24). The sticky window holds the
    last overloaded verdict across that dip.
    """
    supervisor = _wired_supervisor()
    supervisor._liveness_overload_sticky_seconds = 30.0
    state = {'overloaded': True}

    def fake_detail(*args):
        return 'cgroup memory 94.4%' if state['overloaded'] else None

    monkeypatch.setattr(supervisor_mod, '_host_overload_detail', fake_detail)

    # Overloaded reading arms the latch.
    assert supervisor._host_overload_sticky() == 'cgroup memory 94.4%'
    # A reclaim dip inside the window still reports overloaded (sticky).
    state['overloaded'] = False
    held = supervisor._host_overload_sticky()
    assert held is not None and 'sticky' in held
    # Past the window a healthy reading clears the latch.
    supervisor._overload_sticky_until = 0.0
    assert supervisor._host_overload_sticky() is None


def test_host_overload_sticky_disabled_when_window_zero(monkeypatch):
    """A zero sticky window preserves the original point-in-time gate."""
    supervisor = _wired_supervisor()
    supervisor._liveness_overload_sticky_seconds = 0.0
    state = {'overloaded': True}
    monkeypatch.setattr(
        supervisor_mod, '_host_overload_detail',
        lambda *args: 'cgroup memory 94.4%' if state['overloaded'] else None)
    assert supervisor._host_overload_sticky() == 'cgroup memory 94.4%'
    state['overloaded'] = False
    assert supervisor._host_overload_sticky() is None
