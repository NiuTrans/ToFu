#!/usr/bin/env python3
"""Tests for the cross-host PG startup flock (anti-double-start interlock).

The startup lock (`_try_acquire_startup_lock` / `_release_startup_lock`) is the
LAST-LINE, IP/PID-heuristic-independent barrier: a live peer holds an
exclusive flock on `pgdata/.tofu_pg_start.lock` for its entire lifetime, so
even if every ownership heuristic is wrong, a second host physically cannot
acquire the lock and therefore must NOT delete the pidfile or start a second
postmaster on the shared FUSE pgdata (which corrupts pg_subtrans).

These tests exercise the lock primitive itself and the cross-process
enforcement. The takeover wiring (acquire-before-pidfile-removal) is covered
indirectly: the same primitive gates it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def test_acquire_then_release_is_reentrant(tmp_path):
    """Acquiring twice in the same process is a no-op success; release frees it."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    try:
        assert b._try_acquire_startup_lock(pgdata) is True
        # Second acquire in the same process returns True without re-locking.
        assert b._try_acquire_startup_lock(pgdata) is True
    finally:
        b._release_startup_lock()
    # Release is idempotent — safe to call again.
    b._release_startup_lock()


def test_lock_file_created_in_pgdata(tmp_path):
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    try:
        assert b._try_acquire_startup_lock(pgdata) is True
        assert os.path.exists(os.path.join(pgdata, b._STARTUP_LOCK_FILE))
    finally:
        b._release_startup_lock()


def test_second_process_cannot_acquire_held_lock(tmp_path):
    """The core guarantee: a child process is BLOCKED while we hold the lock.

    This is what makes two hosts physically unable to both start a postmaster
    on the same FUSE pgdata. We simulate the second host with a forked child
    that runs its own fresh interpreter state for the lock module-global.
    """
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    assert b._try_acquire_startup_lock(pgdata) is True
    try:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child = "second host"
            os.close(r)
            # Reset the module-global so the child genuinely re-attempts the
            # flock rather than seeing the inherited fd as "already held".
            b._startup_lock_fd = None
            got = b._try_acquire_startup_lock(pgdata)
            os.write(w, b'1' if got else b'0')
            os.close(w)
            os._exit(0)
        os.close(w)
        result = os.read(r, 1)
        os.close(r)
        os.waitpid(pid, 0)
        # Child must have been refused (False) — parent holds the exclusive lock.
        assert result == b'0', 'child acquired a lock the parent already holds — flock not enforced'
    finally:
        b._release_startup_lock()


def test_lock_released_lets_next_acquirer_in(tmp_path):
    """After release, a fresh acquire (simulating a later process) succeeds."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    assert b._try_acquire_startup_lock(pgdata) is True
    b._release_startup_lock()

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        b._startup_lock_fd = None
        got = b._try_acquire_startup_lock(pgdata)
        os.write(w, b'1' if got else b'0')
        os.close(w)
        os._exit(0)
    os.close(w)
    result = os.read(r, 1)
    os.close(r)
    os.waitpid(pid, 0)
    assert result == b'1', 'lock was not released — next acquirer blocked'



def _reset_probe_cache(b):
    b._flock_enforced = None


def test_probe_detects_real_flock_enforcement(tmp_path):
    """On a real locking FS (tmp_path is local), the probe reports True."""
    from lib.database import _bootstrap as b

    _reset_probe_cache(b)
    assert b._probe_flock_enforced(str(tmp_path)) is True
    # Result is cached and leaves no probe file behind.
    assert b._probe_flock_enforced(str(tmp_path)) is True
    assert not os.path.exists(os.path.join(str(tmp_path), '.tofu_flock_probe'))


def test_probe_cached_result_is_reused(tmp_path, monkeypatch):
    """Once probed, the cached verdict is returned without re-running."""
    from lib.database import _bootstrap as b

    _reset_probe_cache(b)
    b._flock_enforced = False  # pretend a prior probe found a no-op FS
    # If it re-probed tmp_path (real FS) it'd return True; cache must win.
    assert b._probe_flock_enforced(str(tmp_path)) is False


def test_require_flock_policy_env(monkeypatch):
    from lib.database import _bootstrap as b

    monkeypatch.delenv('TOFU_PG_REQUIRE_FLOCK', raising=False)
    monkeypatch.delenv('CHATUI_PG_REQUIRE_FLOCK', raising=False)
    assert b._flock_required() is False

    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert b._flock_required() is True
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', 'refuse')
    assert b._flock_required() is True
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '0')
    assert b._flock_required() is False

    # Legacy var honored.
    monkeypatch.setenv('CHATUI_PG_REQUIRE_FLOCK', 'yes')
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '')
    assert b._flock_required() is True


def test_unenforced_flock_warns_but_proceeds_by_default(tmp_path, monkeypatch):
    """Silent-no-op FS + default policy → proceed (return True) with a warning."""
    from lib.database import _bootstrap as b

    _reset_probe_cache(b)
    monkeypatch.setattr(b, '_probe_flock_enforced', lambda pgdata: False)
    monkeypatch.delenv('TOFU_PG_REQUIRE_FLOCK', raising=False)
    monkeypatch.delenv('CHATUI_PG_REQUIRE_FLOCK', raising=False)
    assert b._verify_flock_support_or_warn(str(tmp_path)) is True


def test_unenforced_flock_refuses_when_required(tmp_path, monkeypatch):
    """Silent-no-op FS + TOFU_PG_REQUIRE_FLOCK=1 → refuse PG (return False)."""
    from lib.database import _bootstrap as b

    _reset_probe_cache(b)
    monkeypatch.setattr(b, '_probe_flock_enforced', lambda pgdata: False)
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert b._verify_flock_support_or_warn(str(tmp_path)) is False


def test_unverifiable_flock_refuses_when_required(tmp_path, monkeypatch):
    """Probe-inconclusive (None) + required policy → refuse (conservative)."""
    from lib.database import _bootstrap as b

    _reset_probe_cache(b)
    monkeypatch.setattr(b, '_probe_flock_enforced', lambda pgdata: None)
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert b._verify_flock_support_or_warn(str(tmp_path)) is False
