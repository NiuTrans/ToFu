"""lib/database/_core_schema — SQLAlchemy Core table-definition layer.

> **Status: live — migration COMPLETE (2026-06).** Every table in the
> dual-backend schema is now defined ONCE here as a SQLAlchemy Core `Table`
> object and compiled to correct DDL + DML for BOTH backends (PostgreSQL
> primary, SQLite fallback). `_schema_pg.py` / `_schema_sqlite.py` no longer
> hand-author any `CREATE TABLE`: they import each Core table and create it via
> `create_if_absent`, then apply the backend-specific extras Core can't express
> (indexes, PG-only full-text `tsvector`/GIN/trigger infra, and upgrade-only
> `ALTER TABLE` migrations). This retired the hand-maintained twin-DDL AND the
> `INSERT OR REPLACE`/`_PK_MAP` upsert branch of `_sql_translate.py` — but NOT
> the translator itself, which remains the permanent SQLite→PG dialect bridge
> (`?`→`%s`, `json_extract`, `strftime`, …) that runs on every query on both
> backends.
>
> `create_if_absent` runs the Core-compiled `CREATE TABLE` on a fresh install
> and is a no-op on a populated DB. Parity tests
> (`tests/test_core_schema_parity.py`) prove the generated DDL is
> byte-equivalent to the legacy hand-DDL on both backends, so the migration
> required no `_SCHEMA_VERSION` bump. Defining a NEW table is now a one-place
> change here (plus a parity test) — a §10.3 schema change requiring explicit
> sign-off.

Package layout (facade-preserving split of the former single module)
--------------------------------------------------------------------
This package was split out of a single ~1200-line ``_core_schema.py`` for
maintainability; the import path (``lib.database._core_schema``) and every
public symbol are UNCHANGED — this ``__init__`` re-exports them all so
``from lib.database._core_schema import X`` keeps working byte-identically.

  - ``_helpers`` — the private ``MetaData``, the compile-only dialect
    singletons, the dialect-variant column factories, AND the two per-dialect
    server-default ``FunctionElement`` constructs (``epoch_now`` /
    ``now_timestamp``) together with their ``@compiles`` registrations.
  - ``_ddl`` — DDL compilation (``ddl_for`` / ``index_ddl_for`` / ``both_ddl``),
    the ``create_if_absent`` runner, and the ``upsert_sql`` / ``upsert`` layer.
  - ``_tables`` — every module-level ``sa.Table`` definition (the COMPLETE
    dual-backend schema), each registered ONCE on the shared ``MetaData``.

CRITICAL — @compiles import side-effect: importing ``_helpers`` below runs the
``@compiles(epoch_now, ...)`` / ``@compiles(now_timestamp, ...)`` decorators,
which register GLOBAL SQLAlchemy dialect compilers. Those registrations MUST
fire on package import so that a table using ``epoch_now()`` (e.g.
``TRANSCRIPT_ARCHIVE``) emits correct per-dialect DDL. Keeping the classes with
their compiler fns in ONE submodule imported here guarantees this.

CRITICAL — single-MetaData registration: every ``sa.Table`` is defined exactly
once, in ``_tables`` (which imports the shared ``MetaData`` from ``_helpers``).
Defining a table twice (via a double import) would raise 'table already defined
in MetaData'. This ``__init__`` imports ``_tables`` exactly once.

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

Usage (defining + wiring a new table)
-------------------------------------
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

from lib.log import get_logger

logger = get_logger(__name__)

# ── _helpers: MetaData + dialects + column factories + epoch_now/now_timestamp.
# Importing this module FIRES the @compiles registrations (import side-effect).
from ._helpers import (  # noqa: E402,F401
    metadata,
    _PG_DIALECT,
    _SQLITE_DIALECT,
    _SQLITE_NAMED_DIALECT,
    _active_dialect,
    jsonb_column,
    epoch_now,
    now_timestamp,
    _epoch_now_pg,
    _epoch_now_sqlite,
    _now_timestamp_pg,
    _now_timestamp_sqlite,
    timestamptz_column,
    autoincrement_pk,
    bigint_autoincrement_pk,
    double_column,
    bool_column,
    bigint_column,
    define_table,
)

# ── _ddl: DDL compilation + create_if_absent + upsert layer.
from ._ddl import (  # noqa: E402,F401
    ddl_for,
    index_ddl_for,
    both_ddl,
    create_if_absent,
    upsert_sql,
    upsert,
    _UPSERT_SQL_CACHE,
)

# ── _tables: every sa.Table (registered ONCE on the shared MetaData).
from ._tables import (  # noqa: E402,F401
    DAILY_COST_CACHE,
    SCHEMA_META,
    USERS,
    CONVERSATIONS,
    CONVERSATION_MESSAGES,
    TRADING_CONFIG,
    PRICING_CACHE,
    RECENT_PROJECTS,
    PAPER_REPORTS,
    PAPER_LIBRARY,
    PAPER_TRANSLATIONS,
    PAPER_PODCASTS,
    TASK_RESULTS,
    TASK_EVENTS,
    CHAT_ARTIFACTS,
    TRANSCRIPT_ARCHIVE,
    MESSAGE_QUEUE,
    SCHEDULED_TASKS,
    PROACTIVE_POLL_LOG,
    TIMER_WATCHERS,
    TIMER_POLL_LOG,
    SWARM_SESSIONS,
    SWARM_AGENTS,
    ORCHESTRATION_RUNS,
    ORCHESTRATION_RUN_EVENTS,
    PROJECT_EVENTS,
    PROJECT_CHARTER,
    PROJECT_TASKS,
    PROJECT_STATUS_SNAPSHOTS,
    PROJECT_WATCH_ITEMS,
    PROJECT_WATCH_RESPONSES,
    OPTIMIZER_PROPOSALS,
    OPTIMIZER_ACTION_LOG,
    RATE_LIMIT_EVENTS,
    ERROR_RESOLUTIONS,
    TENANT_USERS,
    BILLING_LEDGER,
    BILLING_WALLETS,
    BILLING_REDEEM_CODES,
    BILLING_PAYMENTS,
)

# Snapshot of the REAL Core table set, taken AFTER this package's own
# registration completes (the ``_tables`` import above). The boot probe
# (``core_boot_table_names``) derives from THIS — never the live shared
# MetaData — so later ``define_table()`` calls on that MetaData (compile-only
# test fixtures in test_core_schema_groundwork, domain plugins) cannot leak
# into the probe and fool it into forcing a full DDL pass on every boot for
# tables the bootstrap intentionally never creates.
_CORE_REGISTERED_TABLES = frozenset(metadata.tables.keys())

__all__ = [
    # ── helpers / dialects / MetaData ──
    'metadata',
    '_PG_DIALECT',
    '_SQLITE_DIALECT',
    '_SQLITE_NAMED_DIALECT',
    '_active_dialect',
    'jsonb_column',
    'epoch_now',
    'now_timestamp',
    'timestamptz_column',
    'autoincrement_pk',
    'bigint_autoincrement_pk',
    'double_column',
    'bool_column',
    'bigint_column',
    'define_table',
    '_CORE_REGISTERED_TABLES',
    # ── DDL + upsert layer ──
    'ddl_for',
    'index_ddl_for',
    'both_ddl',
    'create_if_absent',
    'upsert_sql',
    'upsert',
    '_UPSERT_SQL_CACHE',
    # ── tables ──
    'DAILY_COST_CACHE',
    'SCHEMA_META',
    'USERS',
    'CONVERSATIONS',
    'CONVERSATION_MESSAGES',
    'TRADING_CONFIG',
    'PRICING_CACHE',
    'RECENT_PROJECTS',
    'PAPER_REPORTS',
    'PAPER_LIBRARY',
    'PAPER_TRANSLATIONS',
    'PAPER_PODCASTS',
    'TASK_RESULTS',
    'TASK_EVENTS',
    'CHAT_ARTIFACTS',
    'TRANSCRIPT_ARCHIVE',
    'MESSAGE_QUEUE',
    'SCHEDULED_TASKS',
    'PROACTIVE_POLL_LOG',
    'TIMER_WATCHERS',
    'TIMER_POLL_LOG',
    'SWARM_SESSIONS',
    'SWARM_AGENTS',
    'ORCHESTRATION_RUNS',
    'ORCHESTRATION_RUN_EVENTS',
    'PROJECT_EVENTS',
    'PROJECT_CHARTER',
    'PROJECT_TASKS',
    'PROJECT_STATUS_SNAPSHOTS',
    'PROJECT_WATCH_ITEMS',
    'PROJECT_WATCH_RESPONSES',
    'OPTIMIZER_PROPOSALS',
    'OPTIMIZER_ACTION_LOG',
    'RATE_LIMIT_EVENTS',
    'ERROR_RESOLUTIONS',
    'TENANT_USERS',
    'BILLING_LEDGER',
    'BILLING_WALLETS',
    'BILLING_REDEEM_CODES',
    'BILLING_PAYMENTS',
]
