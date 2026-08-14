"""Safety and idempotence tests for the historical light-row compactor."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'compact_sqlite_projections.py'


def _load():
    spec = importlib.util.spec_from_file_location('compact_sqlite_projections', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(path: Path):
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE conversation_messages ('
        'conv_id TEXT NOT NULL, seq INTEGER NOT NULL, meta TEXT NOT NULL, '
        'meta_light TEXT, PRIMARY KEY(conv_id, seq))')
    authority = {
        'role': 'assistant', 'content': 'answer',
        'toolRounds': [{'status': 'done', 'toolContent': 'keep'}],
        'apiRounds': [{
            'round': 1, 'cost': {'costCny': 0.2},
            'usage': {'prompt_tokens': 12, 'trace_id': 'keep',
                      '_wire_bytes': ['x'] * 5000},
        }],
        '_continueApiRounds': [{
            'round': 2,
            'usage': {'completion_tokens': 3,
                      '_wire_field_bytes': ['x'] * 1000},
        }],
    }
    light = dict(authority)
    light.pop('toolRounds')
    light['_trimmed'] = True
    conn.execute(
        'INSERT INTO conversation_messages VALUES (?,?,?,?)',
        ('cv', 0, json.dumps(authority), json.dumps(light)))
    conn.commit()
    conn.close()
    return authority


def _row(path: Path):
    conn = sqlite3.connect(path)
    row = conn.execute(
        'SELECT meta, meta_light FROM conversation_messages '
        'WHERE conv_id=? AND seq=?', ('cv', 0)).fetchone()
    conn.close()
    return row


def test_dry_run_is_read_only_and_reports_reclaim(tmp_path):
    module = _load()
    path = tmp_path / 'tofu.db'
    _seed(path)
    before = _row(path)
    report = module.run(path, apply=False, batch_size=1)
    assert report['compactable'] == 1
    assert report['logical_bytes_reclaimed'] > 10_000
    assert report['vacuum_performed'] is False
    assert _row(path) == before


def test_apply_changes_only_projection_and_is_idempotent(tmp_path):
    module = _load()
    path = tmp_path / 'tofu.db'
    authority = _seed(path)
    report = module.run(path, apply=True, batch_size=1, sleep_ms=0)
    assert report['applied'] == 1 and report['cas_lost'] == 0

    meta, meta_light = _row(path)
    assert json.loads(meta) == authority, 'lossless authority changed'
    projected = json.loads(meta_light)
    assert projected['apiRounds'][0]['cost'] == {'costCny': 0.2}
    assert projected['apiRounds'][0]['usage'] == {
        'prompt_tokens': 12, 'trace_id': 'keep',
    }
    assert projected['_continueApiRounds'][0]['usage'] == {
        'completion_tokens': 3,
    }
    assert '_wire_' not in meta_light

    second = module.run(path, apply=True, batch_size=1, sleep_ms=0)
    assert second['candidates'] == 0
    assert second['applied'] == 0


def test_script_import_does_not_import_database_bootstrap():
    before = set(sys.modules)
    _load()
    newly_loaded = set(sys.modules) - before
    assert 'lib.database' not in newly_loaded
