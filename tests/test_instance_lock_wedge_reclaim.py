#!/usr/bin/env python3
"""Tests for wedged-holder reclaim of the single-instance startup lock.

Background: ``_acquire_instance_lock`` refuses to reclaim a lock whose recorded
owner is a LIVE local ``server.py`` (``_pid_is_live_server`` True). But a server
whose event loop is WEDGED in a FUSE syscall — the proven root cause of the
5-minute restart stalls — is still alive, still ``server.py``, still holds the
flock, yet cannot serve or release the lock. The fix adds a second signal: the
live loop persists a wall-clock heartbeat to a local-disk sidecar; a restarting
process reads it to distinguish a HEALTHY holder (fresh heartbeat → refuse) from
a WEDGED one (stale heartbeat → reclaim). Every ambiguous case (missing /
unparseable / mismatched-pid / future-dated heartbeat) is fail-safe: refuse.

These tests drive the REAL ``_acquire_instance_lock`` (orphan-held flock, as in
test_instance_lock_reclaim.py) plus the pure helpers, and include an in-memory
NEUTER proving the wedge branch is load-bearing. The neuter monkeypatches the
IMPORTED module (never writes server.py on disk), so it cannot poison the tree —
no ``_NC_GUARDED_SOURCES`` entry needed.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def srv():
    import server  # noqa: F401 — side-effect installs Flask→Quart shim
    return server


class _Log:
    def __init__(self):
        self.records = []

    def _rec(self, level, msg, *a):
        self.records.append((level, (msg % a) if a else msg))

    def debug(self, m, *a):
        self._rec('debug', m, *a)

    def info(self, m, *a):
        self._rec('info', m, *a)

    def warning(self, m, *a):
        self._rec('warning', m, *a)

    def critical(self, m, *a):
        self._rec('critical', m, *a)

    def has(self, level, needle):
        return any(lv == level and needle in msg for lv, msg in self.records)


def _write_lock(path, pid, host):
    with open(path, 'w') as f:
        f.write('%d@%s\n' % (pid, host))


# ─────────────────────────── pure-helper unit tests ──────────────────────────

def test_write_then_read_roundtrip(srv, tmp_path):
    hb = str(tmp_path / 'server.heartbeat')
    assert srv._write_heartbeat(pid=4321, ts=1000.0, path=hb) is True
    assert srv._read_heartbeat(hb) == (4321, 1000.0)


def test_read_missing_and_garbage_are_none(srv, tmp_path):
    assert srv._read_heartbeat(str(tmp_path / 'nope')) == (None, None)
    bad = str(tmp_path / 'server.heartbeat')
    with open(bad, 'w') as f:
        f.write('not-json{{{')
    assert srv._read_heartbeat(bad) == (None, None)


def test_stale_threshold_conservative_floor(srv, monkeypatch):
    monkeypatch.delenv('TOFU_LOOP_HEARTBEAT_SECS', raising=False)
    assert srv._heartbeat_stale_threshold() == 30.0
    monkeypatch.setenv('TOFU_LOOP_HEARTBEAT_SECS', '20')
    assert srv._heartbeat_stale_threshold() == 60.0  # 3×20 > 30
    monkeypatch.setenv('TOFU_LOOP_HEARTBEAT_SECS', '0')  # invalid → 1.0 floor
    assert srv._heartbeat_stale_threshold() == 30.0


def test_holder_wedge_age_matrix(srv, tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_LOOP_HEARTBEAT_SECS', raising=False)  # threshold=30
    hb = str(tmp_path / 'server.heartbeat')
    now = 10_000.0

    # Fresh heartbeat for THIS pid → not wedged.
    srv._write_heartbeat(pid=777, ts=now - 5, path=hb)
    assert srv._holder_wedge_age(777, now=now, path=hb) is None

    # Stale heartbeat for THIS pid → wedged (returns the age).
    srv._write_heartbeat(pid=777, ts=now - 90, path=hb)
    assert srv._holder_wedge_age(777, now=now, path=hb) == pytest.approx(90.0)

    # Stale but for a DIFFERENT pid → ambiguous → not wedged (fail-safe).
    srv._write_heartbeat(pid=888, ts=now - 90, path=hb)
    assert srv._holder_wedge_age(777, now=now, path=hb) is None

    # Future-dated (clock skew) → not wedged.
    srv._write_heartbeat(pid=777, ts=now + 50, path=hb)
    assert srv._holder_wedge_age(777, now=now, path=hb) is None

    # Missing file → not wedged.
    assert srv._holder_wedge_age(777, now=now, path=str(tmp_path / 'gone')) is None


# ─────────────────── end-to-end reclaim decision (real flock) ─────────────────

def _acquire_with_holder(srv, lock, log, wedge_age):
    """Drive _acquire_instance_lock with our own pid recorded as a live server
    and _holder_wedge_age forced to *wedge_age*. Simulates an orphan holding
    the flock so acquisition must go through the reclaim path."""
    import fcntl
    _write_lock(lock, os.getpid(), 'thishost')
    orphan = open(lock, 'r+')
    fcntl.flock(orphan.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    orig_live = srv._pid_is_live_server
    orig_wedge = srv._holder_wedge_age
    srv._pid_is_live_server = lambda pid: pid == os.getpid()
    srv._holder_wedge_age = lambda pid, **kw: wedge_age
    try:
        return srv._acquire_instance_lock(lock, log, hostname='thishost')
    finally:
        srv._pid_is_live_server = orig_live
        srv._holder_wedge_age = orig_wedge
        orphan.close()


def test_wedged_holder_is_reclaimed(srv, tmp_path):
    """A live-local server.py whose heartbeat is stale → reclaimed."""
    lock = str(tmp_path / '.server.lock')
    log = _Log()
    ok, fd = _acquire_with_holder(srv, lock, log, wedge_age=95.0)
    try:
        assert ok is True and fd is not None
        assert log.has('critical', 'WEDGED local server')
        assert log.has('info', 'reclaimed stale lock and acquired')
        with open(lock) as f:
            assert f.readline().strip() == '%d@thishost' % os.getpid()
    finally:
        if fd:
            fd.close()


def test_fresh_holder_is_refused(srv, tmp_path):
    """A live-local server.py with a FRESH heartbeat (wedge_age None) → refuse."""
    lock = str(tmp_path / '.server.lock')
    log = _Log()
    ok, fd = _acquire_with_holder(srv, lock, log, wedge_age=None)
    assert ok is False and fd is None
    assert log.has('critical', 'held by a LIVE local server')
    assert not log.has('critical', 'WEDGED local server')
    assert os.path.exists(lock)  # not unlinked


def test_missing_heartbeat_refuses_like_today(srv, tmp_path):
    """No sidecar at all → _holder_wedge_age returns None → refuse (fail-safe:
    preserves today's OOM-orphan protection)."""
    lock = str(tmp_path / '.server.lock')
    log = _Log()
    # Point the heartbeat at an empty dir so the REAL _holder_wedge_age runs and
    # finds nothing (do NOT stub it here — we want the genuine fail-safe path).
    import fcntl
    _write_lock(lock, os.getpid(), 'thishost')
    orphan = open(lock, 'r+')
    fcntl.flock(orphan.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    orig_live = srv._pid_is_live_server
    srv._pid_is_live_server = lambda pid: pid == os.getpid()
    os.environ['TOFU_HEARTBEAT_DIR'] = str(tmp_path / 'empty_hb')
    try:
        ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
        assert ok is False and fd is None
        assert log.has('critical', 'held by a LIVE local server')
        assert os.path.exists(lock)
    finally:
        srv._pid_is_live_server = orig_live
        os.environ.pop('TOFU_HEARTBEAT_DIR', None)
        orphan.close()


def test_foreign_host_refused_unchanged(srv, tmp_path):
    """A wedge signal must NOT weaken the cross-host guard: a foreign-host lock
    is refused BEFORE the live/wedge check is ever reached."""
    lock = str(tmp_path / '.server.lock')
    log = _Log()
    _write_lock(lock, 999999, 'otherhost')
    import fcntl
    holder = open(lock, 'r+')
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ino_before = os.stat(lock).st_ino
    # Even if a wedge were "detected", the foreign-host branch returns first.
    orig_wedge = srv._holder_wedge_age
    srv._holder_wedge_age = lambda pid, **kw: 999.0
    try:
        ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
        assert ok is False and fd is None
        assert log.has('critical', 'held by another host')
        assert os.stat(lock).st_ino == ino_before  # not unlinked
    finally:
        srv._holder_wedge_age = orig_wedge
        holder.close()


# ─────────────────────────────── NEUTER ─────────────────────────────────────

def test_neuter_ignore_wedge_keeps_it_locked(srv, tmp_path):
    """Prove the wedge branch is load-bearing: if _holder_wedge_age is degraded
    to ALWAYS return None (the pre-fix behaviour — every live server treated as
    healthy), the WEDGED holder is NOT reclaimed and the restart is blocked.

    In-memory monkeypatch only — server.py on disk is never modified, so this
    cannot poison the tree (no _NC_GUARDED_SOURCES entry required)."""
    lock = str(tmp_path / '.server.lock')
    log = _Log()
    # wedge_age forced None even though the holder is genuinely wedged.
    ok, fd = _acquire_with_holder(srv, lock, log, wedge_age=None)
    try:
        assert ok is False and fd is None
        assert log.has('critical', 'held by a LIVE local server')
        assert not log.has('info', 'reclaimed stale lock and acquired')
    finally:
        if fd:
            fd.close()
