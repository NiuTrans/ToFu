"""Database schema initialization — SQLite backend (facade package).

CREATE TABLE, migrations, FTS5. Native SQLite DDL — no translation layer needed.

This package preserves the original ``lib.database._schema_sqlite`` module
facade: every public/private symbol that used to live in the single-file
module is re-exported here so ``from lib.database._schema_sqlite import X``
keeps working byte-identically. Import path UNCHANGED.

Layout:
  _meta.py     — _SCHEMA_VERSION + column/table probes + schema_meta cache
  _selfheal.py — _CRITICAL_COLUMNS + critical-column probe + FTS backfill
  _chat.py     — _init_chat_schema
  _system.py   — _init_system_schema
  __init__.py  — init_db (top-level entry point) + re-exports
"""

import time

from lib.log import get_logger

# ── Re-export the full module surface (facade preservation) ──────────────
from lib.database._schema_sqlite._meta import (  # noqa: F401
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
from lib.database._schema_sqlite._selfheal import (  # noqa: F401
    _CRITICAL_COLUMNS,
    _missing_core_tables,
    _missing_critical_columns,
    _backfill_search_fts,
)
from lib.database._schema_sqlite._chat import _init_chat_schema  # noqa: F401
from lib.database._schema_sqlite._system import _init_system_schema  # noqa: F401

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  init_db — top-level entry point
# ═══════════════════════════════════════════════════════════════════════

def init_db(_new_connection):
    """Initialize all database schemas.

    Uses a schema version cache to skip redundant DDL on subsequent startups.

    Args:
        _new_connection: callable that returns a SqliteConnection.
    """
    logger.info('[DB] Schema initialization started (SQLite)')

    conn = None
    try:
        conn = _new_connection()

        # ── Self-heal orphan tables (runs BEFORE the version fast-path so
        #    already-current deployments still converge). Drops tables left by
        #    removed subsystems / leaked tests that have no Core definition.
        from lib.database._orphan_heal import heal_orphan_tables
        heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)

        # ── Data backfill (flag-gated one-shot, runs BEFORE the version
        #    fast-path so a converged DB still heals): re-key the paper
        #    identity fork — reports saved under hash(strip(text)) vs library
        #    hash(raw) (epic pt_c9a103fe). No-op once flagged in schema_meta.
        #
        # ★ ISOLATED from DDL (pt_2a8aed4d). This import is the ONLY edge from
        #    schema init into the lib.paper → LLM/swarm chain: importing
        #    lib.paper.hash_backfill runs lib/paper/__init__.py (an eager
        #    barrel) which transitively loads the entire LLM-dispatch + swarm
        #    stack. On 2026-07-26 a single merge-conflict marker in
        #    lib/llm_sanitize/_gateway.py therefore made init_db raise a
        #    SyntaxError and abort ALL DDL — crashing boot and leaving a serving
        #    process answering requests against a schema-less DB for 22 min.
        #    A DATA-heal step must never be able to abort schema initialisation:
        #    it is idempotent and re-runs on the next boot, so on ANY failure
        #    (import error / a syntax error in the paper chain / a backfill
        #    runtime error) we log at ERROR and CONTINUE with DDL. Schema init
        #    is a correctness contract; data heal is not.
        try:
            from lib.paper.hash_backfill import backfill_paper_hash_canonical
            backfill_paper_hash_canonical(conn)
        except Exception as e:
            logger.error('[DB] paper hash backfill failed — schema init continues '
                         '(the idempotent heal re-runs on next boot): %s', e,
                         exc_info=True)

        # ── Fast path: version AND optional-domain set both unchanged.
        #    The domain set is part of the cache key so enabling a new domain
        #    (e.g. trading) re-triggers its DDL instead of being skipped.
        from lib.database.schema_registry import active_domains
        t0 = time.monotonic()
        current_version = _get_schema_version(conn)
        current_domains = _get_schema_domains(conn)
        want_domains = ','.join(active_domains())
        if current_version == _SCHEMA_VERSION and current_domains == want_domains:
            missing = _missing_critical_columns(conn)
            missing_tables = _missing_core_tables(conn)
            if missing or missing_tables:
                logger.warning('[DB] Schema version %d current but divergence found '
                               '(missing critical columns %s, missing core tables %s) '
                               '— forcing full DDL migration to converge',
                               _SCHEMA_VERSION, missing, missing_tables)
            else:
                elapsed = time.monotonic() - t0
                logger.info('[DB] Schema version %d + domains [%s] current — skipping '
                            'DDL (fast startup, checked in %.2fs)',
                            _SCHEMA_VERSION, want_domains, elapsed)
                return

        logger.info('[DB] Schema version %s → %d, domains [%s] → [%s] — running '
                    'full DDL migration', current_version, _SCHEMA_VERSION,
                    current_domains, want_domains)

        _init_chat_schema(conn)
        logger.info('[DB] Chat schema initialized')
        # system schema first — it creates the core-owned schema_meta table
        # the version/domain cache writes into below.
        _init_system_schema(conn)
        logger.info('[DB] System schema initialized')
        # Optional domains (e.g. trading) register their initializers via
        # lib/database/schema_registry.py — in-tree shim or tofu.schema plugin.
        from lib.database.schema_registry import run_registered
        run_registered(conn)

        _set_schema_version(conn, _SCHEMA_VERSION)
        _set_schema_domains(conn, active_domains())
        try:
            from lib.log import audit_log
            audit_log('db_schema_init', backend='sqlite', version=_SCHEMA_VERSION,
                      domains=want_domains, prev_version=current_version)
        except Exception as _ae:
            logger.debug('[DB] audit_log db_schema_init failed: %s', _ae)

        elapsed = time.monotonic() - t0
        logger.info('[DB] Schema initialization complete in %.1fs (version %d)',
                    elapsed, _SCHEMA_VERSION)
    except Exception as e:
        logger.error('[DB] Schema init failed: %s', e, exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as ce:
                logger.debug('[DB] Schema init conn close failed: %s', ce)
