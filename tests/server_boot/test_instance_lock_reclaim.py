#!/usr/bin/env python3
"""Tests for the self-healing single-instance startup lock in server.py.

Background: ``_acquire_instance_lock`` guards against two servers running from
the same project dir via an exclusive ``flock`` on ``data/.server.lock``. But a
``flock`` is bound to an open file *description*, not to process liveness — an
OOM ``SIGKILL`` skips the lock release, and an orphaned child (or an unclean
death on a FUSE mount) can keep the fd's advisory lock held indefinitely. So a
contended flock does NOT prove a live server is running.

These tests exercise the reclaim decision (mirroring stop.sh): a stale LOCAL
lock (dead recorded pid) is reclaimed by unlink+retry on a fresh inode; a
foreign-host lock or a live-local-server lock is refused.
"""

import fcntl
import os
import platform
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def srv():
    import lib.server_boot.lock as server
    return server


class _Log:
    """Capture logger calls by level for assertions."""

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


def _dead_pid():
    """A pid guaranteed not to be alive: fork a child and reap it."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _stat_fields(pid):
    with open('/proc/%d/stat' % pid, 'rb') as f:
        raw = f.read()
    return raw[raw.rfind(b')') + 2:].split()


def _await_zombie(pid, *, threads):
    deadline = time.monotonic() + 5
    while True:
        fields = _stat_fields(pid)
        if fields[0] == b'Z' and int(fields[17]) == threads:
            return
        assert time.monotonic() < deadline, 'child did not reach the zombie state'
        time.sleep(0.01)


def _flocked_holder_lock(tmp_path, pid):
    """A lock naming *pid* whose flock stays held on the original inode."""
    lock = str(tmp_path / '.server.lock')
    _write_lock(lock, pid, 'thishost')
    holder = open(lock, 'r+')
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return lock, holder


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux zombie state')
def test_reclaims_lock_of_exited_holder_its_parent_never_reaped(srv, tmp_path):
    """Signal 0 still reaches an unreaped zombie and its cmdline reads empty.

    Counting that as a live server made every replacement boot refuse to
    start for as long as the parent kept the zombie.
    """
    zombie = os.fork()
    if zombie == 0:
        os._exit(0)
    fd = None
    try:
        _await_zombie(zombie, threads=1)
        lock, holder = _flocked_holder_lock(tmp_path, zombie)
        log = _Log()
        try:
            ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
            assert ok is True and fd is not None
            assert log.has('warning', 'reclaiming stale lock pid=%d' % zombie)
        finally:
            holder.close()
    finally:
        if fd:
            fd.close()
        os.waitpid(zombie, 0)


@pytest.mark.skipif(
    not sys.platform.startswith('linux')
    or platform.machine() not in ('x86_64', 'AMD64'),
    reason='Linux x86-64 thread-exit syscall fixture')
def test_zombie_leader_with_a_running_thread_keeps_its_lock(srv, tmp_path):
    # Its descriptors (and flock) live until the last thread exits.
    child = subprocess.Popen([sys.executable, '-c', (
        'import ctypes, threading, time\n'
        'threading.Thread(target=time.sleep, args=(60,), daemon=True).start()\n'
        'ctypes.CDLL(None).syscall(60, 0)\n')])  # SYS_exit ends the leader only
    try:
        _await_zombie(child.pid, threads=2)
        lock, holder = _flocked_holder_lock(tmp_path, child.pid)
        log = _Log()
        try:
            ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
            assert ok is False and fd is None
            assert log.has('critical', 'held by a LIVE local server')
            assert os.path.exists(lock)
        finally:
            holder.close()
    finally:
        child.kill()
        child.wait(timeout=5)


# ── (a) local dead pid → reclaim succeeds ────────────────────────────────────

def test_reclaims_stale_local_dead_pid(srv, tmp_path):
    lock = str(tmp_path / '.server.lock')
    _write_lock(lock, _dead_pid(), 'thishost')
    log = _Log()

    # Simulate the orphan holding the flock: an fd on the OLD inode, still locked.
    import fcntl
    orphan = open(lock, 'r+')
    fcntl.flock(orphan.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
    try:
        assert ok is True and fd is not None
        assert log.has('warning', 'reclaiming stale lock')
        assert log.has('info', 'reclaimed stale lock and acquired')
        # A fresh inode was created (different from the orphan's now-unlinked one).
        assert os.stat(lock).st_ino != os.fstat(orphan.fileno()).st_ino
        # The reclaimed lock is stamped with our pid.
        with open(lock) as f:
            assert f.readline().strip() == '%d@thishost' % os.getpid()
    finally:
        if fd:
            fd.close()
        orphan.close()


# ── (b) foreign host → refuse, never unlink ──────────────────────────────────

def test_refuses_foreign_host_and_keeps_lock(srv, tmp_path):
    lock = str(tmp_path / '.server.lock')
    _write_lock(lock, 999999, 'otherhost')
    log = _Log()

    import fcntl
    holder = open(lock, 'r+')
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ino_before = os.stat(lock).st_ino

    ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
    try:
        assert ok is False and fd is None
        assert log.has('critical', 'held by another host')
        # The foreign lock file was NOT unlinked (same inode, contents intact).
        assert os.stat(lock).st_ino == ino_before
        with open(lock) as f:
            assert 'otherhost' in f.readline()
    finally:
        holder.close()


# ── (c) live local server.py pid → refuse ────────────────────────────────────

def test_refuses_live_local_server(srv, tmp_path):
    lock = str(tmp_path / '.server.lock')
    # Our own pid is alive and this test process's cmdline contains 'server.py'?
    # Not necessarily (pytest), so stamp our pid and force the cmdline check.
    _write_lock(lock, os.getpid(), 'thishost')
    log = _Log()

    import fcntl
    holder = open(lock, 'r+')
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Make _pid_is_live_server report True for our pid regardless of the test
    # runner's actual cmdline (we are simulating a live server owner).
    orig = srv._pid_is_live_server
    srv._pid_is_live_server = lambda pid: pid == os.getpid()
    try:
        ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost')
        assert ok is False and fd is None
        assert log.has('critical', 'held by a LIVE local server')
        assert os.path.exists(lock)  # not unlinked
    finally:
        srv._pid_is_live_server = orig
        holder.close()


# ── helper-level checks ──────────────────────────────────────────────────────

def test_pid_is_live_server_dead_pid(srv):
    assert srv._pid_is_live_server(_dead_pid()) is False


def test_read_lock_entry_variants(srv, tmp_path):
    lock = str(tmp_path / 'l')
    _write_lock(lock, 4321, 'h1')
    assert srv._read_instance_lock_entry(lock) == (4321, 'h1')

    with open(lock, 'w') as f:
        f.write('garbage-no-at\n')
    assert srv._read_instance_lock_entry(lock) == (None, None)

    with open(lock, 'w') as f:
        f.write('notanint@h2\n')
    assert srv._read_instance_lock_entry(lock) == (None, 'h2')

    assert srv._read_instance_lock_entry(str(tmp_path / 'missing')) == (None, None)


# ── NEUTER: blind flock (no reclaim) must make case (a) go red ───────────────

def test_neuter_blind_flock_fails_case_a(srv, tmp_path):
    """Prove the reclaim logic is load-bearing: with allow_reclaim=False (the
    old blind-flock behaviour), a stale-local-dead-pid lock held by an orphan
    fd is NOT reclaimed and acquisition FAILS."""
    lock = str(tmp_path / '.server.lock')
    _write_lock(lock, _dead_pid(), 'thishost')
    log = _Log()

    import fcntl
    orphan = open(lock, 'r+')
    fcntl.flock(orphan.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ok, fd = srv._acquire_instance_lock(lock, log, hostname='thishost',
                                            allow_reclaim=False)
        assert ok is False and fd is None
        assert not log.has('warning', 'reclaiming stale lock')
    finally:
        orphan.close()
