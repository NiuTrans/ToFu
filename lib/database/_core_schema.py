"""lib/database/_core_schema.py — SQLAlchemy Core table-definition layer.

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

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.expression import FunctionElement

from lib.log import get_logger

logger = get_logger(__name__)

# A private MetaData so these definitions never collide with anything else
# and are never auto-reflected against a live DB.
metadata = sa.MetaData()

# Cached dialect singletons (compile-only; no DBAPI, no connection).
_PG_DIALECT = postgresql.dialect()
_SQLITE_DIALECT = sqlite.dialect()
# SQLite dialect that emits NAMED binds (`:col`) instead of positional `?`, so
# an upsert can bind from the SAME row-dict the PG path uses (PG psycopg2's
# pyformat already emits `%(col)s`). This is the single-row-dict contract that
# makes `upsert()` backend-agnostic. psycopg2 (PG) and sqlite3 both accept a
# Mapping of params, so one dict drives both.
_SQLITE_NAMED_DIALECT = sqlite.dialect(paramstyle='named')


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
    """A JSON column that is ``JSONB`` on PostgreSQL and ``TEXT`` on SQLite —
    the dual-backend JSON idiom this project uses.

    The SQLite variant is ``sa.Text`` (declared ``TEXT``), NOT SQLAlchemy's
    ``JSON`` type: the live hand-DDL declares these columns ``TEXT``, and on
    SQLite the declared type drives column affinity — ``TEXT`` yields TEXT
    affinity whereas ``JSON`` (matching no affinity-keyword substring) yields
    NUMERIC affinity. Emitting ``JSON`` would silently diverge a fresh Core
    install from every existing DB. The project serializes JSON to a string
    itself, so TEXT storage is exactly right."""
    return postgresql.JSONB().with_variant(sa.Text, 'sqlite')


class epoch_now(FunctionElement):
    """A server-side ``DEFAULT`` that yields the current Unix epoch seconds,
    compiled per-dialect to match the project's live hand-DDL exactly:

    * PostgreSQL → ``EXTRACT(EPOCH FROM NOW())::BIGINT``
    * SQLite     → ``strftime('%s','now')``

    Use as ``server_default=epoch_now()``. A single literal ``server_default``
    can't differ per backend, so this custom construct is the only way to
    reproduce the two different default *expressions* from one definition."""
    name = 'epoch_now'
    inherit_cache = True


@compiles(epoch_now, 'postgresql')
def _epoch_now_pg(element, compiler, **kw):  # noqa: D401
    return 'EXTRACT(EPOCH FROM NOW())::BIGINT'


@compiles(epoch_now, 'sqlite')
def _epoch_now_sqlite(element, compiler, **kw):  # noqa: D401
    return "strftime('%s','now')"


class now_timestamp(FunctionElement):
    """A server-side ``DEFAULT`` yielding the current wall-clock timestamp,
    compiled per-dialect to match the live hand-DDL:

    * PostgreSQL → ``NOW()``
    * SQLite     → ``datetime('now')``

    Use as ``server_default=now_timestamp()``, paired with
    ``timestamptz_column()`` (PG ``TIMESTAMPTZ`` / SQLite ``TEXT``). This is the
    human-readable timestamp idiom (``users.created_at``); contrast
    ``epoch_now()`` which yields integer epoch seconds for the bigint columns."""
    name = 'now_timestamp'
    inherit_cache = True


@compiles(now_timestamp, 'postgresql')
def _now_timestamp_pg(element, compiler, **kw):  # noqa: D401
    return 'NOW()'


@compiles(now_timestamp, 'sqlite')
def _now_timestamp_sqlite(element, compiler, **kw):  # noqa: D401
    return "datetime('now')"


def timestamptz_column():
    """A timestamp column that is ``TIMESTAMPTZ`` on PostgreSQL and ``TEXT`` on
    SQLite — the live idiom for human-readable timestamps (``users.created_at``).

    SQLite has no native timestamp type and the live DDL stores these as
    ``TEXT`` (``datetime('now')`` strings), so the SQLite variant is ``sa.Text``.
    On PG, ``sa.DateTime(timezone=True)`` renders ``TIMESTAMP WITH TIME ZONE``
    — the canonical spelling of the ``TIMESTAMPTZ`` alias used in hand-DDL;
    the parity ``_norm`` treats the two spellings as equal."""
    return sa.DateTime(timezone=True).with_variant(sa.Text, 'sqlite')


def autoincrement_pk():
    """An auto-incrementing integer primary-key column rendering ``SERIAL`` on
    PostgreSQL and ``INTEGER ... AUTOINCREMENT`` on SQLite.

    Two pieces are required to match the live hand-DDL:

    * this column (``sa.Integer`` + ``primary_key=True``) → ``SERIAL`` on PG,
      ``INTEGER`` on SQLite;
    * the table MUST also be defined with ``sqlite_autoincrement=True`` so
      SQLite emits the explicit ``AUTOINCREMENT`` keyword (otherwise SQLite
      uses an implicit rowid alias, diverging from the live DDL).

    Pass the kwarg via ``define_table(..., sqlite_autoincrement=True)``."""
    return sa.Column('id', sa.Integer, primary_key=True)


def bigint_autoincrement_pk():
    """An auto-incrementing **64-bit** integer primary-key column rendering
    ``BIGSERIAL`` on PostgreSQL and ``INTEGER ... AUTOINCREMENT`` on SQLite.

    Like :func:`autoincrement_pk` but for tables whose live PG DDL uses
    ``BIGSERIAL`` (e.g. ``rate_limit_events`` — a high-churn event log whose id
    can exceed 32 bits). ``sa.BigInteger`` + ``primary_key=True`` renders
    ``BIGSERIAL`` on PG; the SQLite variant is pinned to ``Integer`` so it stays
    ``INTEGER PRIMARY KEY AUTOINCREMENT`` (the only type eligible to alias the
    rowid). As with :func:`autoincrement_pk`, the table MUST also be defined
    with ``sqlite_autoincrement=True`` so SQLite emits the ``AUTOINCREMENT``
    keyword."""
    return sa.Column('id', sa.BigInteger().with_variant(sa.Integer, 'sqlite'),
                     primary_key=True)


def double_column():
    """A float column that is ``DOUBLE PRECISION`` on PostgreSQL and ``REAL`` on
    SQLite — the live idiom for cost/amount columns.

    ``sa.Double`` alone renders ``DOUBLE`` on SQLite (live uses ``REAL``), and
    ``sa.Float`` renders ``FLOAT`` on PG (live uses ``DOUBLE PRECISION``), so we
    pin both halves: ``sa.Double`` for PG, ``sa.REAL`` variant for SQLite."""
    return sa.Double().with_variant(sa.REAL, 'sqlite')


def bool_column():
    """A boolean column that is ``BOOLEAN`` on PostgreSQL and ``INTEGER`` on
    SQLite.

    The live hand-DDL uses ``BOOLEAN`` on PG but ``INTEGER`` (0/1) on SQLite,
    which has no native boolean type — and as with ``jsonb_column``/
    ``bigint_column``, the *declared* SQLite type drives affinity, so we pin
    the SQLite variant to ``Integer`` rather than let SQLAlchemy emit
    ``BOOLEAN`` (which would carry NUMERIC affinity and a CHECK constraint on
    some configs). Pair with ``server_default=sa.false()`` / ``sa.true()`` —
    those render ``false``/``true`` on PG and ``0``/``1`` on SQLite, matching
    the live defaults on both."""
    return sa.Boolean().with_variant(sa.Integer, 'sqlite')


def bigint_column():
    """An integer column that is ``BIGINT`` on PostgreSQL and ``INTEGER`` on
    SQLite.

    The project's hand-DDL uses ``BIGINT`` on PG (epoch-ms timestamps exceed
    32-bit) but plain ``INTEGER`` on SQLite — where ``INTEGER`` is already a
    64-bit signed value AND the only type eligible to alias the rowid, so
    emitting ``BIGINT`` there would both mismatch the live schema and change
    affinity. ``sa.BigInteger`` alone renders ``BIGINT`` on *both* dialects,
    so we pin the SQLite variant to ``Integer`` to reproduce the live shape."""
    return sa.BigInteger().with_variant(sa.Integer, 'sqlite')


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


# ═══════════════════════════════════════════════════════════════════════
#  Registered tables — the COMPLETE dual-backend schema (migration done 2026-06).
#
#  Every table the project creates lives here; _schema_pg.py / _schema_sqlite.py
#  no longer hand-author CREATE TABLE. A table appears here only after its
#  parity test in tests/test_core_schema_parity.py is green on BOTH backends,
#  proving the Core-generated DDL is byte-equivalent to the live hand-DDL. The
#  DDL is unchanged, so NO _SCHEMA_VERSION bump is required.
# ═══════════════════════════════════════════════════════════════════════

# daily_cost_cache — pre-aggregated per-day LLM cost. Composite PK
# (user_id, date 'YYYY-MM-DD'). cost is DOUBLE PRECISION/REAL, conversations_json
# is JSONB/TEXT. Exercises double_column + composite-PK upsert (queries-on-Core).
DAILY_COST_CACHE = define_table(
    'daily_cost_cache',
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('date', sa.Text, nullable=False),
    sa.Column('cost', double_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('conversations_json', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('computed_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('user_id', 'date'),
)

# schema_meta — core-owned key/value store for bootstrap metadata
# (schema version + active-domain set). Same shape as trading_config; lives in
# core so the fast-startup version cache survives when the trading domain is
# disabled or extracted into its own package. See lib/database/schema_registry.py.
SCHEMA_META = define_table(
    'schema_meta',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False, server_default=''),
)

# users — account table. Auto-increment PK (SERIAL/INTEGER AUTOINCREMENT),
# unique username, and a human-readable created_at (TIMESTAMPTZ/TEXT) defaulting
# to NOW()/datetime('now'). FK target for conversations.user_id.
USERS = define_table(
    'users',
    autoincrement_pk(),
    sa.Column('username', sa.Text, nullable=False, unique=True),
    sa.Column('display_name', sa.Text, nullable=False, server_default=''),
    sa.Column('password_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', timestamptz_column(), server_default=now_timestamp()),
    sqlite_autoincrement=True,
)

# conversations — chat transcripts. OPTION B migration: Core owns the shared
# base columns (incl. search_text, which SQLite's base CREATE carries and which
# PG adds via a _column_exists-guarded ALTER that becomes a no-op once Core
# emits the column). The PG-ONLY full-text infrastructure — the search_tsv
# tsvector column, pg_trgm EXTENSION, idx_conv_search_trgm / idx_conv_search_tsv
# GIN indexes, and the conversations_search_tsv_trg trigger + function — CANNOT
# be expressed by create_if_absent's single ddl_for and remain as explicit,
# guarded post-create DDL in _schema_pg.py (exactly as before). messages /
# settings are JSONB on PG, TEXT on SQLite. Composite PK (id, user_id); FK to
# users(id) ON DELETE CASCADE.
CONVERSATIONS = define_table(
    'conversations',
    sa.Column('id', sa.Text, nullable=False),
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default='New Chat'),
    sa.Column('messages', jsonb_column(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
    sa.Column('settings', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('msg_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('search_text', sa.Text, nullable=False, server_default=''),
    sa.PrimaryKeyConstraint('id', 'user_id'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
)

# conversation_messages — Phase 5 "messages-as-rows". The per-message row
# store that the conversations.messages JSONB array migrates INTO. Landing
# migrator-first behind the TOFU_MESSAGES_ROWS flag (lib/database/messages_rows.py):
# a one-shot idempotent backfill + dual-write, with reads gated on a proven
# byte-identical build_search_text reconstruction BEFORE any read cutover.
#
# Column split rationale: the four columns build_search_text() actually reads —
# role, content, thinking, translated_content — are first-class so the search
# blob can be reconstructed from rows alone (the verification invariant). The
# whole original message dict (incl. _msgId, timestamp, finishReason, usage,
# toolRounds, model, modifiedFileList, …) is preserved verbatim in meta JSONB,
# so a row round-trips back to the exact JSONB element with no field loss.
# content_json holds multipart content (list of text/image parts) as a JSON
# string; content holds the plain-string form. Exactly one is populated per row
# (mirrors the str-vs-list branch in build_search_text). Composite PK
# (conv_id, seq) preserves order; (conv_id, msg_id) is separately UNIQUE for
# index-free addressing. FK to conversations(id) is intentionally OMITTED —
# conversations has a COMPOSITE PK (id, user_id), so a single-column FK can't
# target it; the migrator/dual-writer scope rows by conv_id within the owning
# user's write path.
CONVERSATION_MESSAGES = define_table(
    'conversation_messages',
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('msg_id', sa.Text, nullable=False, server_default=''),
    sa.Column('role', sa.Text, nullable=False, server_default=''),
    sa.Column('content', sa.Text, nullable=False, server_default=''),
    sa.Column('content_json', jsonb_column(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column('thinking', sa.Text, nullable=False, server_default=''),
    sa.Column('translated_content', sa.Text, nullable=False, server_default=''),
    sa.Column('meta', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('conv_id', 'seq'),
)

# trading_config — key/value store; identical shape on PG + SQLite.
TRADING_CONFIG = define_table(
    'trading_config',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False, server_default=''),
)

# pricing_cache — system key/value cache with an epoch-ms timestamp.
PRICING_CACHE = define_table(
    'pricing_cache',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# recent_projects — MRU list keyed by filesystem path.
RECENT_PROJECTS = define_table(
    'recent_projects',
    sa.Column('path', sa.Text, primary_key=True),
    sa.Column('count', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('last_used', bigint_column(), nullable=False),
)

# paper_reports — persistent cache for paper analysis reports; composite PK
# (paper_hash, lang). All TEXT + bigint created_at. `meta` is a JSON blob
# holding the resolved generation model + token usage + cost (rendered as a
# "finish tag" badge under the report); '' on legacy rows.
PAPER_REPORTS = define_table(
    'paper_reports',
    sa.Column('paper_hash', sa.Text, nullable=False),
    sa.Column('lang', sa.Text, nullable=False, server_default='en'),
    sa.Column('report', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('meta', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('paper_hash', 'lang'),
)

# paper_library — server-side bookshelf; composite PK (id, user_id), FK to
# users. qa_history/images/babel_cache are plain TEXT (json.dumps strings, NOT
# JSONB) on both backends — matches the live DDL.
PAPER_LIBRARY = define_table(
    'paper_library',
    sa.Column('id', sa.Text, nullable=False),
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('pdf_url', sa.Text, nullable=False, server_default=''),
    sa.Column('pdf_filename', sa.Text, nullable=False, server_default=''),
    sa.Column('arxiv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('paper_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('parsed_text', sa.Text, nullable=False, server_default=''),
    sa.Column('qa_history', sa.Text, nullable=False, server_default='[]'),
    sa.Column('images', sa.Text, nullable=False, server_default='[]'),
    sa.Column('babel_cache', sa.Text, nullable=False, server_default='{}'),
    sa.Column('page_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('id', 'user_id'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
)

# paper_translations — Babel-mode whole-paper translation cache; composite PK
# (paper_hash, lang). created_at is epoch-ms (bigint on PG, integer on SQLite).
PAPER_TRANSLATIONS = define_table(
    'paper_translations',
    sa.Column('paper_hash', sa.Text, nullable=False),
    sa.Column('lang', sa.Text, nullable=False),
    sa.Column('text', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('paper_hash', 'lang'),
)

# task_results — persisted chat/task output. Single-col PK; several nullable
# TEXT columns (no default) and a nullable completed_at timestamp.
TASK_RESULTS = define_table(
    'task_results',
    sa.Column('task_id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('content', sa.Text, nullable=False, server_default=''),
    sa.Column('thinking', sa.Text, nullable=False, server_default=''),
    sa.Column('error', sa.Text),
    sa.Column('status', sa.Text, nullable=False, server_default='done'),
    sa.Column('tool_rounds', sa.Text),
    sa.Column('search_results', sa.Text),
    sa.Column('metadata', sa.Text),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('completed_at', bigint_column()),
)

# task_events — persisted SSE event log; composite PK (task_id, event_id).
# payload is JSONB on PG / TEXT on SQLite.
TASK_EVENTS = define_table(
    'task_events',
    sa.Column('task_id', sa.Text, nullable=False),
    sa.Column('event_id', bigint_column(), nullable=False),
    sa.Column('ts_ms', bigint_column(), nullable=False),
    sa.Column('type', sa.Text, nullable=False),
    sa.Column('payload', jsonb_column(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'event_id'),
)

# chat_artifacts — renderable reports promoted out of chat (md/html/svg).
# Single-col PK; two JSONB columns with '{}' default, a BOOLEAN pinned flag,
# and bigint timestamps. Exercises jsonb_column + bool_column + bigint_column.
CHAT_ARTIFACTS = define_table(
    'chat_artifacts',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('msg_id', sa.Text, nullable=False, server_default=''),
    sa.Column('source', sa.Text, nullable=False),
    sa.Column('source_ref', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('format', sa.Text, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('content', sa.Text, nullable=False),
    sa.Column('content_sha256', sa.Text, nullable=False),
    sa.Column('size_bytes', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('version', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('parent_id', sa.Text, nullable=False, server_default=''),
    sa.Column('pinned', bool_column(), nullable=False, server_default=sa.false()),
    sa.Column('meta', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('deleted_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# transcript_archive — pre-compaction snapshots + metadata. Auto-increment PK
# (SERIAL on PG / INTEGER AUTOINCREMENT on SQLite); created_at uses a
# per-dialect epoch_now() default. All metadata columns are in the base
# CREATE on both backends (the ALTERs in _schema_*.py are upgrade-only).
TRANSCRIPT_ARCHIVE = define_table(
    'transcript_archive',
    autoincrement_pk(),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('messages_json', sa.Text, nullable=False),
    sa.Column('summary', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=epoch_now()),
    sa.Column('trigger', sa.Text, nullable=False, server_default='force'),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('round_num', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_before', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('tokens_after', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('msgs_before', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('msgs_after', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sqlite_autoincrement=True,
)


# ── Wave 2 (2026-06): the remaining hand-DDL tables, migrated onto Core. ──
# Same parity-gated workflow as the tables above — each has a byte-equivalence
# test in tests/test_core_schema_parity.py that is green on BOTH backends.

# message_queue — unified priority turn-source queue. Single TEXT PK; payload /
# config are plain TEXT (json strings, not JSONB) with '{}' defaults.
#   kind     — turn source: 'real' (human), 'workflow_step', or 'autopilot'
#              (a persistent armed-marker sentinel that is NOT dispatched as a
#              task; the autopilot hook consults it). See lib/message_queue.py.
#   priority — lower = higher priority. real=10, workflow_step=50, autopilot=90.
#              Rows dispatch in (priority ASC, position ASC) order so a human
#              message always pre-empts an autopilot sentinel.
MESSAGE_QUEUE = define_table(
    'message_queue',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('payload', sa.Text, nullable=False, server_default="{}"),
    sa.Column('config', sa.Text, nullable=False, server_default="{}"),
    sa.Column('position', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('kind', sa.Text, nullable=False, server_default="real"),
    sa.Column('priority', sa.Integer, nullable=False, server_default=sa.text('100')),
    sa.Column('created_at', bigint_column(), nullable=False),
)

# scheduled_tasks — cron/agent task registry. Single TEXT PK. Mixes NOT-NULL
# columns, nullable TEXT columns with no default (last_run/last_result),
# nullable-with-default columns (description and the proactive-agent fields),
# and BOOLEAN flags (enabled/notify_*). The post-create ALTERs in _schema_*.py
# stay (upgrade-only); Core's create only fires on a fresh install.
SCHEDULED_TASKS = define_table(
    'scheduled_tasks',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('name', sa.Text, nullable=False),
    sa.Column('schedule', sa.Text, nullable=False),
    sa.Column('task_type', sa.Text, nullable=False, server_default='command'),
    sa.Column('command', sa.Text, nullable=False),
    sa.Column('description', sa.Text, server_default=''),
    sa.Column('enabled', bool_column(), nullable=False, server_default=sa.true()),
    sa.Column('notify_on_failure', bool_column(), nullable=False, server_default=sa.true()),
    sa.Column('notify_on_success', bool_column(), nullable=False, server_default=sa.false()),
    sa.Column('max_runtime', sa.Integer, nullable=False, server_default=sa.text('300')),
    sa.Column('last_run', sa.Text),
    sa.Column('last_result', sa.Text),
    sa.Column('last_status', sa.Text, server_default='never'),
    sa.Column('run_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('fail_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('created_at', sa.Text, nullable=False, server_default=''),
    sa.Column('updated_at', sa.Text, nullable=False, server_default=''),
    sa.Column('target_conv_id', sa.Text, server_default=''),
    sa.Column('source_conv_id', sa.Text, server_default=''),
    sa.Column('tools_config', sa.Text, server_default="{}"),
    sa.Column('poll_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('last_poll_at', sa.Text, server_default=''),
    sa.Column('last_poll_decision', sa.Text, server_default=''),
    sa.Column('last_poll_reason', sa.Text, server_default=''),
    sa.Column('last_execution_at', sa.Text, server_default=''),
    sa.Column('last_execution_task_id', sa.Text, server_default=''),
    sa.Column('last_execution_status', sa.Text, server_default=''),
    sa.Column('execution_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('max_executions', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('expires_at', sa.Text, server_default=''),
)

# proactive_poll_log — append-only poll decisions. Auto-increment PK
# (SERIAL/INTEGER AUTOINCREMENT).
PROACTIVE_POLL_LOG = define_table(
    'proactive_poll_log',
    autoincrement_pk(),
    sa.Column('task_id', sa.Text, nullable=False),
    sa.Column('poll_time', sa.Text, nullable=False),
    sa.Column('decision', sa.Text, nullable=False, server_default='skip'),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sa.Column('status_snapshot', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('execution_task_id', sa.Text, server_default=''),
    sqlite_autoincrement=True,
)

# timer_watchers — durable timer/condition watchers. Single TEXT PK; all TEXT +
# integer poll fields, nullable-with-default trailing columns.
TIMER_WATCHERS = define_table(
    'timer_watchers',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('source_task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('check_instruction', sa.Text, nullable=False),
    sa.Column('check_command', sa.Text, nullable=False, server_default=''),
    sa.Column('continuation_message', sa.Text, nullable=False),
    sa.Column('poll_interval', sa.Integer, nullable=False, server_default=sa.text('60')),
    sa.Column('max_polls', sa.Integer, nullable=False, server_default=sa.text('120')),
    sa.Column('poll_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('status', sa.Text, nullable=False, server_default='active'),
    sa.Column('tools_config', sa.Text, nullable=False, server_default="{}"),
    sa.Column('created_at', sa.Text, nullable=False, server_default=''),
    sa.Column('updated_at', sa.Text, nullable=False, server_default=''),
    sa.Column('triggered_at', sa.Text, server_default=''),
    sa.Column('cancelled_at', sa.Text, server_default=''),
    sa.Column('execution_task_id', sa.Text, server_default=''),
    sa.Column('last_poll_at', sa.Text, server_default=''),
    sa.Column('last_poll_decision', sa.Text, server_default=''),
    sa.Column('last_poll_reason', sa.Text, server_default=''),
)

# timer_poll_log — append-only timer poll decisions. Auto-increment PK.
TIMER_POLL_LOG = define_table(
    'timer_poll_log',
    autoincrement_pk(),
    sa.Column('timer_id', sa.Text, nullable=False),
    sa.Column('poll_time', sa.Text, nullable=False),
    sa.Column('decision', sa.Text, nullable=False, server_default='wait'),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sa.Column('check_output', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('poll_id', sa.Text, nullable=False, server_default=''),
    sa.Column('raw_output', sa.Text, nullable=False, server_default=''),
    sqlite_autoincrement=True,
)

# swarm_sessions — durable swarm session state. Single TEXT PK; TEXT json
# columns + bigint timestamps defaulting to 0.
SWARM_SESSIONS = define_table(
    'swarm_sessions',
    sa.Column('swarm_key', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='running'),
    sa.Column('specs_json', sa.Text, nullable=False, server_default="[]"),
    sa.Column('config_json', sa.Text, nullable=False, server_default="{}"),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# swarm_agents — per-agent message checkpoints. Composite PK (swarm_key,
# agent_id); delivered is an INTEGER flag (0/1) on both backends in the live
# DDL — kept as plain Integer, NOT bool_column.
SWARM_AGENTS = define_table(
    'swarm_agents',
    sa.Column('swarm_key', sa.Text, nullable=False),
    sa.Column('agent_id', sa.Text, nullable=False),
    sa.Column('role', sa.Text, nullable=False, server_default=''),
    sa.Column('objective', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('messages_json', sa.Text, nullable=False, server_default="[]"),
    sa.Column('result_json', sa.Text, nullable=False, server_default="{}"),
    sa.Column('rounds_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('delivered', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('swarm_key', 'agent_id'),
)

# orchestration_runs — durable flow-run instances. Single TEXT PK.
ORCHESTRATION_RUNS = define_table(
    'orchestration_runs',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('orch_id', sa.Text, nullable=False, server_default=''),
    sa.Column('name', sa.Text, nullable=False, server_default=''),
    sa.Column('definition', sa.Text, nullable=False, server_default="{}"),
    sa.Column('input', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('final', sa.Text, nullable=False, server_default=''),
    sa.Column('error', sa.Text, nullable=False, server_default=''),
    sa.Column('created_by', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('finished_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# orchestration_run_events — append-only durable event log; composite PK
# (run_id, seq).
ORCHESTRATION_RUN_EVENTS = define_table(
    'orchestration_run_events',
    sa.Column('run_id', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('type', sa.Text, nullable=False, server_default=''),
    sa.Column('node_id', sa.Text, nullable=False, server_default=''),
    sa.Column('payload', sa.Text, nullable=False, server_default="{}"),
    sa.Column('ts', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('run_id', 'seq'),
)

# optimizer_proposals — nightly self-tuning proposals. Single TEXT PK;
# confidence is DOUBLE PRECISION/REAL.
OPTIMIZER_PROPOSALS = define_table(
    'optimizer_proposals',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('created_at', sa.Text, nullable=False),
    sa.Column('title', sa.Text, nullable=False),
    sa.Column('rationale', sa.Text, nullable=False),
    sa.Column('action_type', sa.Text, nullable=False),
    sa.Column('action_args', sa.Text, nullable=False),
    sa.Column('severity', sa.Text, nullable=False, server_default='low'),
    sa.Column('confidence', double_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('evidence', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending_review'),
    sa.Column('status_reason', sa.Text, nullable=False, server_default=''),
)

# optimizer_action_log — applied-action audit + revert tracking. Single TEXT PK.
OPTIMIZER_ACTION_LOG = define_table(
    'optimizer_action_log',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('proposal_id', sa.Text, nullable=False),
    sa.Column('applied_at', sa.Text, nullable=False),
    sa.Column('expires_at', sa.Text, nullable=False, server_default=''),
    sa.Column('pre_metric', sa.Text, nullable=False, server_default=''),
    sa.Column('outcome_metric', sa.Text, nullable=False, server_default=''),
    sa.Column('outcome_recorded_at', sa.Text, nullable=False, server_default=''),
    sa.Column('reverted_at', sa.Text, nullable=False, server_default=''),
    sa.Column('revert_reason', sa.Text, nullable=False, server_default=''),
)

# rate_limit_events — per-request gate log. BIGSERIAL/INTEGER AUTOINCREMENT PK
# (high-churn, id can exceed 32 bits). ts_ms is epoch-ms.
RATE_LIMIT_EVENTS = define_table(
    'rate_limit_events',
    bigint_autoincrement_pk(),
    sa.Column('endpoint', sa.Text, nullable=False),
    sa.Column('ip', sa.Text, nullable=False),
    sa.Column('ts_ms', bigint_column(), nullable=False),
    sqlite_autoincrement=True,
)

# error_resolutions — operator error-triage notes. Single TEXT PK. NOTE: this
# table is created ONLY on PostgreSQL in the live bootstrap (it has no SQLite
# CREATE), so only a PG parity test + PG-path wiring exist for it.
ERROR_RESOLUTIONS = define_table(
    'error_resolutions',
    sa.Column('fingerprint', sa.Text, primary_key=True),
    sa.Column('logger_name', sa.Text, nullable=False, server_default=''),
    sa.Column('sample_message', sa.Text, nullable=False, server_default=''),
    sa.Column('resolved_by', sa.Text, nullable=False, server_default=''),
    sa.Column('ticket', sa.Text, nullable=False, server_default=''),
    sa.Column('notes', sa.Text, nullable=False, server_default=''),
    sa.Column('resolved_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# tenant_users — multi-tenant relay user table (distinct from chat `users`).
# email is inline UNIQUE; email_verified is a plain INTEGER (0/1) on BOTH
# backends in the live DDL — NOT bool_column. metadata is TEXT json.
TENANT_USERS = define_table(
    'tenant_users',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('email', sa.Text, nullable=False, unique=True),
    sa.Column('password_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('display_name', sa.Text, nullable=False, server_default=''),
    sa.Column('role', sa.Text, nullable=False, server_default='user'),
    sa.Column('status', sa.Text, nullable=False, server_default='active'),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('last_login_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('email_verified', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('metadata', sa.Text, nullable=False, server_default="{}"),
)

# billing_ledger — append-only source-of-truth for credit movements. Single
# TEXT PK; all amounts are BIGINT micro-credits.
BILLING_LEDGER = define_table(
    'billing_ledger',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('user_id', sa.Text, nullable=False),
    sa.Column('ts', bigint_column(), nullable=False),
    sa.Column('amount_micro', bigint_column(), nullable=False),
    sa.Column('kind', sa.Text, nullable=False),
    sa.Column('ref_type', sa.Text, nullable=False, server_default=''),
    sa.Column('ref_id', sa.Text, nullable=False, server_default=''),
    sa.Column('balance_after_micro', bigint_column(), nullable=False),
    sa.Column('note', sa.Text, nullable=False, server_default=''),
)

# billing_wallets — denormalized balance cache. Single TEXT PK (user_id).
BILLING_WALLETS = define_table(
    'billing_wallets',
    sa.Column('user_id', sa.Text, primary_key=True),
    sa.Column('balance_micro', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('currency', sa.Text, nullable=False, server_default='CREDIT'),
    sa.Column('low_balance_alert_micro', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# billing_redeem_codes — prepaid redeem codes. Single TEXT PK (code).
BILLING_REDEEM_CODES = define_table(
    'billing_redeem_codes',
    sa.Column('code', sa.Text, primary_key=True),
    sa.Column('amount_micro', bigint_column(), nullable=False),
    sa.Column('batch', sa.Text, nullable=False, server_default=''),
    sa.Column('created_by', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('expires_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('redeemed_by', sa.Text, nullable=False, server_default=''),
    sa.Column('redeemed_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('note', sa.Text, nullable=False, server_default=''),
)

# billing_payments — external payment records. Single TEXT PK. amount_minor is
# minor-currency units; credit_micro is the granted micro-credits. raw is TEXT json.
BILLING_PAYMENTS = define_table(
    'billing_payments',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('user_id', sa.Text, nullable=False),
    sa.Column('provider', sa.Text, nullable=False),
    sa.Column('provider_id', sa.Text, nullable=False, server_default=''),
    sa.Column('amount_minor', bigint_column(), nullable=False),
    sa.Column('currency', sa.Text, nullable=False, server_default='USD'),
    sa.Column('credit_micro', bigint_column(), nullable=False),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('settled_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('raw', sa.Text, nullable=False, server_default="{}"),
)


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
           update_cols=None, insert_cols=None, commit=False, retry=False):
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
        commit: if True, commit after executing (kv/one-shot callers); leave
            False inside a larger transaction (hot-path batch writers commit
            once at the end).
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
