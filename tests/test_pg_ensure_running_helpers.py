#!/usr/bin/env python3
"""Unit tests for the helpers extracted from ``_ensure_pg_running`` (2026-06-24).

The 486-line ``_ensure_pg_running`` was decomposed by lifting two self-contained,
side-effect-light steps into named helpers:

  * ``_pgdata_major_compatible(pgdata) -> bool`` — Step 3b: refuse to start when
    the pgdata major version can't be served by the local postgres binary.
    Previously UNTESTED inline code — these tests are the net for the lift.
  * ``_try_explicit_pg_target(...) -> (handled, result)`` — Step 1: the explicit
    env-target branch (already covered end-to-end by
    test_pg_explicit_local_autostart.py; here we pin the (handled, result)
    contract directly).

These are pure-ish: ``_pgdata_major_compatible`` only reads a file + shells
``postgres --version``; ``_try_explicit_pg_target`` reads env + (optionally)
connects. All subprocess / connect calls are monkeypatched so the tests stay
fast and offline.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  _pgdata_major_compatible (Step 3b)
# ══════════════════════════════════════════════════════════

def _write_pg_version(pgdata, major):
    os.makedirs(pgdata, exist_ok=True)
    with open(os.path.join(pgdata, 'PG_VERSION'), 'w') as f:
        f.write(f'{major}\n')


def test_no_pg_version_file_is_compatible(tmp_path):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    os.makedirs(pgdata, exist_ok=True)
    # No PG_VERSION file → proceed (True).
    assert b._pgdata_major_compatible(pgdata) is True


def test_matching_major_is_compatible(tmp_path, monkeypatch):
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _write_pg_version(pgdata, '17')

    import subprocess

    class _Out:
        returncode = 0
        stdout = 'postgres (PostgreSQL) 17.2'
        stderr = ''
    monkeypatch.setattr(b, '_find_pg_binary', lambda name: '/usr/bin/postgres')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Out())
    assert b._pgdata_major_compatible(pgdata) is True


def test_mismatched_major_is_incompatible(tmp_path, monkeypatch):
    """pgdata major 18 vs binary major 17 → False (caller bails to SQLite)."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _write_pg_version(pgdata, '18')

    import subprocess

    class _Out:
        returncode = 0
        stdout = 'postgres (PostgreSQL) 17.2'
        stderr = ''
    monkeypatch.setattr(b, '_find_pg_binary', lambda name: '/usr/bin/postgres')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Out())
    assert b._pgdata_major_compatible(pgdata) is False


def test_no_postgres_binary_is_incompatible(tmp_path, monkeypatch):
    """FileNotFoundError from the version probe → False (can't verify → bail)."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _write_pg_version(pgdata, '17')

    def _boom(name):
        raise FileNotFoundError('postgres')
    monkeypatch.setattr(b, '_find_pg_binary', _boom)
    assert b._pgdata_major_compatible(pgdata) is False


def test_probe_error_is_nonfatal_compatible(tmp_path, monkeypatch):
    """A non-FileNotFound probe error is non-fatal → True (let normal flow try)."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _write_pg_version(pgdata, '17')

    import subprocess

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd='postgres', timeout=5)
    monkeypatch.setattr(b, '_find_pg_binary', lambda name: '/usr/bin/postgres')
    monkeypatch.setattr(subprocess, 'run', _timeout)
    assert b._pgdata_major_compatible(pgdata) is True


# ══════════════════════════════════════════════════════════
#  _try_explicit_pg_target (Step 1) — (handled, result) contract
# ══════════════════════════════════════════════════════════

def _dsn(host, port):
    return f'host={host} port={port} dbname=tofu'


def test_no_explicit_target_falls_through(tmp_path, monkeypatch):
    from lib.database import _bootstrap as b
    monkeypatch.delenv('TOFU_PG_HOST', raising=False)
    monkeypatch.delenv('TOFU_PG_PORT', raising=False)
    handled, result = b._try_explicit_pg_target(
        str(tmp_path), str(tmp_path), '127.0.0.1', 15432, _dsn)
    assert handled is False and result is None


def test_explicit_reachable_returns_result(tmp_path, monkeypatch):
    from lib.database import _bootstrap as b
    monkeypatch.setenv('TOFU_PG_HOST', '10.1.2.3')
    monkeypatch.setenv('TOFU_PG_PORT', '15439')

    psycopg2 = pytest.importorskip('psycopg2')

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _Conn())
    handled, result = b._try_explicit_pg_target(
        str(tmp_path), str(tmp_path), '127.0.0.1', 15432, _dsn)
    assert handled is True
    assert result and result['PG_HOST'] == '10.1.2.3' and result['PG_PORT'] == 15439


def test_explicit_remote_unreachable_returns_handled_none(tmp_path, monkeypatch):
    from lib.database import _bootstrap as b
    psycopg2 = pytest.importorskip('psycopg2')
    monkeypatch.setenv('TOFU_PG_HOST', '10.99.99.99')
    monkeypatch.setenv('TOFU_PG_PORT', '15439')

    def _refused(*a, **k):
        raise psycopg2.OperationalError('refused')
    monkeypatch.setattr(psycopg2, 'connect', _refused)
    handled, result = b._try_explicit_pg_target(
        str(tmp_path), str(tmp_path), '10.99.99.99', 15439, _dsn)
    # Genuinely remote + unreachable → handled (caller returns None, no local start).
    assert handled is True and result is None


def test_explicit_local_ours_down_falls_through(tmp_path, monkeypatch):
    """Unreachable explicit-LOCAL target naming OUR pgdata → fall through to start."""
    from lib.database import _bootstrap as b
    psycopg2 = pytest.importorskip('psycopg2')
    pgdata = str(tmp_path / 'pgdata')
    os.makedirs(pgdata, exist_ok=True)
    with open(os.path.join(pgdata, 'postgresql.conf'), 'w') as f:
        f.write('port = 15439\n')

    monkeypatch.setenv('TOFU_PG_PORT', '15439')
    monkeypatch.delenv('TOFU_PG_HOST', raising=False)
    monkeypatch.setattr(b, '_pg_binaries_present', lambda: True)

    def _refused(*a, **k):
        raise psycopg2.OperationalError('refused')
    monkeypatch.setattr(psycopg2, 'connect', _refused)
    handled, result = b._try_explicit_pg_target(
        pgdata, str(tmp_path), '127.0.0.1', 15439, _dsn)
    # Ours + down → fall through (caller proceeds to local start), NOT a hard return.
    assert handled is False and result is None


# ══════════════════════════════════════════════════════════
#  initdb gate — must key on POPULATED, not mere dir existence
#  (regression: the flock probe + startup lock pre-create an EMPTY
#   pgdata, which used to defeat `if not os.path.isdir(pgdata)` and
#   silently fall back to SQLite on a fresh local-split path)
# ══════════════════════════════════════════════════════════

def _stub_prestart_helpers(b, monkeypatch):
    """Make _ensure_pg_running reach the initdb gate offline + deterministically.

    Stubs every step BEFORE the gate so no real PG / network / port scan runs,
    and reproduces the real-world side effect that caused the bug: the flock
    probe / startup lock creating the pgdata directory before the gate.
    """
    monkeypatch.delenv('TOFU_PG_HOST', raising=False)
    monkeypatch.delenv('TOFU_PG_PORT', raising=False)

    # _ensure_pg_running lives in lib.database._bootstrap._orchestrate, which
    # binds every helper with a top-level ``from ... import ...``. Patching the
    # facade package alone therefore does NOT reach the call sites — it only
    # worked on hosts where the REAL helpers happened to behave like the stubs
    # (a dev box with PG binaries passes the Step-0 `_pg_binaries_present`
    # gate; a CI box without any bails to SQLite before the initdb gate).
    # Patch BOTH namespaces so the test is hermetic on every host.
    from lib.database._bootstrap import _orchestrate as orch
    for target in (b, orch):
        monkeypatch.setattr(target, '_pg_binaries_present', lambda: True)
        monkeypatch.setattr(target, '_try_explicit_pg_target',
                            lambda *a, **k: (False, None))
        monkeypatch.setattr(target, '_read_our_pg_port', lambda pgdata: None)
        monkeypatch.setattr(target, '_scan_for_our_pg', lambda *a, **k: None)
        monkeypatch.setattr(target, '_pg_already_running_on_another_machine',
                            lambda *a, **k: (False, None))
        monkeypatch.setattr(target, '_pgdata_major_compatible',
                            lambda pgdata: True)

    # The real _verify_flock_support_or_warn → _probe_flock_enforced does
    # os.makedirs(pgdata); reproduce that side effect (create the dir) and
    # return True so the flow proceeds to the gate.
    def _flock_creates_dir(pgdata):
        os.makedirs(pgdata, exist_ok=True)
        return True
    monkeypatch.setattr(b, '_verify_flock_support_or_warn', _flock_creates_dir)
    monkeypatch.setattr(orch, '_verify_flock_support_or_warn', _flock_creates_dir)


def test_empty_pgdata_dir_still_triggers_bootstrap(tmp_path, monkeypatch):
    """An existing but EMPTY pgdata (pre-created by the flock probe) must route
    to _bootstrap_pg — not fall through to pg_ctl start on a non-cluster dir."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _stub_prestart_helpers(b, monkeypatch)

    from lib.database._bootstrap import _orchestrate as orch
    called = {}

    def _fake_bootstrap(pg, base, host, port, user, pw, db):
        called['pgdata'] = pg
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15432, 'PG_DSN': 'x'}
    monkeypatch.setattr(orch, '_bootstrap_pg', _fake_bootstrap)

    result = b._ensure_pg_running(pgdata, str(tmp_path), '127.0.0.1', 15432,
                                  '', '', 'tofu')
    # The gate saw an unpopulated dir → bootstrap ran (the fix).
    assert called.get('pgdata') == pgdata
    assert result and result['PG_PORT'] == 15432


def test_populated_pgdata_does_not_rerun_bootstrap(tmp_path, monkeypatch):
    """A populated cluster (PG_VERSION present) must NOT be re-initdb'd — the
    gate skips _bootstrap_pg and proceeds to the reuse/start path."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    _write_pg_version(pgdata, '18')  # populated
    _stub_prestart_helpers(b, monkeypatch)

    from lib.database._bootstrap import _orchestrate as orch

    def _boom_bootstrap(*a, **k):
        raise AssertionError('_bootstrap_pg must NOT run on a populated cluster')
    monkeypatch.setattr(orch, '_bootstrap_pg', _boom_bootstrap)
    # Stop the flow right after the gate so we don't try a real pg_ctl start;
    # a populated dir means the gate is passed and we enter the pidfile/start
    # path — force that to short-circuit cleanly.
    monkeypatch.setattr(b, '_fix_unix_socket_conf', lambda pgdata: None)
    import subprocess
    monkeypatch.setattr(subprocess, 'run',
                        lambda *a, **k: type('R', (), {'returncode': 1, 'stdout': '', 'stderr': 'stub'})())
    monkeypatch.setattr(b, '_try_acquire_startup_lock', lambda pgdata: False)

    # Should not raise (bootstrap not called); returns None via the
    # startup-lock-contended path — the point is _bootstrap_pg was skipped.
    b._ensure_pg_running(pgdata, str(tmp_path), '127.0.0.1', 15432, '', '', 'tofu')


def test_bootstrap_strips_tofu_artifacts_before_initdb(tmp_path, monkeypatch):
    """_bootstrap_pg must remove leftover .tofu* lock/probe files (left by the
    flock probe / startup lock) so initdb's empty-dir requirement is met, and
    must NOT touch any non-.tofu content."""
    from lib.database import _bootstrap as b
    pgdata = str(tmp_path / 'pgdata')
    os.makedirs(pgdata, exist_ok=True)
    # Artifacts the pre-start helpers leave behind:
    open(os.path.join(pgdata, '.tofu_pg_start.lock'), 'w').close()
    open(os.path.join(pgdata, '.tofu_flock_probe'), 'w').close()

    seen = {}

    def _fake_initdb_run(cmd, *a, **k):
        # At the moment initdb is invoked, our .tofu artifacts must be gone.
        seen['leftover_tofu'] = [n for n in os.listdir(pgdata)
                                 if n.startswith('.tofu')]
        # Fail fast right after so we don't proceed into real start/createdb.
        return type('R', (), {'returncode': 1, 'stdout': '', 'stderr': 'stub-stop'})()

    from lib.database._bootstrap import _orchestrate as orch
    monkeypatch.setattr(orch, '_find_pg_binary', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(orch, '_get_username', lambda: 'tofu')
    import subprocess
    monkeypatch.setattr(subprocess, 'run', _fake_initdb_run)

    orch._bootstrap_pg(pgdata, str(tmp_path), '127.0.0.1', 15432, '', '', 'tofu')
    assert seen.get('leftover_tofu') == [], (
        'initdb was invoked with leftover .tofu artifacts still present')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
