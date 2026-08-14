"""Safety, authority-preservation, and idempotence for diagnostics cleanup."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'compact_sqlite_diagnostics.py'


def _load():
    spec = importlib.util.spec_from_file_location('compact_sqlite_diagnostics', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(path: Path):
    db = sqlite3.connect(path)
    db.execute(
        'CREATE TABLE task_events (task_id TEXT, event_id INTEGER, type TEXT, '
        'payload TEXT, note TEXT, PRIMARY KEY(task_id,event_id))')
    db.execute(
        'CREATE TABLE task_results (task_id TEXT PRIMARY KEY, status TEXT, '
        'completed_at INTEGER, metadata TEXT, content TEXT)')
    event = {
        'type': 'done',
        'usage': {'total_tokens': 9, '_wire_bytes': ['x'] * 1000},
        'committedMessage': {
            'content': 'authority',
            'apiRounds': [{'usage': {
                'trace_id': 'keep', '_wire_field_bytes': ['y'] * 1000,
            }}],
        },
    }
    metadata = {
        'usage': {'input_tokens': 4, '_wire_fp': ['z'] * 1000},
        'apiRounds': [{'cost': {'costCny': 1}, 'usage': {
            'output_tokens': 2, '_wire_static': ['q'] * 1000,
        }}],
    }
    db.execute('INSERT INTO task_events VALUES (?,?,?,?,?)',
               ('t1', 1, 'done', json.dumps(event), 'event-authority'))
    db.execute('INSERT INTO task_results VALUES (?,?,?,?,?)',
               ('t1', 'done', 123, json.dumps(metadata), 'result-authority'))
    db.commit()
    db.close()


def _rows(path: Path):
    db = sqlite3.connect(path)
    event = db.execute(
        'SELECT payload,note FROM task_events WHERE task_id=? AND event_id=1',
        ('t1',)).fetchone()
    result = db.execute(
        'SELECT metadata,content,status,completed_at FROM task_results '
        'WHERE task_id=?', ('t1',)).fetchone()
    db.close()
    return event, result


def test_dry_run_is_read_only_and_reports_both_tables(tmp_path):
    module = _load()
    path = tmp_path / 'tofu.db'
    _seed(path)
    before = _rows(path)
    report = module.run(path, apply=False, batch_size=1)
    assert report['events']['compactable'] == 1
    assert report['results']['compactable'] == 1
    assert report['events']['logical_bytes_reclaimed'] > 1000
    assert report['results']['logical_bytes_reclaimed'] > 1000
    assert report['vacuum_performed'] is False
    assert _rows(path) == before


def test_apply_preserves_public_fields_and_other_columns(tmp_path):
    module = _load()
    path = tmp_path / 'tofu.db'
    _seed(path)
    report = module.run(path, apply=True, batch_size=1, sleep_ms=0)
    assert report['events']['applied'] == 1
    assert report['results']['applied'] == 1

    event_row, result_row = _rows(path)
    event = json.loads(event_row[0])
    metadata = json.loads(result_row[0])
    assert event_row[1] == 'event-authority'
    assert result_row[1:] == ('result-authority', 'done', 123)
    assert event['usage'] == {'total_tokens': 9}
    assert event['committedMessage']['content'] == 'authority'
    assert event['committedMessage']['apiRounds'][0]['usage'] == {
        'trace_id': 'keep',
    }
    assert metadata['usage'] == {'input_tokens': 4}
    assert metadata['apiRounds'][0] == {
        'cost': {'costCny': 1}, 'usage': {'output_tokens': 2},
    }

    second = module.run(path, apply=True, batch_size=1, sleep_ms=0)
    assert second['events']['candidates'] == 0
    assert second['results']['candidates'] == 0


def test_import_has_no_database_bootstrap_side_effect():
    before = set(sys.modules)
    _load()
    newly_loaded = set(sys.modules) - before
    assert 'lib.database' not in newly_loaded
