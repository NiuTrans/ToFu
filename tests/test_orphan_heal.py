"""Tests for the orphan-table startup self-heal — lib/database/_orphan_heal.py.

Pins the deployment-wide acceptance criterion "live-DB ∖ Core ∖ external = ∅":
a known orphan table left by a removed subsystem (agent_sessions) or a leaked
test scratch table (_aio_test) is dropped on init, EXCEPT a require_empty
orphan that still holds rows (fail-safe against a live writer).

Run:  pytest tests/test_orphan_heal.py -m unit
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Conn:
    """Minimal stand-in for the project's connection wrapper (.​_conn)."""
    def __init__(self, raw):
        self._conn = raw


def _table_exists(conn, table):
    cur = conn._conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _count_rows(conn, table):
    cur = conn._conn.cursor()
    cur.execute(f'SELECT count(*) FROM {table}')
    return int(cur.fetchone()[0])


@pytest.fixture()
def conn(tmp_path):
    raw = sqlite3.connect(str(tmp_path / 'heal.db'))
    yield _Conn(raw)
    raw.close()


@pytest.mark.unit
class TestOrphanHeal:
    def test_empty_orphans_are_dropped(self, conn):
        from lib.database._orphan_heal import heal_orphan_tables
        conn._conn.execute('CREATE TABLE agent_sessions (conv_id TEXT, backend TEXT, session_id TEXT)')
        conn._conn.execute('CREATE TABLE _aio_test (id INTEGER PRIMARY KEY, name TEXT)')
        conn._conn.commit()

        dropped = heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)

        assert set(dropped) == {'agent_sessions', '_aio_test'}
        assert not _table_exists(conn, 'agent_sessions')
        assert not _table_exists(conn, '_aio_test')

    def test_nonempty_require_empty_orphan_is_spared(self, conn):
        """agent_sessions with rows must NOT be dropped (a live writer may exist)."""
        from lib.database._orphan_heal import heal_orphan_tables
        conn._conn.execute('CREATE TABLE agent_sessions (conv_id TEXT, backend TEXT, session_id TEXT)')
        conn._conn.execute("INSERT INTO agent_sessions VALUES ('c1', 'codex', 's1')")
        conn._conn.commit()

        dropped = heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)

        assert 'agent_sessions' not in dropped
        assert _table_exists(conn, 'agent_sessions')
        assert _count_rows(conn, 'agent_sessions') == 1

    def test_nonempty_test_scratch_is_still_dropped(self, conn):
        """_aio_test is require_empty=False — never a real table, drop regardless."""
        from lib.database._orphan_heal import heal_orphan_tables
        conn._conn.execute('CREATE TABLE _aio_test (id INTEGER PRIMARY KEY, name TEXT)')
        conn._conn.execute("INSERT INTO _aio_test (id, name) VALUES (1, 'x')")
        conn._conn.commit()

        dropped = heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)

        assert '_aio_test' in dropped
        assert not _table_exists(conn, '_aio_test')

    def test_noop_when_orphans_absent(self, conn):
        """Idempotent: nothing to drop on a clean DB."""
        from lib.database._orphan_heal import heal_orphan_tables
        dropped = heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)
        assert dropped == []
