#!/usr/bin/env python3
"""Regression tests for the PG IP-flap split-brain guard.

Reproduces the 2026-06-06 incident: a host's own IP was reassigned by the
cloud-IDE network (10.114.x → 10.148.x), so bootstrap read the
`.pg_owner_host` marker (old IP), concluded its OWN postmaster belonged to a
"remote" host, deleted postmaster.pid, and started a SECOND postmaster on the
same FUSE-mounted pgdata — corrupting pg_subtrans.

The fix (`_pidfile_pid_is_live_local_postgres`) makes a live local postgres
the IP-independent ground truth for "this host already owns pgdata", short-
circuiting the destructive takeover regardless of how the IP flaps.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _write_pidfile(pgdata, pid):
    with open(os.path.join(pgdata, 'postmaster.pid'), 'w') as f:
        # PG's postmaster.pid: first line is the PID; remaining lines are
        # data dir, start time, port, etc. Only the first line matters here.
        f.write(f'{pid}\n{pgdata}\n1780000000\n15439\n')


def test_live_local_postgres_detected_despite_ip_flap(tmp_path, monkeypatch):
    """A live local postgres PID short-circuits remote-owner detection."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    # The marker claims a DIFFERENT IP than this host (the flap).
    with open(os.path.join(pgdata, '.pg_owner_host'), 'w') as f:
        f.write('10.148.172.131')
    _write_pidfile(pgdata, 424242)

    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.114.81.191')
    # Simulate "PID 424242 is a live local postgres".
    monkeypatch.setattr('lib.compat.is_process_alive', lambda pid: True)
    monkeypatch.setattr('lib.compat.is_process_named',
                        lambda pid, name: name == 'postgres')

    assert b._pidfile_pid_is_live_local_postgres(pgdata) is True

    # Step 3 must NOT report a remote owner — it's actually us.
    is_remote, host = b._pg_already_running_on_another_machine(pgdata, 15439)
    assert is_remote is False
    assert host is None


def test_dead_pid_is_not_treated_as_owner(tmp_path, monkeypatch):
    """A stale pidfile (dead PID) is correctly NOT treated as a live owner."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    _write_pidfile(pgdata, 999999)
    monkeypatch.setattr('lib.compat.is_process_alive', lambda pid: False)

    assert b._pidfile_pid_is_live_local_postgres(pgdata) is False


def test_live_pid_but_not_postgres_is_stale(tmp_path, monkeypatch):
    """A live PID that is NOT postgres (recycled PID) is a stale pidfile."""
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    _write_pidfile(pgdata, 12345)
    monkeypatch.setattr('lib.compat.is_process_alive', lambda pid: True)
    monkeypatch.setattr('lib.compat.is_process_named', lambda pid, name: False)

    assert b._pidfile_pid_is_live_local_postgres(pgdata) is False


def test_missing_pidfile_returns_false(tmp_path):
    """No postmaster.pid → not a live owner (and no crash)."""
    from lib.database import _bootstrap as b

    assert b._pidfile_pid_is_live_local_postgres(str(tmp_path)) is False


def test_name_check_failure_assumes_live_postgres(tmp_path, monkeypatch):
    """If the process-name probe raises, we SAFELY assume live postgres.

    Better to skip a takeover than to risk a double-start that corrupts the
    cluster — the whole point of the guard.
    """
    from lib.database import _bootstrap as b

    pgdata = str(tmp_path)
    _write_pidfile(pgdata, 77777)
    monkeypatch.setattr('lib.compat.is_process_alive', lambda pid: True)

    def _boom(pid, name):
        raise PermissionError('no /proc access')

    monkeypatch.setattr('lib.compat.is_process_named', _boom)

    assert b._pidfile_pid_is_live_local_postgres(pgdata) is True
