"""lib/database/_core_schema/_helpers.py — dialect-aware column & DDL helpers.

The foundational layer of the Core table-definition package: the private
``MetaData`` every groundwork table registers on, the cached compile-only
dialect singletons, the dialect-variant column factories
(``jsonb_column`` / ``timestamptz_column`` / ``autoincrement_pk`` / …), and the
two per-dialect server-default ``FunctionElement`` constructs
(``epoch_now`` / ``now_timestamp``) WITH their ``@compiles`` registrations.

CRITICAL: the ``@compiles(...)`` decorators below register GLOBAL SQLAlchemy
dialect compilers as an import side-effect. Keeping each ``FunctionElement``
class together with its compiler fns in this ONE submodule — imported by the
package ``__init__`` — is what guarantees the registration fires exactly once
on ``import lib.database._core_schema``. See the package ``__init__`` docstring.

See the package ``__init__.py`` for the full rationale on why SQLAlchemy Core.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.compiler import compiles
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


# Table names owned by OPTIONAL schema domains — created by that domain's
# registered initializer (lib/database/schema_registry.py), NOT by the
# always-on chat/system bootstrap. Excluded from the boot-time missing-table
# probe, otherwise a vanilla install (trading disabled) would force a full
# DDL pass on EVERY boot for a table it intentionally never creates.
OPTIONAL_DOMAIN_TABLES = frozenset({'trading_config'})

# Core tables the live bootstrap creates ONLY on PostgreSQL (no SQLite CREATE
# exists by design — see the ERROR_RESOLUTIONS note in _tables.py). Excluded
# from the SQLite boot probe for the same reason as OPTIONAL_DOMAIN_TABLES:
# probing them on SQLite would force a full DDL pass on EVERY boot of every
# SQLite install, permanently defeating the version fast-path.
PG_ONLY_CORE_TABLES = frozenset({'error_resolutions'})


def core_boot_table_names(backend: str = None) -> list:
    """Names of always-on Core tables the chat/system bootstrap must create.

    Derived from the shared MetaData minus tables the bootstrap intentionally
    never creates on the given backend — optional-domain tables (both
    backends) and PG-only tables (when ``backend == 'sqlite'``) — so a newly
    added Core table is covered automatically. ``backend`` defaults to the
    active ``lib.database._core._BACKEND``. The 2026-07-25 paper_podcasts
    incident (table registered without bumping ``_SCHEMA_VERSION``) slipped
    past the version fast-path on every existing deployment; the boot probe
    consuming this list is what makes that bug class self-healing.
    """
    import lib.database._core_schema as _cs  # noqa: F401 — registers all tables
    if backend is None:
        try:
            from lib.database import _core
            backend = getattr(_core, '_BACKEND', 'sqlite')
        except Exception as e:  # pragma: no cover - defensive
            logger.debug('[CoreSchema] backend probe failed, defaulting sqlite: %s', e)
            backend = 'sqlite'
    excluded = set(OPTIONAL_DOMAIN_TABLES)
    if backend != 'pg':
        excluded |= PG_ONLY_CORE_TABLES
    # Derive from the package-init snapshot (tables _core_schema itself
    # registered), NEVER the live shared MetaData: later define_table() calls
    # (compile-only test fixtures, domain plugins) must not leak into the
    # boot probe — otherwise each one becomes a phantom "missing" table that
    # forces a full DDL pass on EVERY boot.
    names = getattr(_cs, '_CORE_REGISTERED_TABLES', None)
    if names is None:  # pragma: no cover - defensive (partial import)
        names = frozenset(metadata.tables.keys())
    return sorted(set(names) - excluded)
