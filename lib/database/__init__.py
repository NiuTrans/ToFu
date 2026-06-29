"""lib/database — Dual-backend database layer (PostgreSQL primary, SQLite fallback).

All public symbols are re-exported from _core for backward compatibility.
Existing ``from lib.database import get_db, DOMAIN_CHAT`` still works.

Sub-modules:
  _core             — Config, backend detection, connection pool, Flask/thread-local helpers
  _sql_translate    — SQL compatibility translation for PG (regex, cache)
  _wrappers         — DictRow, PgCursor, PgConnection, sanitization (PG)
  _schema_pg        — PostgreSQL DDL (CREATE TABLE / migration / version cache)
  _schema_sqlite    — SQLite DDL (CREATE TABLE / migration / FTS5)
  _bootstrap        — PG server management (start/stop/discover/bootstrap)
"""

# ── Re-export everything from _core for backward compatibility ──
from lib.database._core import (  # noqa: F401
    # Backend info
    _BACKEND,
    # Config / constants
    BASE_DIR,
    DB_PATH,
    DOMAIN_CHAT,
    DOMAIN_SYSTEM,
    DOMAIN_TRADING,
    PG_DBNAME,
    PG_DSN,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    # Row wrapper / connection types
    DictRow,
    PgConnection,
    PgCursor,
    # Column check
    _column_exists,
    # Backward-compat helper
    _tune_connection,
    # Flask teardown
    close_db,
    # Write retry
    db_execute_with_retry,
    # Connection management
    get_db,
    get_thread_db,
    close_thread_db,
    # Schema init
    init_db,
    # Test-only: per-test SQLite isolation
    reset_sqlite_for_tests,
    restore_db_state,
    # JSON serialization
    json_dumps_pg,
    # Database availability
    db_available,
    pg_available,
    # Opt-in SQL-error log suppression (expected-to-fail probes)
    suppress_sql_error_log,
    # Sanitization
    strip_null_bytes_deep,
    # SQL translation
    translate_sql,
    # Warmup
    warmup_db,
    # TOAST corruption self-heal (PG-only, silent on SQLite)
    heal_toast_corruption,
    # Graceful shutdown
    shutdown_pool,
)
from lib.database._bootstrap import (  # noqa: F401
    # Scheduled logical backup (pg_dumpall) — PG-only, no-op on SQLite
    backup_pg_database,
)
from lib.database.aio import (  # noqa: F401
    # Async facade (Stage-2 native-async migration) — leak-safe await-able DB
    async_execute,
    async_executescript,
    async_fetchall,
    async_fetchone,
    async_transaction,
    run_pooled,
    shutdown_async_db,
)

__all__ = [
    '_BACKEND',
    'BASE_DIR', 'DB_PATH',
    'PG_HOST', 'PG_PORT', 'PG_DBNAME', 'PG_USER', 'PG_PASSWORD', 'PG_DSN',
    'DOMAIN_CHAT', 'DOMAIN_TRADING', 'DOMAIN_SYSTEM',
    'translate_sql',
    'DictRow', 'PgCursor', 'PgConnection',
    'strip_null_bytes_deep', 'json_dumps_pg',
    'get_db', 'get_thread_db', 'close_thread_db', 'close_db',
    'db_execute_with_retry',
    'warmup_db',
    'heal_toast_corruption',
    'init_db',
    'reset_sqlite_for_tests', 'restore_db_state',
    '_column_exists',
    'db_available', 'pg_available',
    'suppress_sql_error_log',
    '_tune_connection',
    'shutdown_pool',
    'backup_pg_database',
    # Async facade
    'async_execute', 'async_fetchone', 'async_fetchall',
    'async_executescript', 'async_transaction', 'run_pooled', 'shutdown_async_db',
]
