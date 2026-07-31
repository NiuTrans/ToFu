"""tests/test_audit_log_nonblocking.py — audit_log non-blocking write path.

``audit_log`` must never perform disk I/O on the caller's thread in
production: audit.log lives on a FUSE/NFS mount, and the old synchronous
append (plus per-call ``os.makedirs``) could freeze the event loop when the
mount hung. The caller now only enqueues a serialised JSON line; a dedicated
daemon writer thread does the disk I/O.

Under pytest the write stays synchronous (``_audit_sync_writes``), so the
queue path is exercised here by monkeypatching that predicate off. The
writer reads ``AUDIT_LOG_FILE`` / ``LOG_DIR`` at write time, so redirecting
those module globals works regardless of when the writer thread started.
"""

import json
import threading
import time

import lib.log as log_mod


def _force_queue_path(monkeypatch, tmp_path):
    monkeypatch.setattr(log_mod, '_audit_sync_writes', lambda: False)
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))


def _drain():
    assert log_mod._audit_queue is not None
    log_mod._audit_queue.join()


def test_audit_log_sync_under_pytest_by_default(tmp_path, monkeypatch):
    """Default (pytest) mode: the line is on disk when audit_log returns."""
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.audit_log('sync_event', user='alice')
    lines = (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry['event'] == 'sync_event'
    assert entry['user'] == 'alice'
    assert 'timestamp' in entry


def test_audit_log_queue_path_writes_json_line(tmp_path, monkeypatch):
    """Queue path: the writer thread appends a well-formed JSON line."""
    _force_queue_path(monkeypatch, tmp_path)
    log_mod.audit_log('queued_event', model='opus-5', count=3)
    _drain()
    entry = json.loads(
        (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()[0])
    assert entry['event'] == 'queued_event'
    assert entry['model'] == 'opus-5'
    assert entry['count'] == 3


def test_audit_log_returns_before_write_completes(tmp_path, monkeypatch):
    """The caller must not wait for the disk write — the whole point of the
    queue. Stall the write behind a gate; audit_log must return immediately."""
    _force_queue_path(monkeypatch, tmp_path)
    gate = threading.Event()
    real_write = log_mod._audit_write_line

    def slow_write(line):
        assert gate.wait(5), 'test gate never released'
        real_write(line)

    monkeypatch.setattr(log_mod, '_audit_write_line', slow_write)
    start = time.monotonic()
    log_mod.audit_log('slow_event')
    assert time.monotonic() - start < 1.0
    gate.set()
    _drain()
    assert 'slow_event' in (tmp_path / 'audit.log').read_text(encoding='utf-8')


def test_audit_log_write_failure_falls_back_without_raising(
        tmp_path, monkeypatch, caplog):
    """A failing write on the queue path must not raise into the caller and
    must surface via the 'audit' logger fallback (from the writer thread)."""
    _force_queue_path(monkeypatch, tmp_path)
    # Point at a path whose parent is a regular file → open() fails.
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a dir')
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(blocker / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(blocker))
    with caplog.at_level('ERROR', logger='audit'):
        log_mod.audit_log('doomed_event')
        _drain()
    assert any('Failed to write audit log' in r.message
               and 'doomed_event' in r.message for r in caplog.records)
