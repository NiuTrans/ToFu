"""lib/database/_core_schema/_ddl.py — DDL compilation + upsert layer.

The compile-and-execute helpers built on top of the column/table primitives in
``_helpers.py``:

  - ``ddl_for`` / ``index_ddl_for`` / ``both_ddl`` — compile-only DDL rendering;
  - ``create_if_absent`` — the idempotent fresh-install ``CREATE TABLE`` runner;
  - ``upsert_sql`` / ``upsert`` (+ the ``_UPSERT_SQL_CACHE`` memo) — the
    backend-agnostic ``INSERT … ON CONFLICT … DO UPDATE`` layer.

All strictly compile-only except ``create_if_absent`` / ``upsert`` which hand
the compiled SQL to the project's existing connection API (no SQLAlchemy
Engine is ever opened). See the package ``__init__.py`` for the rationale.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from lib.log import get_logger

from ._helpers import (
    _PG_DIALECT,
    _SQLITE_DIALECT,
    _SQLITE_NAMED_DIALECT,
    _active_dialect,
)

logger = get_logger(__name__)


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


def create_if_absent(conn, table: sa.Table, *, table_exists) -> bool:
    """Create ``table`` from its Core definition iff it does not yet exist.

    Idempotent and upgrade-safe: on a populated DB ``table_exists`` returns
    True and this is a no-op (the live table is left exactly as-is — no
    CREATE, no ALTER, no data touched). On a fresh install it runs the
    Core-compiled ``CREATE TABLE``, which parity tests guarantee is
    byte-equivalent to the legacy hand-DDL.

    Args:
        conn: project DB connection wrapper (has ``_conn``).
        table: the Core ``Table`` to create.
        table_exists: backend-appropriate ``_table_exists(conn, name)``
            callable (PG and SQLite implement it differently).

    Returns:
        True if a CREATE was issued (fresh install), False if skipped.
    """
    if table_exists(conn, table.name):
        logger.debug('[CoreSchema] %s exists; skipping create', table.name)
        return False
    ddl = ddl_for(table)
    conn._conn.cursor().execute(ddl)
    logger.info('[CoreSchema] created %s from Core definition', table.name)
    return True


def upsert_sql(table: sa.Table, *, conflict_cols, update_cols=None,
               insert_cols=None, dialect=None) -> str:
    """Compile a dialect-correct ``INSERT … ON CONFLICT … DO UPDATE/NOTHING``.

    Replaces the hand-maintained ``_PK_MAP`` / regex ``INSERT OR REPLACE``
    translation in ``_sql_translate.py`` for migrated tables.

    ``insert_cols`` controls which columns the INSERT lists:

    * ``None`` (default) → ALL table columns. The row-dict must supply every
      column (matches the full-row INSERT OR REPLACE case).
    * an explicit list → a PARTIAL insert of just those columns; the omitted
      columns fall to their schema DEFAULT (or a BEFORE-trigger, e.g.
      ``conversations.search_tsv``). This mirrors the legacy ``REPLACE``-with-
      column-subset form where unnamed columns took their defaults. Conflict
      columns are auto-included.

    ``update_cols`` = columns overwritten on conflict; ``None`` defaults to the
    inserted columns minus the conflict columns (so a partial insert updates
    exactly what it wrote); pass ``[]`` for ``DO NOTHING``.

    The output uses NAMED binds on BOTH backends — ``%(col)s`` (PG pyformat)
    and ``:col`` (SQLite named) — so a single row-dict binds either one. Bind
    it with :func:`upsert` rather than hand-passing a tuple. Compile-only.
    """
    d = dialect or _active_dialect()
    is_pg = d is _PG_DIALECT or getattr(d, 'name', '') == 'postgresql'
    if not is_pg:
        # Force named paramstyle so SQLite is dict-bindable like PG.
        d = _SQLITE_NAMED_DIALECT
    insert = (postgresql.insert if is_pg else sqlite.insert)(table)
    if insert_cols is not None:
        # Partial insert: list only the requested columns (conflict cols always
        # included so ON CONFLICT has its target values). Omitted columns use
        # their DEFAULT / trigger. Bind each by name via the row-dict.
        cols = list(dict.fromkeys(list(conflict_cols) + list(insert_cols)))
        insert = insert.values({c: sa.bindparam(c) for c in cols})
        default_update_pool = [c for c in cols if c not in conflict_cols]
    else:
        default_update_pool = [c.name for c in table.columns if c.name not in conflict_cols]
    excluded = insert.excluded
    if update_cols is None:
        update_cols = default_update_pool
    if update_cols:
        stmt = insert.on_conflict_do_update(
            index_elements=list(conflict_cols),
            set_={c: getattr(excluded, c) for c in update_cols},
        )
    else:
        stmt = insert.on_conflict_do_nothing(index_elements=list(conflict_cols))
    return str(stmt.compile(dialect=d))


# Memoized compiled-SQL cache for upsert(): keyed by
# (table name, conflict tuple, update tuple-or-None, backend). The compiled
# string is dialect-stable, so hot-path callers (per-message conversation
# writes, per-event task_events) never recompile. Bounded by the finite set
# of (table, column-set) combinations in the codebase.
_UPSERT_SQL_CACHE: dict = {}


def upsert(db, table: sa.Table, row: dict, *, conflict_cols=None,
           update_cols=None, insert_cols=None, commit=None, retry=False):
    """Backend-agnostic UPSERT — the single reusable replacement for every
    ``INSERT OR REPLACE INTO <t> … VALUES(?…)`` call-site.

    This is the canonical pattern for ALL migrated tables, kv and hot-path
    alike: the caller passes a plain ``{column: value}`` dict and never sees
    SQL or paramstyle. Internally it compiles (once, then cached) a
    dialect-correct ``INSERT … ON CONFLICT … DO UPDATE`` via :func:`upsert_sql`
    for the ACTIVE backend and binds ``row`` by name on both PG and SQLite.

    Args:
        db: a thread-local connection (``get_thread_db(...)``).
        table: the Core ``Table`` (e.g. ``PRICING_CACHE``).
        row: ``{column_name: value}`` for every column being written. Keys
            must cover all NOT-NULL-without-default columns; extra keys error.
        conflict_cols: the conflict target. Defaults to the table's primary-key
            columns (the common case — matches the old INSERT OR REPLACE).
        update_cols: columns to overwrite on conflict; ``None`` = the inserted
            columns minus the conflict columns (full replace of what was
            written, matching INSERT OR REPLACE), ``[]`` = DO NOTHING.
        insert_cols: restrict the INSERT to this subset of columns (omitted
            columns fall to their DEFAULT / trigger). ``None`` = all table
            columns, so ``row`` must supply every one. Use the subset form for
            partial writers like the feishu ``conversations`` sync that only
            sets some columns and relies on defaults (e.g. ``settings``) and
            the ``search_tsv`` trigger for the rest. Keys of ``row`` must match
            the inserted columns (conflict cols auto-included).
        commit: REQUIRED unless ``retry=True``. If True, commit after
            executing (kv/one-shot callers); pass False explicitly inside a
            larger transaction (hot-path batch writers commit once at the
            end). Requiring the choice prevents an omitted keyword from
            leaking a write transaction until request teardown/watchdog.
        retry: if True, route the write through
            ``lib.database.db_execute_with_retry`` (retries on contention /
            transient connection loss). Use for call-sites that previously
            wrapped their INSERT OR REPLACE in that helper, and for hot-path
            writers under concurrency. Returns ``None`` in this mode (the
            retry helper does not surface a cursor). ALWAYS commits (the retry
            helper rolls back between attempts, so a non-committed retry is
            meaningless) — the ``commit`` arg is ignored when ``retry=True``.
            For an in-transaction write that must NOT commit, use retry=False.

    Returns the cursor from ``db.execute`` (or ``None`` when ``retry=True``).
    """
    if commit is None and not retry:
        raise TypeError(
            'upsert() requires an explicit commit=True/False choice unless '
            'retry=True owns the commit')
    if conflict_cols is None:
        conflict_cols = [c.name for c in table.primary_key.columns]
    from lib.database import _core
    backend = getattr(_core, '_BACKEND', 'sqlite')
    cache_key = (
        table.name, tuple(conflict_cols),
        tuple(update_cols) if update_cols is not None else None,
        tuple(insert_cols) if insert_cols is not None else None, backend,
    )
    sql = _UPSERT_SQL_CACHE.get(cache_key)
    if sql is None:
        sql = upsert_sql(table, conflict_cols=conflict_cols,
                         update_cols=update_cols, insert_cols=insert_cols)
        _UPSERT_SQL_CACHE[cache_key] = sql
    if retry:
        # db_execute_with_retry's contract is execute-AND-commit (it rolls back
        # between retry attempts, so a non-committed retry is meaningless). It
        # defaults commit=True; force it here so a caller that omitted commit=
        # still gets a durable, cross-connection-visible write — matching the
        # legacy db_execute_with_retry call-sites this replaced. (A bug where
        # retry=True + default commit=False left the write uncommitted made
        # rows invisible to the async read pool — see conversation-search.)
        from lib.database import db_execute_with_retry
        db_execute_with_retry(db, sql, row, commit=True)
        return None
    cur = db.execute(sql, row)
    if commit:
        db.commit()
    return cur
