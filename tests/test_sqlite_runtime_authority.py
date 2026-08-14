"""SQLite defaults and PostgreSQL is an equal, exact, fail-closed selection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _clean_db_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        'TOFU_DB_BACKEND', 'CHATUI_DB_BACKEND',
        'TOFU_REQUIRE_PG', 'CHATUI_REQUIRE_PG',
        'TOFU_ALLOW_POSTGRES_ROLLBACK',
    ):
        env.pop(name, None)
    env.update({
        'TOFU_DB_PATH': str(tmp_path / 'authority.db'),
        'TOFU_DATA_DIR': str(tmp_path),
        'TOFU_SERVER_PROCESS': '0',
    })
    return env


def test_no_backend_env_selects_sqlite_without_pg_runtime(tmp_path):
    script = """
import json, sys
import lib.database
print(json.dumps({
    'backend': lib.database._BACKEND,
    'pg_modules': sorted(
        name for name in sys.modules
        if name.startswith('lib.database._bootstrap')
        or name.startswith('lib.database._pg_backup')
        or name.startswith('lib.database._pg_ownership')
        or name == 'lib.database._pg_seed'
        or name.startswith('lib.database._schema_pg')
    ),
}))
"""
    proc = subprocess.run(
        [sys.executable, '-c', script], cwd=str(_ROOT),
        env=_clean_db_env(tmp_path), text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result == {'backend': 'sqlite', 'pg_modules': []}


def test_bootstrap_pg_repair_requires_explicit_operator_intent(monkeypatch):
    import bootstrap

    for name in (
        'TOFU_DB_BACKEND', 'CHATUI_DB_BACKEND',
        'TOFU_REQUIRE_PG', 'CHATUI_REQUIRE_PG',
    ):
        monkeypatch.delenv(name, raising=False)
    assert bootstrap._legacy_pg_runtime_requested() is False
    assert bootstrap._try_conda_install_postgresql() is False

    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    assert bootstrap._legacy_pg_runtime_requested() is True
    monkeypatch.setenv('TOFU_DB_BACKEND', 'pg')
    with pytest.raises(RuntimeError, match='exactly sqlite or postgres'):
        bootstrap._legacy_pg_runtime_requested()


def test_bootstrap_llm_prompt_never_reintroduces_postgresql(monkeypatch):
    import bootstrap

    captured = {}

    class _Opener:
        def open(self, request, timeout):
            captured['body'] = json.loads(request.data.decode('utf-8'))
            raise OSError('stop after prompt capture')

    monkeypatch.setattr(bootstrap.urllib.request, 'build_opener', lambda *_: _Opener())
    result = bootstrap._call_llm('initdb not found', {
        'base_url': 'https://example.invalid/v1',
        'model': 'test',
        'api_keys': ['not-a-real-key'],
    })
    assert result['unresolvable'] is True
    prompt = captured['body']['messages'][0]['content']
    assert 'SQLite and PostgreSQL are equal Storage Sidecar backends' in prompt
    assert 'Never suggest automatic backend fallback' in prompt
    assert 'conda:postgresql>=18' not in prompt
