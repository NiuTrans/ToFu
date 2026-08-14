"""Legacy in-process repositories during the storage.v1 domain migration.

All public symbols are re-exported from _core for backward compatibility.
Existing ``from lib.database import get_db, DOMAIN_CHAT`` still works.

The long-term authority is ``lib.storage_sidecar``. New application code must
use named ``StorageClient`` operations and must not add connections, cursors,
or transaction ownership here. SQLite and PostgreSQL are equal selected
backends; this compatibility surface is removed domain-by-domain before the
one-window production cutover.

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
    pooled_db,
    assert_write_transaction,
    write_transaction,
    pooled_write_transaction,
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
    # Shutdown-race awareness (concise finalize logging during PG stop)
    is_expected_shutdown_error,
    log_db_finalize_error,
    mark_pg_stopping,
    pg_is_stopping,
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
    # Storage pressure observability
    get_commit_count,
    reset_commit_count,
    get_sqlite_writer_lane_stats,
    # Load-shedding: typed pool-exhaustion error → mapped to HTTP 503
    PoolExhaustedError,
    # Fail-loud SQLite transaction-boundary violation (never retry blindly)
    SQLiteWriteDisciplineError,
)
def backup_pg_database(*args, **kwargs):
    """Compatibility entry point loaded only during an explicit PG backup."""
    from lib.database._pg_backup import backup_pg_database as _backup_pg
    return _backup_pg(*args, **kwargs)


from lib.database.backup import (  # noqa: F401
    # Backend-neutral scheduled backup + explicit SQLite snapshot primitive
    backup_database,
    backup_sqlite_database,
)
from lib.database.scoped_sequences import (  # noqa: F401
    allocate_scoped_sequence,
    lock_scoped_sequence,
)
from lib.database.paper_reports_repository import (  # noqa: F401
    mutate_paper_report_meta,
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
    'get_db', 'get_thread_db', 'close_thread_db', 'pooled_db',
    'assert_write_transaction',
    'write_transaction', 'pooled_write_transaction', 'close_db',
    'db_execute_with_retry',
    'warmup_db',
    'heal_toast_corruption',
    'init_db',
    'reset_sqlite_for_tests', 'restore_db_state',
    '_column_exists',
    'db_available', 'pg_available',
    'suppress_sql_error_log',
    'is_expected_shutdown_error', 'log_db_finalize_error',
    'mark_pg_stopping', 'pg_is_stopping',
    '_tune_connection',
    'shutdown_pool',
    'get_commit_count', 'reset_commit_count',
    'get_sqlite_writer_lane_stats',
    'PoolExhaustedError',
    'SQLiteWriteDisciplineError',
    'backup_pg_database',
    'backup_database', 'backup_sqlite_database',
    'allocate_scoped_sequence',
    'lock_scoped_sequence',
    'mutate_paper_report_meta',
    # Async facade
    'async_execute', 'async_fetchone', 'async_fetchall',
    'async_executescript', 'async_transaction', 'run_pooled', 'shutdown_async_db',
]
