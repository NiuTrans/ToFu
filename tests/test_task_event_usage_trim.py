"""Historical event-usage trim is storage-only and transaction bounded."""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

pytestmark = pytest.mark.unit


def _migration():
    path = os.path.join(os.path.dirname(__file__),
                        '_migrate_trim_task_event_usage.py')
    spec = importlib.util.spec_from_file_location('_event_usage_trim', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_batch_apply_strips_wire_and_commits_once():
    mig = _migration()
    event = {'type': 'done', 'committedMessage': {
        'apiRounds': [{'round': 1, 'usage': {
            'trace_id': 'keep', '_wire_bytes': list(range(1000)),
        }}],
    }}

    class Cursor:
        def __init__(self, row=None, rowcount=0):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class DB:
        def __init__(self):
            self.calls = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if sql.startswith('SELECT payload'):
                return Cursor({'payload': json.dumps(event), 'type': 'done'})
            if sql.startswith('UPDATE task_events'):
                return Cursor(rowcount=1)
            raise AssertionError(sql)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    db = DB()
    keys = [('task-1', 1, 'done', 100), ('task-2', 2, 'done', 100)]
    report = mig._process_batch(db, keys, apply=True)
    assert report['changed'] == 2 and db.commits == 1
    updates = [call for call in db.calls if call[0].startswith('UPDATE')]
    assert len(updates) == 2
    for _sql, params in updates:
        saved = json.loads(params[0])
        usage = saved['committedMessage']['apiRounds'][0]['usage']
        assert usage == {'trace_id': 'keep'}


def test_batch_dry_run_writes_nothing_and_rolls_back():
    mig = _migration()
    event = {'type': 'round_usage',
             'usage': {'input_tokens': 3, '_wire_bytes': [1, 2, 3]}}

    class Cursor:
        rowcount = 0

        def fetchone(self):
            return {'payload': event, 'type': 'round_usage'}

    class DB:
        def __init__(self):
            self.sql = []
            self.rollbacks = 0

        def execute(self, sql, params=None):
            self.sql.append(sql)
            return Cursor()

        def commit(self):
            raise AssertionError('dry-run must not commit')

        def rollback(self):
            self.rollbacks += 1

    db = DB()
    report = mig._process_batch(
        db, [('task-1', 1, 'round_usage', 100)], apply=False)
    assert report['changed'] == 1 and db.rollbacks == 1
    assert not any(sql.startswith('UPDATE') for sql in db.sql)
