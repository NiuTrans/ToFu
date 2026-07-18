#!/usr/bin/env python3
"""Guard tests: install.sh defaults to SQLite; PostgreSQL is opt-in.

Background — the "install is too slow / fails too often" complaint (2026-07):
  The PostgreSQL install step (layered icu/libxml2/PG-major conda solve +
  initdb + start smoke-test + force-reinstall recovery) is the single
  slowest and most failure-prone part of install.sh, yet single-user setups
  (the overwhelming majority) never need PG — SQLite is a zero-config, fully
  supported fallback. So PG became OPT-IN behind `--with-postgres`; the
  default install skips PG entirely and pins TOFU_DB_BACKEND=sqlite.

  These tests pin that contract by STATIC ANALYSIS of install.sh (no network,
  no conda, no server): without `--with-postgres`, no PG install / initdb /
  smoke-test branch may fire, and the "you have pgdata but PG is off" warning
  that tells old users how to recover their data must survive.
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


def _pg_install_block(text: str) -> str:
    """The PG-install region: from its header comment to the next step marker."""
    start = text.index('Install PostgreSQL + psycopg2 from conda-forge')
    tail = text[start:]
    # The PG install region ends at the next `step "..."` (Step 7: ripgrep).
    nxt = re.search(r'\nstep "', tail)
    end = start + (nxt.start() if nxt else len(tail))
    return text[start:end]


def test_with_postgres_flag_parses_and_defaults_off():
    """--with-postgres must parse to WITH_POSTGRES=1 and default to 0."""
    text = _install_sh()
    assert 'WITH_POSTGRES=0' in text, 'WITH_POSTGRES has no default-off init'
    assert '--with-postgres)' in text, 'no --with-postgres argument parser'
    assert re.search(r'--with-postgres\)\s+WITH_POSTGRES=1', text), \
        '--with-postgres does not set WITH_POSTGRES=1'
    _ok('--with-postgres parses to WITH_POSTGRES=1 and defaults off')


def test_pg_install_is_gated_on_with_postgres():
    """The conda `postgresql=` install must sit BEHIND the WITH_POSTGRES guard."""
    block = _pg_install_block(_install_sh())
    # The block must open with the opt-in guard.
    assert re.search(r'if \[\[ "\$WITH_POSTGRES" -ne 1 \]\]; then', block), \
        'PG install block does not branch on $WITH_POSTGRES'
    # The actual heavy install (conda install postgresql=<major>) must exist
    # and must live AFTER the guard's SQLite-default branch — i.e. it is only
    # reached on the else (PG opt-in) path.
    guard_idx = block.index('if [[ "$WITH_POSTGRES" -ne 1 ]]; then')
    pg_solve = re.search(r'conda install -n "\$ENV_NAME".*?"postgresql=\$\{_try_major\}"',
                         block, re.S)
    assert pg_solve, 'the layered `postgresql=<major>` conda solve vanished'
    assert pg_solve.start() > guard_idx, \
        'the postgresql conda solve is NOT downstream of the WITH_POSTGRES guard'
    _ok('the PostgreSQL conda solve only runs on the --with-postgres path')


def test_default_path_runs_no_pg_install_or_initdb():
    """Without --with-postgres, no PG install/initdb branch is reachable.

    We assert the SQLite-default branch of the guard contains NO conda
    postgresql install and NO initdb delegation — it must be a pure no-op that
    leaves PG_INSTALLED_MAJOR empty (which makes every downstream PG step
    short-circuit).
    """
    block = _pg_install_block(_install_sh())
    # Extract the SQLite-default branch body: from the guard `then` up to the
    # first `elif`/`else` that begins the PG-opt-in paths.
    m = re.search(
        r'if \[\[ "\$WITH_POSTGRES" -ne 1 \]\]; then(.*?)\nelif \[\[ "\$FORCE_SQLITE"',
        block, re.S)
    assert m, 'could not isolate the SQLite-default branch body'
    # Strip comment lines — we care about actual command invocations, not
    # prose in the explanatory comment (which legitimately names initdb etc).
    default_body = '\n'.join(
        ln for ln in m.group(1).splitlines()
        if not ln.lstrip().startswith('#'))
    assert 'postgresql=' not in default_body, \
        'SQLite-default branch still runs a postgresql conda install'
    assert 'initdb' not in default_body, \
        'SQLite-default branch still runs initdb'
    assert 'PG_INSTALLED_MAJOR=' not in default_body, \
        'SQLite-default branch sets PG_INSTALLED_MAJOR (must stay empty)'
    _ok('default (no --with-postgres) path runs no PG install / initdb')


def test_downstream_pg_steps_require_installed_major():
    """The initdb-delegate and smoke-test steps must gate on PG_INSTALLED_MAJOR.

    This is the cascade that makes gating just the install block sufficient:
    when PG_INSTALLED_MAJOR is empty (SQLite default), the bootstrap and
    smoke-test steps below are unreachable.
    """
    text = _install_sh()
    # initdb delegation (local-disk split) requires a non-empty installed major.
    assert re.search(
        r'if \[\[ -z "\$DB_BACKEND_CHOICE" && -n "\$PG_INSTALLED_MAJOR".*?initdb via runtime',
        text, re.S), 'initdb-delegate step is not gated on -n PG_INSTALLED_MAJOR'
    # pg_ctl smoke-test likewise requires a non-empty installed major.
    assert re.search(
        r'elif \[\[ -z "\$DB_BACKEND_CHOICE" && -n "\$PG_INSTALLED_MAJOR".*?Smoke-testing PostgreSQL',
        text, re.S), 'smoke-test step is not gated on -n PG_INSTALLED_MAJOR'
    _ok('initdb + smoke-test steps require a non-empty PG_INSTALLED_MAJOR')


def test_default_env_backend_is_sqlite():
    """With no PG installed, .env must be pinned to TOFU_DB_BACKEND=sqlite."""
    text = _install_sh()
    # The validation region sets DB_BACKEND_CHOICE=sqlite when PG_INSTALLED_MAJOR
    # is empty, and the .env writer emits sqlite for that choice.
    assert re.search(
        r'elif \[\[ -z "\$PG_INSTALLED_MAJOR" \]\]; then.*?DB_BACKEND_CHOICE="sqlite"',
        text, re.S), 'no-PG case does not set DB_BACKEND_CHOICE=sqlite'
    assert re.search(
        r'if \[\[ "\$DB_BACKEND_CHOICE" == "sqlite" \]\]; then\s*\n\s*_set_env_var "TOFU_DB_BACKEND" "sqlite"',
        text), '.env writer does not pin TOFU_DB_BACKEND=sqlite for the sqlite choice'
    _ok('default install pins TOFU_DB_BACKEND=sqlite in .env')


def test_existing_pgdata_recovery_warning_survives():
    """Old users with pgdata but PG off must be told how to re-enable it.

    Owner requirement: never let an existing PostgreSQL dataset silently go
    dark — the warning must name --with-postgres as the recovery path.
    """
    text = _install_sh()
    # The "pgdata exists but no PG binaries" warning must still exist.
    assert 'pgdata exists (PG ${PGDATA_MAJOR}) but no PG binaries installed in env' in text, \
        'the pgdata-exists warning was removed'
    # And it must point users at --with-postgres to recover their data.
    m = re.search(r'pgdata exists \(PG \$\{PGDATA_MAJOR\}\) but no PG binaries.*?fi',
                  text, re.S)
    assert m and '--with-postgres' in m.group(0), \
        'pgdata-exists warning does not tell the user to re-run with --with-postgres'
    assert m and 'NOT lost' in m.group(0), \
        'pgdata-exists warning no longer reassures the data is preserved'
    _ok('existing-pgdata recovery warning survives and names --with-postgres')


def main():
    print()
    print(_color('═══ install.sh SQLite-default / PG-opt-in Guard Tests ═══', '36'))
    print()
    tests = [
        test_with_postgres_flag_parses_and_defaults_off,
        test_pg_install_is_gated_on_with_postgres,
        test_default_path_runs_no_pg_install_or_initdb,
        test_downstream_pg_steps_require_installed_major,
        test_default_env_backend_is_sqlite,
        test_existing_pgdata_recovery_warning_survives,
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
    main()
