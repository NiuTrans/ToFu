"""Schema initialization entry point — PostgreSQL backend.

``init_db`` wires up orphan-heal, the version/domain fast-path (with the
critical-column self-heal probe), the statement_timeout raise/restore, and the
chat + system + optional-domain schema bootstrap.
"""

import time

from lib.log import get_logger

from lib.database._schema_pg._meta import (
    _SCHEMA_VERSION, _count_rows, _table_exists,
    _get_schema_version, _set_schema_version,
    _get_schema_domains, _set_schema_domains,
)
from lib.database._schema_pg._selfheal import (
    _missing_core_tables,
    _missing_critical_columns,
)
from lib.database._schema_pg._chat import (
    _apply_chat_runtime_tuning,
    _init_chat_schema,
)
from lib.database._schema_pg._system import _init_system_schema

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  init_db — top-level entry point
# ═══════════════════════════════════════════════════════════════════════

def init_db(_new_pg_connection, _STATEMENT_TIMEOUT_MS):
    """Initialize all database schemas.

    Uses a schema version cache to skip redundant DDL on subsequent
    startups.

    Args:
        _new_pg_connection: callable that returns a PgConnection.
        _STATEMENT_TIMEOUT_MS: statement timeout for normal operations (restored after DDL).
    """
    logger.info('[DB] Schema initialization started (PostgreSQL)')

    conn = None
    try:
        conn = _new_pg_connection()

        # ── Self-heal orphan tables (runs BEFORE the version fast-path so
        #    already-current deployments still converge). Drops tables left by
        #    removed subsystems / leaked tests that have no Core definition.
        from lib.database._orphan_heal import heal_orphan_tables
        heal_orphan_tables(conn, table_exists=_table_exists, count_rows=_count_rows)

        # ── Data backfill (flag-gated one-shot, runs BEFORE the version
        #    fast-path so a converged DB still heals): re-key the paper
        #    identity fork — reports saved under hash(strip(text)) vs library
        #    hash(raw) (epic pt_c9a103fe). No-op once flagged in schema_meta.
        # Dependency-light import: schema maintenance must not initialize the
        # eager lib.paper → PDF/ONNX/LLM/swarm graph. Data repair is isolated
        # from core DDL just like the SQLite twin.
        try:
            from lib.paper_hash_backfill import backfill_paper_hash_canonical
            backfill_paper_hash_canonical(conn)
        except Exception as e:
            logger.error('[DB] paper hash backfill failed — schema init continues '
                         '(the idempotent heal retries next boot): %s', e,
                         exc_info=True)

        # ── Fast path: check if schema is already at current version AND
        #    the set of optional domains is unchanged. The domain set is part
        #    of the cache key because enabling a new domain (e.g. trading) on a
        #    server that booted without it must re-trigger that domain's DDL —
        #    otherwise the version fast-path would silently skip its tables.
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
                # Schema versioning deliberately skips full DDL, but mutable
                # table storage parameters (autovacuum/statistics) must still
                # converge on existing installations. This pass is a handful
                # of metadata-only ALTERs and handles its own failure without
                # making a performance knob an availability dependency.
                _apply_chat_runtime_tuning(conn)
                elapsed = time.monotonic() - t0
                logger.info('[DB] Schema version %d + domains [%s] current — skipping '
                            'DDL (fast startup, checked in %.2fs)',
                            _SCHEMA_VERSION, want_domains, elapsed)
                return

        logger.info('[DB] Schema version %s → %d, domains [%s] → [%s] — running '
                    'full DDL migration', current_version, _SCHEMA_VERSION,
                    current_domains, want_domains)

        # Raise statement_timeout for DDL
        try:
            cur = conn.cursor()
            cur.execute('SET SESSION statement_timeout = %s', ('600s',))
            conn.commit()
            cur.close()
            logger.debug('[DB] Raised statement_timeout to 600s for schema init')
        except Exception as e:
            logger.debug('[DB] Could not raise statement_timeout for init (non-fatal): %s', e)
            try:
                conn.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Rollback after statement_timeout raise failed: %s', _rb_err)

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
            audit_log('db_schema_init', backend='pg', version=_SCHEMA_VERSION,
                      domains=want_domains, prev_version=current_version)
        except Exception as _ae:
            logger.debug('[DB] audit_log db_schema_init failed: %s', _ae)

        # Restore normal statement_timeout
        try:
            cur = conn.cursor()
            cur.execute('SET SESSION statement_timeout = %s',
                        (f'{_STATEMENT_TIMEOUT_MS}ms',))
            conn.commit()
            cur.close()
        except Exception as _st_err:
            logger.debug('[DB] Could not restore statement_timeout after DDL (non-fatal): %s', _st_err)

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
            except Exception as _close_err:
                logger.debug('[DB] Error closing schema-init connection: %s', _close_err)
