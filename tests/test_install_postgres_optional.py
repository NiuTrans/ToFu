"""Standalone installer cannot provision a local PostgreSQL authority."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'install.sh'


def _source() -> str:
    return INSTALLER.read_text(encoding='utf-8')


def test_installer_is_personal_sqlite_only_and_rejects_removed_pg_flags():
    help_result = subprocess.run(
        ['bash', str(INSTALLER), '--help'],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert '--with-postgres' not in help_result.stdout
    assert '--reinit-pgdata' not in help_result.stdout

    removed = subprocess.run(
        ['bash', str(INSTALLER), '--with-postgres'],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert removed.returncode == 2
    assert 'distributed PostgreSQL with Kubernetes' in removed.stderr


def test_installer_has_no_database_server_install_or_start_path():
    source = _source()
    for removed_capability in (
        'initdb',
        'pg_ctl',
        '_ensure_pg_running',
        '_run_pg_bootstrap_delegate',
        '_run_pg_ctl_smoke',
        '"postgresql=',
    ):
        assert removed_capability not in source
    assert '"psycopg>=3.2"' in source
    assert '"psycopg-pool>=3.2"' in source


def test_installer_rewrites_existing_env_to_the_new_personal_contract():
    source = _source()
    assert '_set_env_var "TOFU_DEPLOYMENT_MODE" "personal"' in source
    assert '_set_env_var "TOFU_PROCESS_ROLE" "all"' in source
    assert 'TOFU_REQUIRE_PG TOFU_REPLICA_RING TOFU_STORAGE_MODE' in source
    # The whole retired ``*_DB_*`` family is removed by one bounded matcher,
    # so future legacy selectors cannot escape by changing only the suffix.
    assert '(TOFU|CHATUI)_DB_[A-Za-z0-9_]+' in source
    assert 'TOFU_DEPLOYMENT_MODE=personal TOFU_PROCESS_ROLE=all' in source
    assert 'TOFU_DB_BACKEND="$_SMOKE_BACKEND"' not in source


def test_legacy_pgdata_is_only_detected_and_left_untouched():
    source = _source()
    marker = 'step "Selecting personal SQLite storage"'
    start = source.index(marker)
    end = source.index('#  Step 9: Configure .env', start)
    selection = source[start:end]

    assert '[[ -d "${INSTALL_DIR}/data/pgdata" ]]' in selection
    assert 'left untouched' in selection
    assert 'mv ' not in selection
    assert 'rm ' not in selection


def test_installer_shell_is_syntactically_valid():
    result = subprocess.run(
        ['bash', '-n', str(INSTALLER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
