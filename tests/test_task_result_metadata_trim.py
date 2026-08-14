"""Historical task-result wire diagnostics are removed safely and once."""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

pytestmark = pytest.mark.unit


def _migration():
    path = os.path.join(os.path.dirname(__file__),
                        '_migrate_trim_task_result_metadata.py')
    spec = importlib.util.spec_from_file_location('_result_meta_trim', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fat_meta():
    return {
        'finishReason': 'stop',
        'usage': {'input_tokens': 5},
        'apiRounds': [
            {'round': 1, 'usage': {
                'input_tokens': 3,
                'trace_id': 'trace-visible',
                '_dispatch': {'provider': 'visible'},
                '_wire_bytes': list(range(1000)),
                '_wire_field_bytes': {'messages': 'x' * 10000},
            }},
            {'round': 2, 'usage': {'output_tokens': 7}},
        ],
    }


def test_trim_metadata_reuses_live_sanitizer_and_preserves_visible_fields():
    mig = _migration()
    meta = _fat_meta()
    clean = mig.trim_metadata(meta)
    assert len(json.dumps(clean)) < len(json.dumps(meta)) / 5
    usage = clean['apiRounds'][0]['usage']
    assert not any(key.startswith('_wire_') for key in usage)
    assert usage['trace_id'] == 'trace-visible'
    assert usage['_dispatch'] == {'provider': 'visible'}
    assert clean['finishReason'] == 'stop' and clean['usage'] == {'input_tokens': 5}
    assert '_wire_bytes' in meta['apiRounds'][0]['usage'], 'input must not mutate'


def test_apply_uses_terminal_timestamp_cas():
    mig = _migration()

    class Cursor:
        def __init__(self, row=None, rowcount=0):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class DB:
        def __init__(self, update_count):
            self.update_count = update_count
            self.calls = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if sql.startswith('SELECT metadata'):
                return Cursor({'metadata': json.dumps(_fat_meta()),
                               'status': 'done', 'completed_at': 12345})
            if sql.startswith('UPDATE task_results'):
                return Cursor(rowcount=self.update_count)
            raise AssertionError(sql)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    db = DB(1)
    result = mig._process_one(db, 'task-cas', apply=True)
    assert result['status'] == 'applied' and db.commits == 1
    sql, params = next(call for call in db.calls
                       if call[0].startswith('UPDATE task_results'))
    assert 'status=? AND completed_at=?' in sql
    assert params[-2:] == ('done', 12345)

    lost = DB(0)
    result = mig._process_one(lost, 'task-cas', apply=True)
    assert result['status'] == 'cas_lost'
    assert lost.commits == 0 and lost.rollbacks == 1


def test_nonterminal_row_is_never_rewritten():
    mig = _migration()

    class Cursor:
        rowcount = 0

        def fetchone(self):
            return {'metadata': json.dumps(_fat_meta()), 'status': 'running',
                    'completed_at': 12345}

    class DB:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)
            return Cursor()

        def commit(self):
            raise AssertionError('running row must not commit')

        def rollback(self):
            pass

    db = DB()
    assert mig._process_one(db, 'running', apply=True)['status'] == 'nonterminal'
    assert not any(sql.startswith('UPDATE') for sql in db.sql)
