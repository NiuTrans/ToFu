"""Tests for the critical-column startup self-heal — lib/database/_schema_pg.py.

Pins the fix for the "version-current-but-column-missing" divergence: the
version-cache fast-path trusts the stored _schema_version integer as a proxy
for "all DDL applied", but the version write and the per-column ALTERs are NOT
atomic across restarts on slow (FUSE) storage. A migration that commits the
version yet has a later ADD COLUMN fail/time out leaves the DB current-by-version
but column-missing, and every future boot would fast-path past the fix.

`_missing_critical_columns` runs BEFORE the fast-path so such a DB re-migrates.

Run:  pytest tests/test_pg_critical_column_selfheal.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Conn:
    """Minimal stand-in for the project's connection wrapper (._conn)."""
    def __init__(self, raw):
        self._conn = raw


@pytest.mark.unit
class TestCriticalColumnSelfHeal:
    def test_reports_missing_columns_on_existing_table(self, monkeypatch):
        """A present table missing a critical column is reported (would force migration)."""
        import lib.database._schema_pg as sp

        present = {('project_tasks', 'blocked_until'), ('project_tasks', 'block_count')}
        monkeypatch.setattr(sp, '_table_exists', lambda conn, t: t == 'project_tasks')
        monkeypatch.setattr(sp, '_column_exists', lambda conn, t, c: (t, c) in present)

        missing = sp._missing_critical_columns(_Conn(object()))
        # the three columns NOT in `present` must be flagged
        assert ('project_tasks', 'block_reason') in missing
        assert ('project_tasks', 'wait_paths') in missing
        assert ('project_tasks', 'dispatch_target') in missing
        # the two present ones must NOT be flagged
        assert ('project_tasks', 'blocked_until') not in missing
        assert ('project_tasks', 'block_count') not in missing

    def test_no_missing_when_all_present(self, monkeypatch):
        """All critical columns present → empty list → fast-path allowed."""
        import lib.database._schema_pg as sp
        monkeypatch.setattr(sp, '_table_exists', lambda conn, t: True)
        monkeypatch.setattr(sp, '_column_exists', lambda conn, t, c: True)
        assert sp._missing_critical_columns(_Conn(object())) == []

    def test_absent_table_is_skipped(self, monkeypatch):
        """A missing table is NOT reported — the create path owns it, this guard
        only catches column-missing on an EXISTING table."""
        import lib.database._schema_pg as sp
        monkeypatch.setattr(sp, '_table_exists', lambda conn, t: False)
        # _column_exists must never be consulted when the table is absent
        def _boom(conn, t, c):
            raise AssertionError('_column_exists called for an absent table')
        monkeypatch.setattr(sp, '_column_exists', _boom)
        assert sp._missing_critical_columns(_Conn(object())) == []

    def test_probe_failure_is_best_effort(self, monkeypatch):
        """A probe raising must not propagate — returns [] (never blocks boot)."""
        import lib.database._schema_pg as sp

        def _raise(conn, t):
            raise RuntimeError('information_schema unavailable')
        monkeypatch.setattr(sp, '_table_exists', _raise)
        assert sp._missing_critical_columns(_Conn(object())) == []

    def test_project_tasks_critical_set_matches_engine_columns(self):
        """The registry must cover the 5 engine columns whose absence broke the
        live board (blocked_until/block_count/block_reason/wait_paths/dispatch_target)."""
        import lib.database._schema_pg as sp
        cols = set(sp._CRITICAL_COLUMNS.get('project_tasks', ()))
        assert {'blocked_until', 'block_count', 'block_reason',
                'wait_paths', 'dispatch_target'} <= cols
