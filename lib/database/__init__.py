"""lib/database — PostgreSQL database layer (package).

All public symbols are re-exported from sub-modules for backward compatibility.
Existing ``from lib.database import get_db, DOMAIN_CHAT`` still works.

Sub-modules:
  _core           — Config, connection pool, Flask/thread-local helpers (coordinator)
  _sql_translate   — SQL compatibility translation (regex, cache)
  _wrappers        — DictRow, PgCursor, PgConnection, sanitization
  _schema          — DDL (CREATE TABLE / migration / version cache)
  _bootstrap       — PG server management (start/stop/discover/bootstrap)
"""

# ── Re-export everything from _core for backward compatibility ──
from lib.database._core import (
    # Config / constants
    BASE_DIR,
    DOMAIN_CHAT,
    DOMAIN_SYSTEM,
    DOMAIN_TRADING,
    PG_DBNAME,
    PG_DSN,
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    # Sanitization / JSON
    DictRow,
    PgConnection,
    PgCursor,
    _column_exists,
    # Backward-compat helpers
    _tune_connection,
    close_db,
    db_execute_with_retry,
    # Connection management
    get_db,
    get_thread_db,
    # Schema
    init_db,
    json_dumps_pg,
    # Bootstrap status
    pg_available,
    strip_null_bytes_deep,
    # SQL translation
    translate_sql,
    warmup_db,
)

__all__ = [
    'BASE_DIR',
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
    'pg_available',
    '_tune_connection',
]
