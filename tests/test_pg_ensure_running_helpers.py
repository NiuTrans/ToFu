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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
