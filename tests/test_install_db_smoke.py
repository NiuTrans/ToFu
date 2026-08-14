#!/usr/bin/env python3
"""Guard tests: install.sh's post-install DB smoke test.

Background — the "SQLite is our default but is it actually usable on THIS
box?" concern (2026-07):
  After the SQLite-default + uv-fast-path optimization, the installer picks a
  DB backend but never PROVED the chosen backend could actually open, create a
  table, and read/write. The smoke test closes that gap: it runs an
  authenticated Sidecar write → read → delete round-trip through the same
  storage boundary and resolved DB target the server will use, and ABORTS the
  install (fail) if it can't — giving the default SQLite user PG-grade
  confidence without paying conda's cost.

  These tests pin the contract by static analysis (no network, no DB, no
  server): the step exists in the shared tail (covers both uv + conda),
  runs the full round-trip via $ENV_PYTHON, leaves no probe record, ABORTS
  (fail, not warn) on failure, and its backend hint never tells a failed-PG
  user to switch to PG.
"""

import os
import re
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _install_sh() -> str:
    with open(os.path.join(ROOT, 'install.sh'), 'r', encoding='utf-8') as f:
        return f.read()


def _smoke_block(text: str) -> str:
    """The install.sh region from the smoke step to the next step marker."""
    start = text.index('step "Verifying the storage Sidecar works')
    tail = text[start:]
    nxt = re.search(r'\nstep "', tail[10:])
    end = start + 10 + (nxt.start() if nxt else len(tail) - 10)
    return text[start:end]


def test_smoke_step_exists_after_env_before_launch():
    """The smoke step must sit AFTER `.env ready` and BEFORE Step 10/launch, so
    it runs on the shared tail (covering both the uv and conda paths)."""
    text = _install_sh()
    env_idx = text.index('ok ".env ready (PORT=${PORT})"')
    smoke_idx = text.index('step "Verifying the storage Sidecar works')
    launch_idx = text.index('#  Step 10: Launch or print completion')
    assert env_idx < smoke_idx < launch_idx, \
        'DB smoke step is not positioned between .env-ready and Step 10 launch'
    _ok('smoke step runs on the shared tail (after .env, before launch)')


def test_smoke_runs_full_round_trip():
    """The heredoc drives the authenticated storage boundary end to end."""
    block = _smoke_block(_install_sh())
    for fragment in ('from lib.storage import StorageSupervisor',
                     "'record.put'", "'record.get'", "'record.delete'",
                     "storage.client.health()['backend']"):
        assert fragment in block, f'Sidecar smoke round-trip missing: {fragment}'
    _ok('smoke test performs Sidecar health/write/read/delete')


def test_smoke_script_has_no_raw_driver_or_runtime_ddl():
    """Installer must not become a second connection/schema implementation."""
    block = _smoke_block(_install_sh())
    for forbidden in ('sqlite3.connect', 'psycopg2.connect',
                      'CREATE TABLE', 'DROP TABLE', '.commit()'):
        assert forbidden not in block, f'installer bypasses data layer: {forbidden}'
    _ok('installer owns no driver connections, DDL, or transaction boundary')


def test_sidecar_smoke_roundtrip_leaves_no_probe_record(tmp_path):
    """The same semantic API commits, reads back, and cleans up."""
    from lib.storage import StorageSupervisor

    key = 'installer-contract-probe'
    with StorageSupervisor(
            project_root=tmp_path / 'storage', backend='sqlite') as storage:
        assert storage.client.health()['backend'] == 'sqlite'
        storage.client.command(
            'record.put',
            {'namespace': 'system.install_smoke', 'key': key, 'value': True},
            'put-installer-contract-probe')
        stored = storage.client.query(
            'record.get',
            {'namespace': 'system.install_smoke', 'key': key})
        assert stored is not None and stored['value'] is True
        storage.client.command(
            'record.delete',
            {'namespace': 'system.install_smoke', 'key': key},
            'delete-installer-contract-probe')
        assert storage.client.query(
            'record.get',
            {'namespace': 'system.install_smoke', 'key': key}) is None
    _ok('Sidecar smoke probe leaves no storage record')


def test_smoke_failure_aborts_install():
    """A failed smoke test must call `fail` (abort), never merely `warn`."""
    block = _smoke_block(_install_sh())
    # The Python side exits non-zero on failure...
    assert 'sys.exit(1)' in block, 'smoke heredoc does not sys.exit(1) on failure'
    # ...and the shell else-branch turns that into a hard `fail` (abort).
    m = re.search(r'\nelse\n(.*?)fi\s*$', block, re.S)
    assert m, 'smoke step has no else (failure) branch'
    else_body = m.group(1)
    assert 'fail ' in else_body, 'smoke failure branch does not call fail (abort)'
    assert '|| warn' not in block and re.search(r'\bwarn "SQLite backend failed', block) is None, \
        'smoke failure must abort via fail, not warn'
    _ok('a failed smoke test aborts the install via fail (not warn)')


def test_smoke_backend_hint_does_not_misdirect():
    """Backend is derived from DB_BACKEND_CHOICE/PG_INSTALLED_MAJOR, and the
    failure hint must not tell a failed-PG user to switch to PG (or a failed-
    SQLite user to switch to SQLite)."""
    block = _smoke_block(_install_sh())
    # Backend derivation uses the decision already persisted to .env.
    assert '_SMOKE_BACKEND="$DB_BACKEND_CHOICE"' in block, \
        'smoke backend does not use the selected install backend'
    # Both backend branches exist in the failure hint.
    assert re.search(r'if \[\[ "\$_SMOKE_BACKEND" == "sqlite" \]\]; then', block), \
        'failure hint does not branch on the backend'
    # Each failure describes its own backend and refuses an implicit fallback.
    sqlite_hint = re.search(r'SQLite backend failed.*?"', block, re.S)
    pg_hint = re.search(r'PostgreSQL backend failed.*?"', block, re.S)
    assert sqlite_hint and 'disk space/write permissions' in sqlite_hint.group(0)
    assert pg_hint and 'storage-postgresql.log' in pg_hint.group(0)
    assert 'Refusing backend fallback' in sqlite_hint.group(0)
    assert 'Refusing backend fallback' in pg_hint.group(0)
    _ok('selected backend is preserved; failure hints never silently redirect')


def test_smoke_uses_env_python():
    """The round-trip must run via $ENV_PYTHON — the interpreter both the uv
    and conda paths set — so it verifies the environment that was installed."""
    block = _smoke_block(_install_sh())
    assert '"$ENV_PYTHON"' in block, 'smoke test does not run via $ENV_PYTHON'
    # And it runs from the install dir so `import lib.database` resolves.
    assert 'cd "$INSTALL_DIR"' in block, 'smoke test does not cd into INSTALL_DIR'
    _ok('smoke test runs via $ENV_PYTHON from the install dir')


def main():
    print()
    print(_color('═══ install.sh post-install DB smoke Guard Tests ═══', '36'))
    print()
    tests = [
        test_smoke_step_exists_after_env_before_launch,
        test_smoke_runs_full_round_trip,
        test_smoke_script_has_no_raw_driver_or_runtime_ddl,
        test_smoke_failure_aborts_install,
        test_smoke_backend_hint_does_not_misdirect,
        test_smoke_uses_env_python,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_install_db_smoke.standalone')
    main()
