"""Tests for lib/database/_sql_translate.py — SQLite→PostgreSQL SQL translation.

This module sits in the DB write hot path (``PgCursor.execute`` calls
``translate_sql`` on every statement) yet had ZERO tests — it was the single
most bug-prone backend module per the 2026-06 architecture review. These tests
pin every translation rule and document the two known gaps as ``xfail`` so they
are tracked rather than silently shipped.

Run:  pytest tests/test_sql_translate.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tr(sql):
    from lib.database._sql_translate import translate_sql
    return translate_sql(sql)


# ═══════════════════════════════════════════════════════════
#  Placeholders — ? → %s, respecting string literals
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPlaceholders:
    def test_basic_placeholder(self):
        out, is_pragma = _tr('SELECT * FROM t WHERE id = ?')
        assert out == 'SELECT * FROM t WHERE id = %s'
        assert is_pragma is False

    def test_multiple_placeholders(self):
        out, _ = _tr('INSERT INTO t (a, b, c) VALUES (?, ?, ?)')
        assert out == 'INSERT INTO t (a, b, c) VALUES (%s, %s, %s)'

    def test_question_mark_inside_string_literal_preserved(self):
        # A literal '?' inside quotes must NOT become %s.
        out, _ = _tr("SELECT * FROM t WHERE label = 'why?' AND id = ?")
        assert out == "SELECT * FROM t WHERE label = 'why?' AND id = %s"

    def test_escaped_quote_inside_string(self):
        out, _ = _tr("SELECT * FROM t WHERE name = 'O''Brien' AND id = ?")
        assert out == "SELECT * FROM t WHERE name = 'O''Brien' AND id = %s"

    def test_percent_literal_escaped_when_placeholder_present(self):
        # When the statement is %s-interpolated by psycopg2 (it has a ?
        # placeholder), every literal % MUST be doubled to %% so psycopg2
        # restores it to a single %. Otherwise psycopg2 reads 'foo%' / '%a'
        # as a format spec → IndexError / `near "%": syntax error`.
        out, _ = _tr("SELECT * FROM t WHERE name LIKE 'foo%' AND id = ?")
        assert out == "SELECT * FROM t WHERE name LIKE 'foo%%' AND id = %s"

    def test_percent_literal_untouched_without_placeholder(self):
        # No ? placeholder → psycopg2 sends the statement verbatim (no
        # interpolation), so the literal % must NOT be doubled.
        out, _ = _tr("SELECT * FROM t WHERE name LIKE 'foo%'")
        assert out == "SELECT * FROM t WHERE name LIKE 'foo%'"

    def test_like_pattern_both_sides_escaped_with_placeholder(self):
        # The exact shape that crashed recover_stale_tasks_on_startup.
        out, _ = _tr("SELECT id FROM conversations WHERE user_id=? "
                     "AND CAST(settings AS TEXT) LIKE '%activeTaskId%'")
        assert "user_id=%s" in out
        assert "LIKE '%%activeTaskId%%'" in out


# ═══════════════════════════════════════════════════════════
#  PRAGMA handling
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPragma:
    def test_generic_pragma_skipped(self):
        out, is_pragma = _tr('PRAGMA journal_mode=WAL')
        assert out is None
        assert is_pragma is True

    def test_pragma_table_info_becomes_information_schema(self):
        out, is_pragma = _tr('PRAGMA table_info(conversations)')
        assert is_pragma is False
        assert 'information_schema.columns' in out
        assert "table_name = 'conversations'" in out


# ═══════════════════════════════════════════════════════════
#  Time / misc scalar function translation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScalarFunctions:
    def test_strftime_epoch_ms(self):
        out, _ = _tr("SELECT strftime('%s','now')*1000")
        assert "(EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT" in out
        assert 'strftime' not in out

    def test_strftime_epoch_seconds(self):
        out, _ = _tr("SELECT strftime('%s','now')")
        assert "EXTRACT(EPOCH FROM NOW())::BIGINT" in out

    def test_datetime_now(self):
        out, _ = _tr("INSERT INTO t (ts) VALUES (datetime('now'))")
        assert 'NOW()' in out
        assert 'datetime' not in out

    def test_changes_becomes_constant(self):
        out, _ = _tr('SELECT changes()')
        assert out == 'SELECT 0'

    def test_last_insert_rowid(self):
        out, _ = _tr('SELECT last_insert_rowid()')
        assert out == 'SELECT lastval()'

    def test_json_array_length(self):
        out, _ = _tr('SELECT json_array_length(messages) FROM conversations')
        assert 'jsonb_array_length' in out
        assert 'json_array_length' not in out

    def test_autoincrement_to_serial(self):
        out, _ = _tr('CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)')
        assert 'SERIAL PRIMARY KEY' in out

    def test_collate_nocase_removed(self):
        out, _ = _tr('SELECT * FROM t ORDER BY name COLLATE NOCASE')
        assert 'COLLATE' not in out.upper()


# ═══════════════════════════════════════════════════════════
#  json_extract — single-level works; nested/array are known gaps
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestJsonExtract:
    def test_single_level_key(self):
        out, _ = _tr("SELECT json_extract(data, '$.name') FROM t")
        assert "data::jsonb->>'name'" in out
        assert 'json_extract' not in out

    def test_nested_key_path(self):
        # FIXED (review #1): nested path → PG #>> path-array accessor.
        out, _ = _tr("SELECT json_extract(data, '$.a.b') FROM t")
        assert 'json_extract' not in out
        assert "data::jsonb#>>'{a,b}'" in out

    def test_array_index(self):
        # FIXED (review #1): array index segments become positional path ints.
        out, _ = _tr("SELECT json_extract(data, '$.items[0].name') FROM t")
        assert 'json_extract' not in out
        assert "data::jsonb#>>'{items,0,name}'" in out

    def test_single_level_still_uses_arrow(self):
        # Single-key path must keep the simpler ->> form (not #>>).
        out, _ = _tr("SELECT json_extract(data, '$.name') FROM t")
        assert "data::jsonb->>'name'" in out


# ═══════════════════════════════════════════════════════════
#  INSERT OR REPLACE → ON CONFLICT (every _PK_MAP table)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInsertOrReplace:
    def test_single_pk_do_update(self):
        # trading_config PK = (key) — a still-mapped single-PK table. (users,
        # the former example, no longer issues INSERT OR REPLACE in-tree and
        # was pruned from _PK_MAP; only its INSERT OR IGNORE seed remains, which
        # doesn't consult _PK_MAP.)
        out, _ = _tr('INSERT OR REPLACE INTO trading_config (key, value) VALUES (?, ?)')
        assert 'INSERT INTO trading_config (key, value)' in out
        assert 'ON CONFLICT (key) DO UPDATE SET' in out
        assert 'value = EXCLUDED.value' in out

    def test_composite_pk(self):
        # trading_intel_crawl_log PK = (crawl_date, category, source_key) — a
        # still-mapped composite-PK table (conversations was migrated off the
        # translator to the Core upsert path).
        out, _ = _tr('INSERT OR REPLACE INTO trading_intel_crawl_log '
                     '(crawl_date, category, source_key, title) VALUES (?, ?, ?, ?)')
        assert 'ON CONFLICT (crawl_date, category, source_key) DO UPDATE SET' in out
        assert 'title = EXCLUDED.title' in out

    def test_all_columns_are_pk_uses_do_nothing(self):
        # When the inserted columns ARE exactly the PK, there is nothing to
        # update → DO NOTHING. trading_bg_tasks PK = (task_id). (conversations
        # and task_events, former examples, were migrated off the translator to
        # the Core upsert path.)
        out, _ = _tr('INSERT OR REPLACE INTO trading_bg_tasks (task_id) '
                     'VALUES (?)')
        assert 'ON CONFLICT (task_id) DO NOTHING' in out

    @pytest.mark.parametrize('table,pk', [
        ('trading_intel_crawl_log', 'crawl_date, category, source_key'),
        ('trading_config', 'key'),
    ])
    def test_known_tables_emit_conflict_target(self, table, pk):
        out, _ = _tr(f'INSERT OR REPLACE INTO {table} ({pk}, payload) '
                     f'VALUES ({", ".join("?" for _ in range(pk.count(",") + 2))})')
        assert f'ON CONFLICT ({pk}) DO UPDATE SET' in out

    def test_unmapped_table_raises_instead_of_silent_drop(self):
        # FIXED (review #1): an unmapped table can't get a correct ON CONFLICT
        # target, so we now raise loudly instead of emitting a DO NOTHING that
        # silently drops the write.
        with pytest.raises(ValueError, match='_PK_MAP'):
            _tr('INSERT OR REPLACE INTO some_unmapped_table (k, v) VALUES (?, ?)')


@pytest.mark.unit
class TestPkMapCompleteness:
    """Every table that issues INSERT OR REPLACE in the codebase must have a
    PK registered in _PK_MAP, otherwise translate_sql now RAISES on PG (the
    fix for the silent-drop data-loss bug). This guard catches a future table
    that adds INSERT OR REPLACE without a _PK_MAP entry.

    SQLite-only virtual tables (FTS5) are exempt: their writes no-op on
    non-SQLite backends and never reach the PG translator.
    """

    # Tables whose only INSERT OR REPLACE runs DIRECTLY on the SQLite
    # connection and never reaches the PG translator, so they need no _PK_MAP
    # entry:
    #   conversations_fts — SQLite FTS5 virtual table (no-op on PG).
    #   schema_meta       — bootstrap version cache; _schema_sqlite.py writes it
    #                        with INSERT OR REPLACE on the raw sqlite3 conn,
    #                        while _schema_pg.py::_write_meta uses native
    #                        ON CONFLICT. Neither path calls translate_sql.
    SQLITE_ONLY = {'conversations_fts', 'schema_meta'}
    # Artifacts that are not real table names (doc/test placeholders).
    NON_TABLES = {'table', 'unmapped', 'some_unmapped_table'}

    def test_all_insert_or_replace_tables_registered(self):
        import glob
        import re

        from lib.database._sql_translate import _get_pk_columns

        tables = set()
        for pat in ('lib/**/*.py', 'routes/**/*.py'):
            for f in glob.glob(pat, recursive=True):
                try:
                    src = open(f, encoding='utf-8').read()
                except OSError:
                    continue
                for m in re.finditer(r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)',
                                     src, re.IGNORECASE):
                    tables.add(m.group(1))

        missing = sorted(
            t for t in tables
            if t not in self.SQLITE_ONLY
            and t not in self.NON_TABLES
            and not _get_pk_columns(t)
        )
        assert not missing, (
            'These tables use INSERT OR REPLACE but are missing from _PK_MAP '
            'in lib/database/_sql_translate.py — on PostgreSQL their writes '
            'would RAISE (previously: silently dropped). Register their PK: '
            f'{missing}')


@pytest.mark.unit
class TestPkMapNoDeadEntries:
    """Reverse of TestPkMapCompleteness: every _PK_MAP entry must be JUSTIFIED.

    An entry is justified if EITHER:
      * an in-tree ``INSERT OR REPLACE INTO <table>`` call-site exists (the
        translator's upsert branch is actually exercised for it), OR
      * the table is on EXTERNAL_TABLES — owned by an out-of-tree package
        (``tofu-trading``) whose upserts still route through this wrapper, so
        it legitimately has no in-tree caller.

    Without this guard, dead entries accumulate every time a table migrates to
    ``_core_schema.upsert()`` but its _PK_MAP line is left behind (exactly the
    cleanup this test was added to lock in). The forward guard
    (TestPkMapCompleteness) only catches the opposite drift.
    """

    # Tables whose INSERT OR REPLACE call-sites live in the external
    # tofu-trading package (not in this repo). They have no in-tree caller by
    # design; keep this list in sync with the trading schema there.
    EXTERNAL_TABLES = {
        'trading_price_cache', 'trading_config', 'trading_fee_rules',
        'trading_daily_briefing', 'trading_bg_tasks', 'trading_intel_crawl_log',
        'trading_strategy_compatibility',
    }

    def test_no_dead_pk_map_entries(self):
        import glob
        import re

        from lib.database._sql_translate import _PK_MAP

        in_tree = set()
        for pat in ('lib/**/*.py', 'routes/**/*.py'):
            for f in glob.glob(pat, recursive=True):
                try:
                    src = open(f, encoding='utf-8').read()
                except OSError:
                    continue
                for m in re.finditer(r'INSERT\s+OR\s+(?:REPLACE|IGNORE)\s+INTO\s+(\w+)',
                                     src, re.IGNORECASE):
                    in_tree.add(m.group(1))

        dead = sorted(
            t for t in _PK_MAP
            if t not in in_tree and t not in self.EXTERNAL_TABLES
        )
        assert not dead, (
            'These _PK_MAP entries in lib/database/_sql_translate.py have NO '
            'in-tree INSERT OR REPLACE call-site and are not on the '
            'EXTERNAL_TABLES allowlist — they are dead weight (the table likely '
            'migrated to _core_schema.upsert()). Remove them, or add to '
            f'EXTERNAL_TABLES if owned by an out-of-tree package: {dead}')


@pytest.mark.unit
class TestInsertOrIgnore:
    def test_insert_or_ignore_adds_do_nothing(self):
        out, _ = _tr('INSERT OR IGNORE INTO users (id) VALUES (?)')
        assert 'INSERT INTO users' in out
        assert 'ON CONFLICT DO NOTHING' in out


@pytest.mark.unit
class TestCachingAndIdempotence:
    def test_translation_is_cached_and_stable(self):
        sql = 'SELECT * FROM t WHERE id = ?'
        a = _tr(sql)
        b = _tr(sql)
        assert a == b

    def test_plain_select_unchanged_except_placeholders(self):
        out, is_pragma = _tr('SELECT a, b FROM t')
        assert out == 'SELECT a, b FROM t'
        assert is_pragma is False
