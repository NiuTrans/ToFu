#!/usr/bin/env python3
"""Tests for the pgdata copy/move self-heal mechanism.

Reproduces the report: copying ``data/pgdata/`` to a NEW path (a colleague's
home, ``tofu-meituan2``, an open-source clone) used to drag the ownership
markers along, so the fresh instance silently routed every DB call back to
the ORIGINAL machine's PostgreSQL over FUSE.

The fix stamps the pgdata's canonical path into ``.pg_instance_id`` whenever
this process takes local ownership. On a later startup at a DIFFERENT path,
``_heal_if_copied`` detects the mismatch and clears the inherited markers so
the copy starts/owns PG locally.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _markers(pgdata, owner_ip='10.0.0.9'):
    """Lay down a full set of inherited ownership markers."""
    with open(os.path.join(pgdata, '.pg_owner_host'), 'w') as f:
        f.write(owner_ip)
    with open(os.path.join(pgdata, '.tofu_heartbeat'), 'w') as f:
        json.dump({'host': owner_ip, 'pid': 4242, 'ts': 9e9}, f)  # far-future = fresh
    with open(os.path.join(pgdata, 'postmaster.pid'), 'w') as f:
        f.write(f'4242\n{pgdata}\n1780000000\n15439\n')


# ── Instance stamp ────────────────────────────────────────────────────

def test_stamp_written_and_read(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    b._write_instance_stamp(pgdata)
    stamp = b._read_instance_stamp(pgdata)
    assert stamp is not None
    assert b._canonical_pgdata_path(stamp['path']) == b._canonical_pgdata_path(pgdata)
    assert stamp['id']


def test_stamp_stable_id_on_restamp_same_path(tmp_path):
    """Re-stamping the same path keeps the original id (idempotent)."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    b._write_instance_stamp(pgdata)
    first = b._read_instance_stamp(pgdata)['id']
    b._write_instance_stamp(pgdata)
    assert b._read_instance_stamp(pgdata)['id'] == first


def test_missing_stamp_is_not_a_copy(tmp_path):
    """Legacy pgdata with no stamp must NOT be flagged as copied."""
    from lib.database import _bootstrap as b
    was_copied, stamped = b._pgdata_was_copied(str(tmp_path))
    assert was_copied is False


# ── Copy detection ──────────────────────────────────────────────────────

def test_copy_detected_when_path_differs(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    # Stamp claims the directory was created at a DIFFERENT path.
    with open(os.path.join(pgdata, '.pg_instance_id'), 'w') as f:
        json.dump({'path': '/somewhere/else/data/pgdata', 'id': 'abc',
                   'created': 1.0}, f)
    was_copied, stamped = b._pgdata_was_copied(pgdata)
    assert was_copied is True
    assert stamped == '/somewhere/else/data/pgdata'


def test_no_copy_when_path_matches(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    b._write_instance_stamp(pgdata)  # stamps the real current path
    was_copied, _ = b._pgdata_was_copied(pgdata)
    assert was_copied is False


# ── Heal + Step-3 integration ───────────────────────────────────────────

def test_heal_clears_markers_on_copy(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata)
    with open(os.path.join(pgdata, '.pg_instance_id'), 'w') as f:
        json.dump({'path': '/original/pgdata', 'id': 'x', 'created': 1.0}, f)

    assert b._heal_if_copied(pgdata) is True
    # owner_host + heartbeat cleared; pidfile left for PG's own guard.
    assert not os.path.exists(os.path.join(pgdata, '.pg_owner_host'))
    assert not os.path.exists(os.path.join(pgdata, '.tofu_heartbeat'))


def test_heal_noop_when_not_copied(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata)
    b._write_instance_stamp(pgdata)  # same path → not copied
    assert b._heal_if_copied(pgdata) is False
    assert os.path.exists(os.path.join(pgdata, '.pg_owner_host'))


def test_step3_reports_no_remote_when_copied(tmp_path, monkeypatch):
    """The key regression: a copied pgdata must NOT defer to the source PG."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata, owner_ip='10.0.0.9')
    with open(os.path.join(pgdata, '.pg_instance_id'), 'w') as f:
        json.dump({'path': '/original/pgdata', 'id': 'x', 'created': 1.0}, f)

    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.0.0.50')
    is_remote, host = b._pg_already_running_on_another_machine(pgdata, 15439)
    assert is_remote is False
    assert host is None


def test_step3_still_defers_for_same_path_remote(tmp_path, monkeypatch):
    """Same-path multi-host failover must be preserved (no false heal)."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata, owner_ip='10.0.0.9')
    b._write_instance_stamp(pgdata)  # stamp matches current path → not a copy

    monkeypatch.setattr(b, '_get_local_ip', lambda: '10.0.0.50')
    # PID is alive but NOT a local postgres (it's the remote owner).
    monkeypatch.setattr('lib.compat.is_process_alive', lambda pid: True)
    monkeypatch.setattr('lib.compat.is_process_named', lambda pid, name: False)
    monkeypatch.setattr(b, '_pg_real_connect_ok',
                        lambda *a, **k: True)
    is_remote, host = b._pg_already_running_on_another_machine(pgdata, 15439)
    assert is_remote is True
    assert host == '10.0.0.9'


# ── Marker clearing helper ──────────────────────────────────────────────

def test_clear_ownership_markers_keeps_data(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata)
    os.makedirs(os.path.join(pgdata, 'base'))  # stand-in for data
    with open(os.path.join(pgdata, 'PG_VERSION'), 'w') as f:
        f.write('17\n')

    removed = b._clear_ownership_markers(pgdata, remove_pidfile=True)
    assert set(removed) >= {'.pg_owner_host', '.tofu_heartbeat', 'postmaster.pid'}
    assert os.path.isdir(os.path.join(pgdata, 'base'))
    assert os.path.isfile(os.path.join(pgdata, 'PG_VERSION'))


# ── CLI ─────────────────────────────────────────────────────────────────

def test_cli_status_runs_on_copied(tmp_path, capsys):
    from lib.database import pg_admin
    pgdata = str(tmp_path)
    _markers(pgdata)
    with open(os.path.join(pgdata, '.pg_instance_id'), 'w') as f:
        json.dump({'path': '/original/pgdata', 'id': 'x', 'created': 1.0}, f)

    rc = pg_admin.main(['--pgdata', pgdata, 'status'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'COPIED' in out


def test_cli_reset_ownership_clears(tmp_path, capsys, monkeypatch):
    from lib.database import pg_admin
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata)
    # Pidfile is not a live local postgres → reset is allowed.
    monkeypatch.setattr(b, '_pidfile_pid_is_live_local_postgres', lambda pg: False)

    rc = pg_admin.main(['--pgdata', pgdata, 'reset-ownership', '--yes'])
    assert rc == 0
    assert not os.path.exists(os.path.join(pgdata, '.pg_owner_host'))
    assert not os.path.exists(os.path.join(pgdata, 'postmaster.pid'))


def test_cli_reset_refuses_live_local_pg(tmp_path, monkeypatch):
    from lib.database import pg_admin
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path)
    _markers(pgdata)
    monkeypatch.setattr(b, '_pidfile_pid_is_live_local_postgres', lambda pg: True)

    rc = pg_admin.main(['--pgdata', pgdata, 'reset-ownership', '--yes'])
    assert rc == 2
    # Refused → markers untouched.
    assert os.path.exists(os.path.join(pgdata, '.pg_owner_host'))
