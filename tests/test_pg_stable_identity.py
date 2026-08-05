#!/usr/bin/env python3
"""Tests for the IP-flap-proof PG ownership fixes (2026-06-21).

Scenario: a project exported on host A (zw) runs on host B (shxs); both share
ONE FUSE-mounted pgdata at the SAME absolute path, but the container IP flaps
while the host stays the same. Two defects were fixed:

#1  Ownership was keyed on `_get_local_ip()` (a UDP-trick IP that flaps). After
    a flap the server read `.pg_owner_host` (old IP) != local_ip (new IP) and
    mistook its OWN pgdata for a remote one → deferred to a dead 127.0.0.1 DSN
    → "connection ... timeout expired". Fix: a STABLE `.pg_owner_id`
    (machine-id / hostname) is the authoritative self-ownership check.

#2  When the locally-owned PG stalled it raised `timeout expired`, which was
    NOT in `_PG_DEAD_SIGNATURES`, so the runtime self-heal never rebooted PG
    and every call spun on timeouts. Fix: add the timeout/connect-failure
    signatures (self-heal stays gated on is_pg_owned_locally()).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _write_pidfile(pgdata, pid, port=15432):
    with open(os.path.join(pgdata, 'postmaster.pid'), 'w') as f:
        f.write(f'{pid}\n{pgdata}\n1780000000\n{port}\n')


# ── #1: stable host identity ────────────────────────────────────────────

def test_host_identity_env_override(monkeypatch):
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'my-stable-host')
    assert b._get_host_identity() == 'my-stable-host'
    b._HOST_IDENTITY_CACHE = None


def test_host_identity_is_stable_across_ip_flap(monkeypatch):
    """Identity must not change when _get_local_ip() returns a new value."""
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'host-X')
    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.0.0.1')
    id1 = b._get_host_identity()
    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.99.99.99')  # flap
    id2 = b._get_host_identity()
    assert id1 == id2 == 'host-X'
    b._HOST_IDENTITY_CACHE = None


def test_owner_is_self_true_when_id_matches(tmp_path, monkeypatch):
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'host-A')
    pgdata = str(tmp_path)
    b._write_owner_host(pgdata)  # writes .pg_owner_host + .pg_owner_id
    assert os.path.exists(os.path.join(pgdata, '.pg_owner_id'))
    assert b._owner_is_self(pgdata) is True
    b._HOST_IDENTITY_CACHE = None


def test_owner_is_self_false_when_id_differs(tmp_path, monkeypatch):
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    with open(os.path.join(tmp_path, '.pg_owner_id'), 'w') as f:
        f.write('some-other-host')
    monkeypatch.setenv('TOFU_HOST_ID', 'host-A')
    assert b._owner_is_self(str(tmp_path)) is False
    b._HOST_IDENTITY_CACHE = None


def test_owner_is_self_none_when_marker_absent(tmp_path, monkeypatch):
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'host-A')
    assert b._owner_is_self(str(tmp_path)) is None
    b._HOST_IDENTITY_CACHE = None


def test_step3_not_remote_when_identity_matches_despite_ip_flap(tmp_path, monkeypatch):
    """The core fix: stable id says OURS → not remote, even though the IP
    flapped AND the live-PID probe would say 'not postgres'."""
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'host-A')
    pgdata = str(tmp_path)
    # Owner IP is stale (the flap), but the identity marker is ours.
    with open(os.path.join(pgdata, '.pg_owner_host'), 'w') as f:
        f.write('10.20.49.98')
    with open(os.path.join(pgdata, '.pg_owner_id'), 'w') as f:
        f.write('host-A')
    _write_pidfile(pgdata, 4242)
    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.128.197.3')  # flapped
    monkeypatch.delenv('TOFU_PG_STANDALONE', raising=False)

    is_remote, host = b._pg_already_running_on_another_machine(pgdata, 15432)
    assert is_remote is False
    assert host is None
    b._HOST_IDENTITY_CACHE = None


def test_step3_remote_when_identity_differs(tmp_path, monkeypatch):
    """A genuinely different-host identity marker → defer to remote, even if
    the flapping IPs happen to coincide with ours."""
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_HOST_ID', 'host-B')  # we are B
    pgdata = str(tmp_path)
    with open(os.path.join(pgdata, '.pg_owner_host'), 'w') as f:
        f.write('10.128.197.3')   # coincidentally same as our local_ip
    with open(os.path.join(pgdata, '.pg_owner_id'), 'w') as f:
        f.write('host-A')          # but identity says host A owns it
    _write_pidfile(pgdata, 4242)
    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.128.197.3')
    monkeypatch.setattr(b, '_pidfile_pid_is_live_local_postgres', lambda pg: False)
    monkeypatch.setattr(b, '_pg_real_connect_ok', lambda *a, **k: True)
    monkeypatch.delenv('TOFU_PG_STANDALONE', raising=False)

    is_remote, host = b._pg_already_running_on_another_machine(pgdata, 15432)
    assert is_remote is True
    assert host == 'host-A' or host == '10.128.197.3' or host is not None
    b._HOST_IDENTITY_CACHE = None


def test_standalone_heal_skipped_when_identity_is_self(tmp_path, monkeypatch):
    """Standalone heal must NOT clear markers when .pg_owner_id is ours
    (an IP flap is not an inherited remote marker)."""
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    b._HOST_IDENTITY_CACHE = None
    monkeypatch.setenv('TOFU_PG_STANDALONE', '1')
    monkeypatch.setenv('TOFU_HOST_ID', 'host-A')
    pgdata = str(tmp_path)
    with open(os.path.join(pgdata, '.pg_owner_host'), 'w') as f:
        f.write('10.20.49.98')     # stale IP
    with open(os.path.join(pgdata, '.pg_owner_id'), 'w') as f:
        f.write('host-A')          # ours
    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.128.197.3')

    assert b._heal_if_standalone_remote_owner(pgdata) is False
    assert os.path.exists(os.path.join(pgdata, '.pg_owner_host'))
    b._HOST_IDENTITY_CACHE = None


def test_clear_ownership_markers_removes_owner_id(tmp_path):
    from lib.database import _pg_ownership as b  # facade: canonical home of _HOST_IDENTITY_CACHE (_hostid.py)
    pgdata = str(tmp_path)
    for name in ('.pg_owner_host', '.pg_owner_id', b._HEARTBEAT_FILE):
        with open(os.path.join(pgdata, name), 'w') as f:
            f.write('x')
    removed = b._clear_ownership_markers(pgdata, remove_pidfile=False)
    assert '.pg_owner_id' in removed
    assert not os.path.exists(os.path.join(pgdata, '.pg_owner_id'))


# ── #2: timeout self-heal signatures ─────────────────────────────────────

@pytest.mark.parametrize('errtxt', [
    'connection to server at "127.0.0.1", port 15432 failed: timeout expired',
    'could not connect to server: Connection timed out',
    'Operation timed out',
    'Connection refused',
    'server closed the connection unexpectedly',
])
def test_timeout_is_dead_signature(errtxt):
    from lib.database import _core as c
    assert c._pg_error_is_dead(errtxt) is True


@pytest.mark.parametrize('errtxt', [
    'FATAL: password authentication failed for user "x"',
    'role "y" does not exist',
    'syntax error at or near "SELCT"',
    '',
])
def test_non_dead_errors_do_not_trigger_self_heal(errtxt):
    from lib.database import _core as c
    assert c._pg_error_is_dead(errtxt) is False


def test_timeout_is_not_zombie(errtxt='connection ... timeout expired'):
    """A timeout is 'dead' (reboot) but NOT a 'zombie' (no force-stop needed)."""
    from lib.database import _core as c
    assert c._pg_error_is_zombie('timeout expired') is False
    assert c._pg_error_is_zombie('could not open shared memory segment') is True
