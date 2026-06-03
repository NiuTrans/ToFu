"""lib/database/_core_schema.py — SQLAlchemy Core table-definition layer (GROUNDWORK).

> **Status: groundwork only.** This module lets NEW tables be defined ONCE
> as SQLAlchemy Core `Table` objects and compiled to correct DDL + DML for
> BOTH backends (PostgreSQL primary, SQLite fallback), retiring the
> hand-maintained twin-DDL + regex `_sql_translate.py` path *for those
> tables*. It is **not yet wired into `init_db()`** and runs **no DDL** —
> registering a table into the live bootstrap is a §10.3 schema change that
> requires explicit sign-off. Until then this is import-safe and inert.

Why SQLAlchemy *Core* (not the ORM)
-----------------------------------
We want a query/DDL **builder**, not an object-relational mapper. Core gives
us:
  - one `Table(...)` definition → `CreateTable(t).compile(dialect=…)` emits
    native PG and SQLite DDL (verified: `JSONB`↔`JSON`, `Identity`↔autoinc,
    paramstyle `%(x)s`↔`?`);
  - dialect-correct `INSERT … ON CONFLICT … DO UPDATE` upserts, replacing the
    `_PK_MAP` table in `_sql_translate.py` for new tables;
  - **no** session/unit-of-work/identity-map machinery, no model classes —
    callers keep using the existing `get_db()` connection + `.execute()`.

It deliberately does NOT open a SQLAlchemy `Engine` or connection. We only
use the *compiler*. The compiled SQL string + params are handed to the
project's existing connection API, so the connection pool, request-scoped
`g` handling, retry helper, and logging in `_core.py` / `_wrappers.py` stay
the single source of truth for execution.

Usage (once a table is approved + wired)
----------------------------------------
    from lib.database._core_schema import define_table, ddl_for, upsert_sql
    import sqlalchemy as sa

    widgets = define_table(
        'widgets',
        sa.Column('id', sa.Text, primary_key=True),
        sa.Column('payload', jsonb_column()),
        sa.Column('created_at', sa.BigInteger),
    )
    # DDL string for the active backend (compile-only):
    sql = ddl_for(widgets)            # CREATE TABLE … (PG or SQLite flavor)
    # then: db.execute(sql); db.commit()   ← existing connection API

The active backend is read from ``lib.database._core._BACKEND`` so the same
call site yields the right dialect at runtime.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.types import JSON

from lib.log import get_logger

logger = get_logger(__name__)

# A private MetaData so these definitions never collide with anything else
# and are never auto-reflected against a live DB.
metadata = sa.MetaData()

# Cached dialect singletons (compile-only; no DBAPI, no connection).
_PG_DIALECT = postgresql.dialect()
_SQLITE_DIALECT = sqlite.dialect()


def _active_dialect():
    """Return the SQLAlchemy dialect matching the project's active backend.

    Reads ``lib.database._core._BACKEND`` ('pg' | 'sqlite') lazily so this
    module stays import-safe even if imported before backend detection.
    Defaults to SQLite (the safe fallback) if the backend is unknown.
    """
    try:
        from lib.database import _core
        backend = getattr(_core, '_BACKEND', 'sqlite')
    except Exception as e:  # pragma: no cover - defensive
        logger.debug('[CoreSchema] backend probe failed, defaulting sqlite: %s', e)
        backend = 'sqlite'
    return _PG_DIALECT if backend == 'pg' else _SQLITE_DIALECT


def jsonb_column(**kw):
    """A JSON column type that is ``JSONB`` on PostgreSQL and ``JSON``
    (stored as TEXT) on SQLite — the dual-backend JSON idiom this project
    uses (PG JSONB, SQLite TEXT)."""
    return postgresql.JSONB().with_variant(JSON, 'sqlite')


def define_table(name: str, *columns, **kw) -> sa.Table:
    """Define a Core ``Table`` on this module's private MetaData.

    Thin wrapper over ``sqlalchemy.Table`` so all groundwork tables share
    one MetaData and a consistent definition site. Does NOT touch any DB.
    """
    return sa.Table(name, metadata, *columns, **kw)


def ddl_for(table: sa.Table, *, dialect=None) -> str:
    """Compile ``CREATE TABLE`` DDL for the active (or given) backend.

    Compile-only — no connection. Returns the SQL string to hand to the
    existing ``db.execute(...)`` path. Note: callers that need
    ``IF NOT EXISTS`` semantics should guard with the existing
    ``_table_exists()`` helper (Core's compiler does not emit it portably).
    """
    d = dialect or _active_dialect()
    return str(CreateTable(table).compile(dialect=d)).strip()


def index_ddl_for(index: sa.Index, *, dialect=None) -> str:
    """Compile ``CREATE INDEX`` DDL for the active (or given) backend."""
    d = dialect or _active_dialect()
    return str(CreateIndex(index).compile(dialect=d)).strip()


def both_ddl(table: sa.Table) -> dict:
    """Return ``{'pg': <ddl>, 'sqlite': <ddl>}`` — handy for tests and for
    eyeballing the twin output of one definition."""
    return {
        'pg': ddl_for(table, dialect=_PG_DIALECT),
        'sqlite': ddl_for(table, dialect=_SQLITE_DIALECT),
    }


def upsert_sql(table: sa.Table, *, conflict_cols, update_cols=None,
               dialect=None) -> str:
    """Compile a dialect-correct ``INSERT … ON CONFLICT … DO UPDATE/NOTHING``.

    Replaces the hand-maintained ``_PK_MAP`` / regex ``INSERT OR REPLACE``
    translation in ``_sql_translate.py`` for new tables. ``update_cols``
    defaults to every non-conflict column (full upsert); pass ``[]`` for
    ``DO NOTHING``.

    Returns the compiled SQL string with the active backend's paramstyle
    (``%(col)s`` for PG, ``?``-style named for SQLite). Compile-only.
    """
    d = dialect or _active_dialect()
    is_pg = d is _PG_DIALECT or getattr(d, 'name', '') == 'postgresql'
    insert = (postgresql.insert if is_pg else sqlite.insert)(table)
    excluded = insert.excluded
    if update_cols is None:
        update_cols = [c.name for c in table.columns if c.name not in conflict_cols]
    if update_cols:
        stmt = insert.on_conflict_do_update(
            index_elements=list(conflict_cols),
            set_={c: getattr(excluded, c) for c in update_cols},
        )
    else:
        stmt = insert.on_conflict_do_nothing(index_elements=list(conflict_cols))
    return str(stmt.compile(dialect=d))
