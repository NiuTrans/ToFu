"""Storage supervisor lifecycle evidence: every transition reaches the log.

2026-09-21 production gap: the managed tofu-db daemon died and the app stayed
fenced for ~18 minutes with ZERO lines from logger ``tofu.storage.supervisor``
in app.log. Root cause was two-fold and both halves are pinned here:

* routing: ``tofu.*`` logger names were not in the business prefix set, so
  app.log/error.log dropped every supervisor record (WARNING+ leaked only to
  vendor.log, INFO vanished entirely);
* coverage: spawn, termination-escalation, exit (with signal/uptime), and
  restart scheduling transitions now all emit through
  ``lib.log.get_logger`` at appropriate levels.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading

import pytest

import lib.storage.supervisor as supervisor_mod
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


# ── Routing (the silent half of the incident) ────────────────────────────


def _record(name: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg='probe', args=(), exc_info=None)


def test_tofu_storage_loggers_are_business_records_not_vendor():
    from lib.server.server_logging import _BizAndServerOnly, _BizOnly, _VendorOnly

    for name in (
        'tofu.storage.supervisor',
        'tofu.storage.runtime',
        'tofu.storage.engine',
        'tofu.control_rpc',
        'tofu.orchestrator',
    ):
        assert _BizOnly().filter(_record(name)), name
        assert _BizAndServerOnly().filter(_record(name)), name
        assert not _VendorOnly().filter(_record(name)), name


def test_underscore_namespaces_keep_their_existing_routing():
    from lib.server.server_logging import _BizOnly, _VendorOnly

    # tofu_search was already business; tofu_agent/tofu_db keep vendor
    # routing (only their own WARNING+ diagnostics exist today).
    assert _BizOnly().filter(_record('tofu_search.pipeline'))
    assert _VendorOnly().filter(_record('tofu_agent.runtime'))
    assert _VendorOnly().filter(_record('tofu_db.wire'))
    assert _VendorOnly().filter(_record('urllib3.connectionpool'))


# ── Spawn/ready evidence through a fake child ────────────────────────────


class _FakePopen:
    """Popen double over real pipes: stdin lease, stdout control, stderr."""

    def __init__(self):
        self.pid = 424243
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        self.stdin = os.fdopen(in_w, 'w')
        self.stdout = os.fdopen(out_r, 'r')
        self.stderr = os.fdopen(err_r, 'r')
        self._stdout_writer = os.fdopen(out_w, 'w')
        self._stderr_writer = os.fdopen(err_w, 'w')
        self._stdin_reader = os.fdopen(in_r, 'r')
        self._returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def emit(self, obj) -> None:
        self._stdout_writer.write(json.dumps(obj) + '\n')
        self._stdout_writer.flush()

    def emit_stderr(self, line: str) -> None:
        self._stderr_writer.write(line + '\n')
        self._stderr_writer.flush()

    def _die(self, code: int) -> None:
        # A real dying child closes its pipe write ends: stdout/stderr
        # readers observe EOF and the stderr drainer thread exits.
        self._returncode = code
        for writer in (self._stdout_writer, self._stderr_writer):
            try:
                writer.close()
            except OSError:
                pass

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is not None:
            return self._returncode
        raise subprocess.TimeoutExpired(cmd='fake-popen', timeout=timeout)

    def terminate(self):
        self.terminate_calls += 1
        self._die(-15)

    def kill(self):
        self.kill_calls += 1
        self._die(-9)

    def close_pipes(self):
        for stream in (
            self.stdin, self.stdout, self.stderr,
            self._stdout_writer, self._stderr_writer, self._stdin_reader,
        ):
            try:
                stream.close()
            except OSError:
                pass


class _ReadyClient:
    def health(self, deadline=None):
        return {'ready': True, 'backend': 'sqlite'}


def _start_with_fake_child(monkeypatch, child):
    monkeypatch.setattr(
        supervisor_mod.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(
        supervisor_mod, 'StorageClient', lambda *a, **kw: _ReadyClient())
    supervisor = StorageSupervisor(
        project_root=None, backend='sqlite', startup_timeout=5.0)
    return supervisor


def test_spawn_and_ready_transitions_are_logged(monkeypatch, caplog):
    child = _FakePopen()
    supervisor = _start_with_fake_child(monkeypatch, child)
    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        import threading as _t
        emitter = _t.Timer(0.05, lambda: child.emit({
            'type': 'storage.ready', 'protocol': 'storage.v1',
            'port': 47111, 'backend': 'sqlite'}))
        emitter.start()
        try:
            client = supervisor.start()
        finally:
            emitter.cancel()
    assert client is not None
    messages = [r.getMessage() for r in caplog.records]
    assert any('spawned pid=424243' in m for m in messages), messages
    assert any('ready pid=424243' in m for m in messages), messages
    # stderr=PIPE with a live drainer bound to this child.
    assert supervisor._stderr_drainer is not None
    supervisor.stop(timeout=2.0)
    child.close_pipes()


# ── Monitor/termination/exit evidence ────────────────────────────────────


class _OwnerChannel:
    def __init__(self, on_close):
        self._on_close = on_close
        self.closed = False

    def close(self):
        self.closed = True
        self._on_close()


class _FakeChild:
    def __init__(self, *, die_on_eof=False, die_on_term=True, exit_code=0):
        self._die_on_eof = die_on_eof
        self.stdin = _OwnerChannel(self._eof)
        self.pid = 424244
        self.terminate_calls = 0
        self.kill_calls = 0
        self._die_on_term = die_on_term
        self._exit_code = exit_code
        self._returncode = None
        self._exited = threading.Event()

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


class _AlwaysFailingClient:
    def health(self, deadline=None):
        raise RuntimeError('authority is unavailable')


def _wired_supervisor(*, interval=0.01, deadline=0.01, threshold=3):
    supervisor = StorageSupervisor()
    supervisor._liveness_interval_seconds = interval
    supervisor._liveness_probe_deadline_seconds = deadline
    supervisor._liveness_failure_threshold = threshold
    # Pin the host-overload gate to its maximum bounds: these tests drive
    # the recycle path deterministically, and a genuinely pressured host
    # (e.g. cgroup memory at 97%) would otherwise withhold the expected
    # recycle and wedge the monitor thread.
    supervisor._liveness_overload_mem_ratio = 1.0
    supervisor._liveness_overload_load_factor = 32.0
    return supervisor


def test_recycle_and_exit_emit_every_lifecycle_transition(
        monkeypatch, caplog):
    monkeypatch.setattr(supervisor_mod, '_LIVENESS_EOF_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(
        supervisor_mod, '_LIVENESS_TERMINATE_GRACE_SECONDS', 0.05)
    supervisor = _wired_supervisor()
    supervisor._process_started_at = 1.0  # any past marker → uptime note
    child = _FakeChild(die_on_eof=False, die_on_term=False)
    crashes = []
    supervisor.add_crash_callback(crashes.append)
    supervisor._process = child
    supervisor._client = _AlwaysFailingClient()

    with caplog.at_level(logging.DEBUG, logger='tofu.storage.supervisor'):
        thread = threading.Thread(
            target=supervisor._monitor_process, args=(child,), daemon=True)
        thread.start()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert crashes == [-9]
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    text = '\n'.join(message for _level, message in records)
    # liveness probe failures (count/threshold)
    assert 'liveness probe failed (1/3)' in text
    assert 'liveness probe failed (2/3)' in text
    assert 'failed 3 consecutive liveness probes' in text
    # termination escalation steps, in order
    assert 'closing the owner channel' in text
    assert 'sending SIGTERM' in text
    assert 'sending SIGKILL' in text
    assert text.index('owner channel') < text.index('SIGTERM') \
        < text.index('SIGKILL')
    # child exit with code, decoded signal, and uptime
    assert 'exited unexpectedly exit_code=-9' in text
    assert 'signal=9' in text
    assert 'uptime=' in text
    levels = {message: level for level, message in records}
    assert any(level == logging.CRITICAL and 'exited unexpectedly' in message
               for level, message in records)


def test_clean_stop_exit_is_logged_at_info(caplog):
    supervisor = _wired_supervisor()
    child = _FakeChild(exit_code=0)
    supervisor._process = child
    supervisor._client = None
    supervisor._intentional_stop = True
    child._exit(0)

    with caplog.at_level(logging.INFO, logger='tofu.storage.supervisor'):
        supervisor._monitor_process(child)
    messages = [r.getMessage() for r in caplog.records]
    assert any('stopped exit_code=0' in m for m in messages), messages
    assert not any('exited unexpectedly' in m for m in messages), messages


# ── Restart scheduling/attempt evidence ──────────────────────────────────


class _FlappingSupervisor:
    """Supervisor double: start() fails a scripted number of times."""

    ready = False

    def __init__(self, failures: int):
        self.callback = None
        self.failures = failures
        self.starts = 0

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError('spawn refused')
        self.ready = True
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


def test_restart_scheduling_and_attempts_are_logged(caplog):
    runtime = StorageRuntime(_FlappingSupervisor(failures=2))
    with caplog.at_level(logging.INFO, logger='tofu.storage.runtime'):
        runtime._crashed(-9)
        thread = runtime._restart_thread
        assert thread is not None
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert runtime.ready
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    text = '\n'.join(message for _level, message in records)
    assert 'writes fenced after sidecar exit code=-9; scheduling restart' in text
    assert 'restart attempt 1 failed' in text
    assert 'retrying in' in text
    assert 'restart succeeded after 3 attempt(s)' in text
    assert any(level == logging.WARNING and 'scheduling restart' in message
               for level, message in records)
    runtime.stop(timeout=5.0)
