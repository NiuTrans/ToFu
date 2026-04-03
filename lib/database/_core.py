"""lib/database/_core.py — PostgreSQL database layer (slim coordinator).

All sub-concerns have been extracted into sibling modules:
  _sql_translate.py  — SQL compatibility translation (regex, cache)
  _wrappers.py       — DictRow, PgCursor, PgConnection, sanitization
  _schema.py         — Schema DDL, migrations, version cache
  _bootstrap.py      — PG server management (start/stop/discover)

This file retains:
  - Config constants (PG_HOST, PG_PORT, PG_DSN, domains)
  - Connection resilience parameters
  - Connection pool & request-scoped / thread-local helpers
  - init_db() entry point (delegates to _schema)
  - Auto-start on import (delegates to _bootstrap)
"""

import os
import threading
import time

from flask import g

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Connection Config
# ═══════════════════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PG_HOST = os.environ.get('CHATUI_PG_HOST', '127.0.0.1')
PG_PORT = int(os.environ.get('CHATUI_PG_PORT', '15432'))
PG_DBNAME = os.environ.get('CHATUI_PG_DBNAME', 'chatui')
PG_USER = os.environ.get('CHATUI_PG_USER', '')
PG_PASSWORD = os.environ.get('CHATUI_PG_PASSWORD', '')

PG_DSN = f"host={PG_HOST} port={PG_PORT} dbname={PG_DBNAME}"
if PG_USER:
    PG_DSN += f" user={PG_USER}"
if PG_PASSWORD:
    PG_DSN += f" password={PG_PASSWORD}"

# ═══════════════════════════════════════════════════════════════════════
#  Domain Constants
# ═══════════════════════════════════════════════════════════════════════

DOMAIN_CHAT = 'chat'
DOMAIN_TRADING = 'trading'
DOMAIN_SYSTEM = 'system'

logger.info('[DB] PostgreSQL backend: %s:%d/%s', PG_HOST, PG_PORT, PG_DBNAME)


# ═══════════════════════════════════════════════════════════════════════
#  Connection Resilience Parameters
# ═══════════════════════════════════════════════════════════════════════

_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_MS = 120_000
_IDLE_IN_TRANSACTION_S = 300
_TCP_KEEPALIVES_IDLE_S = 30
_TCP_KEEPALIVES_INTERVAL_S = 10
_TCP_KEEPALIVES_COUNT = 3
_IDLE_CHECK_S = 30
_MAX_CONN_AGE_S = 600


# ═══════════════════════════════════════════════════════════════════════
#  Re-export from submodules (backward compat for all existing imports)
# ═══════════════════════════════════════════════════════════════════════

from lib.database._schema import (  # noqa: E402
    _column_exists,
    _init_chat_schema,
    _init_system_schema,
    _init_trading_schema,
)
from lib.database._sql_translate import translate_sql  # noqa: E402
from lib.database._wrappers import (  # noqa: E402
    DictRow,
    PgConnection,
    PgCursor,
    _sanitize_params,
    _sanitize_pg_param,
    _split_sql_statements,
    json_dumps_pg,
    strip_null_bytes_deep,
)

# ═══════════════════════════════════════════════════════════════════════
#  Connection Factory
# ═══════════════════════════════════════════════════════════════════════

def _new_pg_connection():
    """Create a new psycopg2 connection with full resilience parameters."""
    if PG_PORT == 0:
        raise RuntimeError(
            'PostgreSQL is not available (bootstrap failed). '
            'Install PostgreSQL (conda install -c conda-forge postgresql>=18) '
            'or set CHATUI_PG_HOST / CHATUI_PG_PORT to an existing server.'
        )

    import psycopg2
    import psycopg2.extensions

    def _jsonb_as_string(value, cur):
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = bytes(value)
        if isinstance(value, bytes):
            return value.decode('utf-8')
        return str(value)

    JSON_OID = 114
    JSONB_OID = 3802
    json_type = psycopg2.extensions.new_type((JSON_OID,), 'JSON_AS_STR', _jsonb_as_string)
    jsonb_type = psycopg2.extensions.new_type((JSONB_OID,), 'JSONB_AS_STR', _jsonb_as_string)

    conn = psycopg2.connect(
        PG_DSN,
        connect_timeout=_CONNECT_TIMEOUT_S,
        keepalives=1,
        keepalives_idle=_TCP_KEEPALIVES_IDLE_S,
        keepalives_interval=_TCP_KEEPALIVES_INTERVAL_S,
        keepalives_count=_TCP_KEEPALIVES_COUNT,
        application_name='chatui',
        gssencmode='disable',
    )
    psycopg2.extensions.register_type(json_type, conn)
    psycopg2.extensions.register_type(jsonb_type, conn)
    conn.autocommit = False

    try:
        cur = conn.cursor()
        cur.execute('SET SESSION statement_timeout = %s',
                    (f'{_STATEMENT_TIMEOUT_MS}ms',))
        cur.execute('SET SESSION idle_in_transaction_session_timeout = %s',
                    (f'{_IDLE_IN_TRANSACTION_S}s',))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.debug('[DB] Could not set session parameters (non-fatal): %s', e)
        try:
            conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after set-session-params also failed: %s', _rb_err)

    return PgConnection(conn)


def _test_connection(pg_conn):
    """Test if a PgConnection is alive, not expired, and healthy."""
    try:
        if pg_conn._closed:
            return False
        raw = pg_conn._conn
        if raw.closed:
            return False

        now = time.monotonic()

        age = now - pg_conn._created_at
        if age > _MAX_CONN_AGE_S:
            logger.debug('[DB] Connection expired (age=%.0fs > %ds), recycling', age, _MAX_CONN_AGE_S)
            return False

        idle = now - pg_conn._last_used
        if idle < _IDLE_CHECK_S:
            return True

        raw.rollback()
        cur = raw.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        pg_conn._last_used = now
        return True
    except Exception as e:
        logger.debug('[DB] Health check failed: %s', e)
        return False


# ═══════════════════════════════════════════════════════════════════════
#  Backward-compat helper
# ═══════════════════════════════════════════════════════════════════════

def _tune_connection(db):
    """No-op. Kept for backward compatibility with old code paths."""
    return db


# ═══════════════════════════════════════════════════════════════════════
#  Connection Pool
# ═══════════════════════════════════════════════════════════════════════

_conn_pool = []
_conn_pool_lock = threading.Lock()
_CONN_POOL_MAX = 8


def _pool_get():
    """Get a healthy connection from the pool, or create a new one."""
    with _conn_pool_lock:
        while _conn_pool:
            conn = _conn_pool.pop()
            if _test_connection(conn):
                conn._dirty = False
                return conn
            try:
                conn.close()
            except Exception:
                pass
    return _new_pg_connection()


def _pool_put(conn):
    """Return a connection to the pool (after rollback), or close if pool is full."""
    if conn._closed or conn._conn.closed:
        return
    try:
        conn._conn.rollback()
        conn._dirty = False
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return
    with _conn_pool_lock:
        if len(_conn_pool) < _CONN_POOL_MAX:
            _conn_pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  Request-Scoped Connections (Flask g)
# ═══════════════════════════════════════════════════════════════════════

def get_db(domain=DOMAIN_CHAT):
    """Get a request-scoped PostgreSQL connection from the pool."""
    key = f'_db_{domain}'
    db = getattr(g, key, None)
    if db is not None:
        if not _test_connection(db):
            logger.warning('[DB] Request-scoped connection dead for domain=%s, reconnecting', domain)
            try:
                db.close()
            except Exception as _close_err:
                logger.debug('[DB] Error closing dead request-scoped connection: %s', _close_err)
            db = None
            setattr(g, key, None)
    if db is None:
        db = _pool_get()
        setattr(g, key, db)
        logger.debug('[DB] Request-scoped PG connection for domain=%s (from pool)', domain)
    return db


# ═══════════════════════════════════════════════════════════════════════
#  Thread-Local Connections
# ═══════════════════════════════════════════════════════════════════════

_thread_local = threading.local()


def get_thread_db(domain=DOMAIN_CHAT):
    """Return a thread-local PostgreSQL connection."""
    attr = f'db_{domain}'
    db = getattr(_thread_local, attr, None)
    if db is not None:
        if _test_connection(db):
            return db
        else:
            logger.debug('[DB] Health-check failed for %s, reconnecting', domain)
            try:
                db.close()
            except Exception as _close_err:
                logger.debug('[DB] Error closing dead thread-local connection: %s', _close_err)
            setattr(_thread_local, attr, None)

    db = _new_pg_connection()
    setattr(_thread_local, attr, db)
    logger.debug('[DB] New thread-local PG connection for domain=%s thread=%s',
                 domain, threading.current_thread().name)
    return db


# ═══════════════════════════════════════════════════════════════════════
#  Write-Retry Helper
# ═══════════════════════════════════════════════════════════════════════

def db_execute_with_retry(db, sql, params=(), *, commit=True, max_retries=3):
    """Execute a single SQL write with retry on contention or connection loss."""
    import psycopg2
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            db.execute(sql, params)
            if commit:
                db.commit()
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError,
                psycopg2.errors.SerializationFailure) as e:
            try:
                db.rollback()
            except Exception as _rb_err:
                logger.debug('[DB-Retry] Rollback failed: %s', _rb_err)
            is_conn_error = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))
            if is_conn_error and hasattr(db, '_conn'):
                try:
                    fresh = _new_pg_connection()
                    db._conn = fresh._conn
                    db._created_at = fresh._created_at
                    db._last_used = time.monotonic()
                    logger.info('[DB-Retry] Reconnected underlying PG connection (was: %s)', type(e).__name__)
                except Exception as re_err:
                    logger.warning('[DB-Retry] Reconnect failed: %s', re_err)
            if attempt < max_retries:
                delay = 0.5 * (2 ** attempt)
                logger.warning('[DB-Retry] SQL attempt %d/%d %s, retrying in %.1fs: %s — %.80s',
                               attempt + 1, max_retries, type(e).__name__, delay, e, sql)
                time.sleep(delay)
                last_err = e
            else:
                raise
        except Exception:
            try:
                db.rollback()
            except Exception as _rb_err:
                logger.debug('[DB-Retry] Rollback after non-retryable error failed: %s', _rb_err)
            raise
    raise last_err


# ═══════════════════════════════════════════════════════════════════════
#  Flask Teardown
# ═══════════════════════════════════════════════════════════════════════

def close_db(exception):
    """Flask teardown handler — return connections to pool."""
    for domain in (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM):
        key = f'_db_{domain}'
        db = g.pop(key, None)
        if db is not None:
            try:
                if exception:
                    db.rollback()
                elif getattr(db, '_dirty', False):
                    db.commit()
            except Exception as _rb_err:
                logger.debug('[DB] Teardown rollback/commit failed: %s', _rb_err)
            _pool_put(db)


# ═══════════════════════════════════════════════════════════════════════
#  Warmup
# ═══════════════════════════════════════════════════════════════════════

def warmup_db():
    """Verify PostgreSQL connectivity and pre-heat connection."""
    conn = None
    try:
        conn = _new_pg_connection()
        row = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()
        count = row[0] if row else 0
        logger.info('[DB] Warmup done: %d conversations, PostgreSQL connection verified', count)
    except Exception as e:
        logger.warning('[DB] Warmup failed (non-fatal): %s', e, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _close_err:
                logger.debug('[DB] Warmup PG connection close failed: %s', _close_err)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Init (delegates to _schema module)
# ═══════════════════════════════════════════════════════════════════════

def init_db():
    """Initialize all database schemas."""
    from lib.database._schema import init_db as _schema_init_db
    _schema_init_db(_new_pg_connection, _STATEMENT_TIMEOUT_MS)


# ═══════════════════════════════════════════════════════════════════════
#  Auto-start / discover PostgreSQL on import
# ═══════════════════════════════════════════════════════════════════════

_PGDATA = os.path.join(BASE_DIR, 'data', 'pgdata')

from lib.database._bootstrap import _ensure_pg_running as _boot_ensure  # noqa: E402
from lib.database._bootstrap import _stop_pg  # noqa: E402

_pg_result = _boot_ensure(_PGDATA, BASE_DIR, PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME)
if _pg_result:
    PG_HOST = _pg_result['PG_HOST']
    PG_PORT = _pg_result['PG_PORT']
    PG_DSN = _pg_result['PG_DSN']
    pg_available = True
else:
    pg_available = False
    logger.critical('[DB] ═══ PostgreSQL is NOT available ═══\n'
                    '  Could not start or discover a PostgreSQL instance for this project.\n'
                    '  The app will start but ALL database operations will fail.\n'
                    '  Fix: install PostgreSQL (conda install -c conda-forge postgresql>=18)\n'
                    '  or set CHATUI_PG_HOST / CHATUI_PG_PORT to an existing server.')
    PG_HOST = '127.255.255.255'
    PG_PORT = 0
    PG_DSN = 'host=127.255.255.255 port=0 dbname=_none_'
