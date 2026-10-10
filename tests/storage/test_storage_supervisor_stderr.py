"""Managed-child pipe hygiene: startup control reader and stderr drainer.

C7: when the validating startup loop raises early (protocol-violating
progress), a chatty child must not wedge the control-channel reader on the
bounded queue — the reader exits on the terminal flag and the thread/pipe
do not leak.

C11+C12: the managed child's stderr is drained continuously (the child can
never block on a full pipe), engine JSON lines are mirrored into the app
pipeline at matching severity (integrity_fence at any level), raw lines are
forwarded to the inherited stderr, over-long lines are truncated with a
marker, and the drainer lifecycle binds to one child at a time.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import pytest

import lib.storage.supervisor as supervisor_mod
from lib.storage.startup_control import (
    STARTUP_PROGRESS_TYPE,
    encode_startup_progress,
)
from lib.storage.supervisor import (
    StorageSupervisor,
    _MAX_ENGINE_STDERR_LINE_CHARS,
    _drain_child_stderr,
)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_external_connection_file(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)


# ── C7: startup control-channel reader never wedges ──────────────────────


class _ChattyChild:
    """Process double: stdout floods progress envelopes without EOF."""

    def __init__(self):
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, 'r')
        self._stop = threading.Event()
        self._writer = threading.Thread(
            target=self._flood, name='test-chatty-child', daemon=True)
        self._writer.start()

    def _flood(self):
        total = 100
        while not self._stop.is_set():
            total += 1
            try:
                line = encode_startup_progress('replay', 1, total) + '\n'
                os.write(self._write_fd, line.encode('utf-8'))
            except OSError:
                return

    def close(self):
        self._stop.set()
        try:
            os.close(self._write_fd)
        except OSError:
            pass
        self.stdout.close()
        self._writer.join(timeout=5)


def _reader_threads():
    return [t for t in threading.enumerate()
            if t.name == 'storage-startup-control']


def test_chatty_child_reader_terminates_on_early_protocol_violation():
    supervisor = StorageSupervisor(backend='sqlite', startup_timeout=30.0)
    child = _ChattyChild()
    assert _reader_threads() == []
    try:
        # Progress totals change within one phase → the validating loop
        # raises RuntimeError immediately, while the child keeps flooding
        # and the bounded queue fills. The reader must exit via the
        # terminal flag instead of wedging on put() forever.
        with pytest.raises(RuntimeError, match='invalid startup progress'):
            supervisor._read_startup_envelope(child)
        deadline = threading.Event()
        for _ in range(100):
            if not _reader_threads():
                break
            deadline.wait(0.05)
        assert _reader_threads() == []
    finally:
        child.close()


def test_progress_type_constant_used_by_flood():
    # Guard the fixture's shape: a drifted contract name must fail loudly.
    encoded = json.loads(encode_startup_progress('replay', 1, 100))
    assert encoded['type'] == STARTUP_PROGRESS_TYPE


# ── C11+C12: engine stderr draining and mirroring ────────────────────────


def _run_drainer(lines, *, caplog):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, 'r')
    thread = threading.Thread(
        target=_drain_child_stderr, args=(reader,), kwargs={'pid': 4321},
        name='test-stderr-drainer', daemon=True)
    with caplog.at_level(logging.DEBUG, logger='tofu.storage.engine'):
        thread.start()
        for line in lines:
            os.write(write_fd, line.encode('utf-8'))
        os.close(write_fd)  # EOF: the child's exit signal
        thread.join(timeout=5)
    assert not thread.is_alive()
    reader.close()
    return [r for r in caplog.records]


def test_engine_json_lines_mirror_at_matching_severity_and_forward(caplog,
                                                                   capfd):
    lines = [
        json.dumps({
            'source': 'tofu-db', 'level': 'error',
            'target': 'background_pack', 'message': 'pack failed'}) + '\n',
        json.dumps({
            'source': 'tofu-db', 'level': 'warn',
            'target': 'integrity_fence', 'message': 'fenced write'}) + '\n',
        json.dumps({
            'source': 'tofu-db', 'level': 'warn',
            'target': 'background_pack', 'message': 'ordinary warn'}) + '\n',
        'fsync_stats: commits=12 p99=3ms\n',
        'plain non-json child line\n',
    ]
    records = _run_drainer(lines, caplog=caplog)
    by_message = {r.getMessage(): r for r in records}

    error_records = [r for r in records if 'pack failed' in r.getMessage()]
    assert len(error_records) == 1
    assert error_records[0].levelno == logging.ERROR

    fence_records = [r for r in records if 'fenced write' in r.getMessage()]
    assert len(fence_records) == 1
    assert fence_records[0].levelno == logging.WARNING  # any level mirrors

    # Ordinary warn lines stay at debug (not mirrored as incidents).
    ordinary = [r for r in records if 'ordinary warn' in r.getMessage()]
    assert len(ordinary) == 1
    assert ordinary[0].levelno == logging.DEBUG

    # Stats dumps mirror at debug; non-JSON lines forward as-is at INFO.
    assert by_message and all(
        r.levelno == logging.DEBUG for r in records
        if 'fsync_stats' in r.getMessage())
    plain = [r for r in records if 'plain non-json child line' in
             r.getMessage()]
    assert len(plain) == 1
    assert plain[0].levelno == logging.INFO

    # Every raw line also reached the inherited stderr fd, verbatim.
    assert capfd.readouterr().err == ''.join(lines)


def test_overlong_line_is_truncated_with_marker(caplog, capfd):
    oversized = 'x' * (_MAX_ENGINE_STDERR_LINE_CHARS + 4096)
    records = _run_drainer([oversized + '\n'], caplog=caplog)
    mirrored = [r for r in records if 'x' * 64 in r.getMessage()]
    assert len(mirrored) == 1
    message = mirrored[0].getMessage()
    assert '…[truncated]' in message
    assert len(message) < _MAX_ENGINE_STDERR_LINE_CHARS + 512
    # The forward also stays bounded (no unbounded buffers).
    assert len(capfd.readouterr().err) < _MAX_ENGINE_STDERR_LINE_CHARS + 512


def test_respawn_detects_a_dead_previous_drainer(caplog):
    supervisor = StorageSupervisor(backend='sqlite')

    class _Process:
        def __init__(self, pid):
            self.pid = pid
            self._read_fd, self.write_fd = os.pipe()
            self.stderr = os.fdopen(self._read_fd, 'r')

    first = _Process(1111)
    supervisor._start_stderr_drainer(first)
    first_drainer = supervisor._stderr_drainer
    assert first_drainer is not None and first_drainer.is_alive()
    os.close(first.write_fd)  # child death → EOF → drainer exits
    first_drainer.join(timeout=5)
    assert not first_drainer.is_alive()

    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        second = _Process(2222)
        supervisor._start_stderr_drainer(second)
    # A clean previous drainer is ordinary turnover: no alarm, new binding.
    assert not [r for r in caplog.records
                if r.levelno >= logging.CRITICAL]
    assert supervisor._stderr_drainer is not first_drainer
    assert supervisor._stderr_drainer.name.endswith('-2222')
    os.close(second.write_fd)
    supervisor._stderr_drainer.join(timeout=5)
    second.stderr.close()
    first.stderr.close()


def test_live_previous_drainer_at_respawn_is_reported(caplog):
    supervisor = StorageSupervisor(backend='sqlite')
    release = threading.Event()
    zombie = threading.Thread(
        target=release.wait, args=(30,), name='storage-stderr-drainer-9999',
        daemon=True)
    zombie.start()
    supervisor._stderr_drainer = zombie

    class _Process:
        pid = 2222

        def __init__(self):
            self._read_fd, self.write_fd = os.pipe()
            self.stderr = os.fdopen(self._read_fd, 'r')

    process = _Process()
    with caplog.at_level(logging.WARNING, logger='tofu.storage.supervisor'):
        supervisor._start_stderr_drainer(process)
    assert any(r.levelno == logging.CRITICAL and 'still alive' in
               r.getMessage() for r in caplog.records)
    # Exactly one drainer is bound to the new child.
    assert supervisor._stderr_drainer is not zombie
    assert supervisor._stderr_drainer.name == 'storage-stderr-drainer-2222'
    release.set()
    zombie.join(timeout=5)
    os.close(process.write_fd)
    supervisor._stderr_drainer.join(timeout=5)
    process.stderr.close()
