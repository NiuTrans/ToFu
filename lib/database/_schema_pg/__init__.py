"""Database schema initialization — PostgreSQL backend (facade package).

CREATE TABLE, migrations, version cache, tsvector search.
Uses native PostgreSQL DDL (SERIAL, JSONB, tsvector, pg_trgm).

This is a pure re-export facade: the implementation is split across
sub-modules but the import path ``lib.database._schema_pg`` is UNCHANGED —
every ``from lib.database._schema_pg import X`` keeps working byte-identically.

  • ._meta      — _SCHEMA_VERSION const + existence probes + schema_meta cache
  • ._selfheal  — _CRITICAL_COLUMNS + critical-column probe + search backfills
  • ._chat      — _init_chat_schema (chat-domain bootstrap)
  • ._system    — _init_system_schema (system/billing/project bootstrap)
  • ._init      — init_db (top-level entry point)

_SCHEMA_VERSION and _CRITICAL_COLUMNS are single-homed (in ._meta / ._selfheal)
and re-exported here; consumers (lib/database/_core.py) reference them by name.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Meta helpers + schema-version constant ──────────────────────────────
from lib.database._schema_pg._meta import (  # noqa: E402,F401
    _SCHEMA_VERSION,
    _column_exists,
    _table_exists,
    _count_rows,
    _read_meta,
    _write_meta,
    _get_schema_version,
    _set_schema_version,
    _get_schema_domains,
    _set_schema_domains,
)

# ── Self-heal + backfill helpers + critical-column map ──────────────────
from lib.database._schema_pg._selfheal import (  # noqa: E402,F401
    _CRITICAL_COLUMNS,
    _missing_core_tables,
    _missing_critical_columns,
    _backfill_search_text,
    _backfill_search_tsv,
    _safe_create_table,
)

# ── Chat + system schema bootstrap ──────────────────────────────────────
from lib.database._schema_pg._chat import _init_chat_schema  # noqa: E402,F401
from lib.database._schema_pg._system import _init_system_schema  # noqa: E402,F401

# ── Top-level entry point ───────────────────────────────────────────────
from lib.database._schema_pg._init import init_db  # noqa: E402,F401


__all__ = [
    '_SCHEMA_VERSION',
    '_column_exists',
    '_table_exists',
    '_count_rows',
    '_read_meta',
    '_write_meta',
    '_get_schema_version',
    '_set_schema_version',
    '_get_schema_domains',
    '_set_schema_domains',
    '_CRITICAL_COLUMNS',
    '_missing_core_tables',
    '_missing_critical_columns',
    '_backfill_search_text',
    '_backfill_search_tsv',
    '_safe_create_table',
    '_init_chat_schema',
    '_init_system_schema',
    'init_db',
]
