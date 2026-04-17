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
    # Schema init
    init_db,
    # JSON serialization
    json_dumps_pg,
    # Database availability
    db_available,
    pg_available,
    # Sanitization
    strip_null_bytes_deep,
    # SQL translation
    translate_sql,
    # Warmup
    warmup_db,
    # Graceful shutdown
    shutdown_pool,
)

__all__ = [
    '_BACKEND',
    'BASE_DIR', 'DB_PATH',
    'PG_HOST', 'PG_PORT', 'PG_DBNAME', 'PG_USER', 'PG_PASSWORD', 'PG_DSN',
    'DOMAIN_CHAT', 'DOMAIN_TRADING', 'DOMAIN_SYSTEM',
    'translate_sql',
    'DictRow', 'PgCursor', 'PgConnection',
    'strip_null_bytes_deep', 'json_dumps_pg',
    'get_db', 'get_thread_db', 'close_db',
    'db_execute_with_retry',
    'warmup_db',
    'init_db',
    '_column_exists',
    'db_available', 'pg_available',
    '_tune_connection',
    'shutdown_pool',
]
