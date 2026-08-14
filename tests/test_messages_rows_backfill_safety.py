"""Safety and cost bounds for the one-shot messages-row mirror backfill."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load():
    path = Path(__file__).with_name('_migrate_messages_rows_backfill.py')
    spec = importlib.util.spec_from_file_location('messages_rows_backfill_safety', path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


def test_candidate_inventory_avoids_full_jsonb_detoast_and_can_target_drift(monkeypatch):
    migration = _load()

    class DB:
        def __init__(self):
            self.sql = self.params = None

        def execute(self, sql, params=None):
            self.sql, self.params = sql, params
            return _Cursor(rows=[{'id': 'c1', 'n': 123, 'msg_count': 4}])

    db = DB()
    monkeypatch.setattr(migration, '_BACKEND', 'pg')
    assert migration._candidates(
        db, '', 0, 20, count_mismatch_only=True) == [('c1', 123, 4)]
    assert 'pg_column_size(c.messages)' in db.sql
    assert 'messages::text' not in db.sql
    assert 'c.msg_count<>COALESCE(cm.row_count,0)' in db.sql
    assert db.sql.endswith('LIMIT ?')
    assert db.params == (0, 20)


def test_marker_inventory_includes_genuinely_empty_conversations(monkeypatch):
    migration = _load()

    class DB:
        def execute(self, sql, params=None):
            self.sql, self.params = sql, params
            return _Cursor(rows=[{'id': 'empty', 'n': 2, 'msg_count': 0}])

    db = DB()
    monkeypatch.setattr(migration, '_BACKEND', 'pg')
    assert migration._candidates(
        db, '', 0, 0, include_empty=True) == [('empty', 2, 0)]
    assert 'c.msg_count > 0' not in db.sql


def test_rebuild_locks_authority_and_commits_only_verified_mirror(monkeypatch):
    migration = _load()

    class DB:
        def __init__(self):
            self.sql = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            self.sql.append(sql)
            return _Cursor(row={'messages': [{'role': 'user', 'content': 'x'}]})

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    monkeypatch.setattr(migration, '_BACKEND', 'pg')
    seen = []
    monkeypatch.setattr(
        migration, 'backfill_conv',
        lambda db, cid, messages, commit=False:
        seen.append((cid, messages, commit)) or len(messages))
    monkeypatch.setattr(
        migration, 'verify_conv_parity',
        lambda db, cid: {'ok': True, 'conv_id': cid})
    db = DB()
    n, verdict = migration._rebuild_and_verify(db, 'c-lock')
    assert n == 1 and verdict['ok'] is True
    assert db.sql[0].endswith('FOR UPDATE')
    assert seen == [('c-lock', [{'role': 'user', 'content': 'x'}], False)]
    assert db.commits == 1 and db.rollbacks == 0

    monkeypatch.setattr(
        migration, 'verify_conv_parity',
        lambda db, cid: {'ok': False, 'conv_id': cid})
    bad = DB()
    _, verdict = migration._rebuild_and_verify(bad, 'c-bad')
    assert verdict['ok'] is False
    assert bad.commits == 0 and bad.rollbacks == 1


def test_marker_locks_reverifies_and_commits_only_exact_mirror(monkeypatch):
    migration = _load()

    class DB:
        def __init__(self):
            self.sql = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            self.sql.append(sql)
            return _Cursor(row={'messages': [{'role': 'user', 'content': 'x'}]})

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    monkeypatch.setattr(migration, '_BACKEND', 'pg')
    monkeypatch.setattr(
        migration, 'verify_conv_parity',
        lambda db, cid: {'ok': True, 'mirror_current': False})
    marked = []
    monkeypatch.setattr(
        migration, 'mark_conv_mirror_current',
        lambda db, cid, messages: marked.append((cid, messages)))
    db = DB()
    verdict = migration._mark_and_verify(db, 'c-mark')
    assert verdict == {'ok': True, 'mirror_current': True}
    assert db.sql[0].endswith('FOR UPDATE')
    assert marked == [('c-mark', [{'role': 'user', 'content': 'x'}])]
    assert db.commits == 1 and db.rollbacks == 0

    monkeypatch.setattr(
        migration, 'verify_conv_parity',
        lambda db, cid: {'ok': False, 'mirror_current': False})
    bad = DB()
    verdict = migration._mark_and_verify(bad, 'c-bad')
    assert verdict['ok'] is False
    assert bad.commits == 0 and bad.rollbacks == 1
