"""Regression guard for the scheduler-predicate schema self-heal.

Root cause it locks down (2026-07-15): commits 4619ab1/4b72fc0 added the
predicate-promotion columns (condition_kind / condition_command /
condition_regex / tier / predicate_matched / llm_agreed) to the timer +
scheduler tables via ALTER migrations in ``_init_system_schema``, but did NOT
bump ``_SCHEMA_VERSION`` and did NOT register the load-bearing columns in
``_CRITICAL_COLUMNS``. On an existing DB (already stamped at the prior version)
the fast-startup gate skips ALL DDL, so the columns were never added and every
``timer_create`` / ``create_task`` threw ``no column named condition_kind``.

These tests assert the two invariants that make such a divergence self-heal:
  1. Both backends' ``_CRITICAL_COLUMNS`` sets AGREE (a column critical on PG
     must be critical on SQLite and vice-versa).
  2. The critical set COVERS every predicate column named directly in an INSERT
     — the columns whose absence makes create_timer/create_task/_record_poll
     throw — so the version fast-path is forced to re-migrate an old DB.

No live DB, no LLM. Pure structural assertions on the schema modules.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# The predicate columns each subsystem names DIRECTLY in an INSERT/UPDATE, so
# their absence on an already-current DB throws (the exact 2026-07-15 bug).
# This is the contract the self-heal must cover, kept next to the assertions
# so a future predicate-column addition updates it here too.
_REQUIRED_PREDICATE_CRITICAL = {
    'timer_watchers': {'condition_kind', 'condition_command', 'condition_regex'},
    'scheduled_tasks': {'condition_kind', 'condition_command', 'condition_regex'},
    'timer_poll_log': {'tier', 'predicate_matched', 'llm_agreed'},
    'proactive_poll_log': {'tier', 'predicate_matched', 'llm_agreed'},
}


def _critical(module_path):
    mod = __import__(module_path, fromlist=['_CRITICAL_COLUMNS'])
    return {t: set(cols) for t, cols in mod._CRITICAL_COLUMNS.items()}


def test_sqlite_critical_columns_cover_predicate_columns():
    crit = _critical('lib.database._schema_sqlite._selfheal')
    for table, required in _REQUIRED_PREDICATE_CRITICAL.items():
        assert required <= crit.get(table, set()), (
            f'{table}: SQLite _CRITICAL_COLUMNS missing {required - crit.get(table, set())} '
            f'— the version fast-path will skip the ALTER on an existing DB')


def test_pg_critical_columns_cover_predicate_columns():
    crit = _critical('lib.database._schema_pg._selfheal')
    for table, required in _REQUIRED_PREDICATE_CRITICAL.items():
        assert required <= crit.get(table, set()), (
            f'{table}: PG _CRITICAL_COLUMNS missing {required - crit.get(table, set())} '
            f'— the version fast-path will skip the ALTER on an existing DB')


def test_both_backends_critical_columns_agree():
    """A column critical on one backend must be critical on the other — the two
    self-heal sets can never drift, or a divergence heals on only one backend."""
    sqlite_crit = _critical('lib.database._schema_sqlite._selfheal')
    pg_crit = _critical('lib.database._schema_pg._selfheal')
    assert sqlite_crit == pg_crit, (
        f'critical-column sets differ between backends:\n'
        f'  sqlite-only: {set(sqlite_crit) - set(pg_crit)}\n'
        f'  pg-only:     {set(pg_crit) - set(sqlite_crit)}')


def test_predicate_critical_columns_exist_on_core_tables():
    """Every predicate column we mark critical must actually be DEFINED on the
    Core table — otherwise the self-heal would loop forever trying to converge
    on a column the create path never adds."""
    sa = pytest.importorskip('sqlalchemy')  # noqa: F841
    from lib.database import _core_schema as cs
    core_tables = {t.name: {c.name for c in t.columns} for t in cs.metadata.tables.values()}
    for table, required in _REQUIRED_PREDICATE_CRITICAL.items():
        assert table in core_tables, f'{table} not defined on Core metadata'
        missing = required - core_tables[table]
        assert not missing, f'{table}: predicate columns {missing} marked critical but not on Core table'


def test_schema_version_matches_across_backends():
    """The two backends must pin the SAME _SCHEMA_VERSION — a mismatch means one
    backend re-migrates while the other fast-paths, hiding a divergence."""
    from lib.database._schema_sqlite._meta import _SCHEMA_VERSION as lite_v
    from lib.database._schema_pg._meta import _SCHEMA_VERSION as pg_v
    assert lite_v == pg_v, f'schema version drift: sqlite={lite_v} pg={pg_v}'
