"""SQLite authority imports must not activate retired PostgreSQL runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def test_sqlite_database_facade_does_not_import_pg_runtime(tmp_path):
    """Incident 2026-08-13: a SQLite import loaded 20+ PG lifecycle modules."""
    script = """
import json, sys
import lib.database
loaded = sorted(
    name for name in sys.modules
    if name.startswith('lib.database._bootstrap')
    or name.startswith('lib.database._pg_backup')
    or name.startswith('lib.database._pg_ownership')
    or name == 'lib.database._pg_seed'
    or name.startswith('lib.database._schema_pg')
)
print(json.dumps(loaded))
"""
    env = dict(os.environ)
    env.update({
        'TOFU_DB_BACKEND': 'sqlite',
        'TOFU_DB_PATH': str(tmp_path / 'isolated.db'),
        'TOFU_DATA_DIR': str(tmp_path),
        'TOFU_SERVER_PROCESS': '0',
    })
    proc = subprocess.run(
        [sys.executable, '-c', script], cwd=str(_ROOT), env=env,
        text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
