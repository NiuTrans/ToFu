"""lib/database/_core.py — Dual-backend database layer (PostgreSQL primary, SQLite fallback).

Tries PostgreSQL first (full concurrency, JSONB, tsvector). If PG is unavailable
(no binary, no psycopg2, bootstrap failure), falls back to SQLite with WAL mode.

All sub-concerns for PG:
  _sql_translate.py  — SQL compatibility translation (regex, cache)
  _wrappers.py       — DictRow, PgCursor, PgConnection, sanitization
  _schema.py         — Schema DDL, migrations, version cache
  _bootstrap.py      — PG server management (start/stop/discover)

This file retains:
  - Config constants (PG_HOST, PG_PORT, PG_DSN, DB_PATH, domains)
  - Connection resilience parameters
  - Connection pool & request-scoped / thread-local helpers
  - init_db() entry point (delegates to _schema)
  - Backend auto-detection on import
"""

import atexit
import json
import os
import sqlite3
import threading
import time

from flask import g

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Backend Detection
# ═══════════════════════════════════════════════════════════════════════

# Which backend is active: 'pg' or 'sqlite'
_BACKEND = 'sqlite'  # default, upgraded to 'pg' below if possible


# ═══════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# SQLite path (used as fallback)
_DB_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.environ.get('CHATUI_DB_PATH', os.path.join(_DB_DIR, 'chatui.db'))

# PostgreSQL config
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

# Domain constants
DOMAIN_CHAT = 'chat'
DOMAIN_TRADING = 'trading'
DOMAIN_SYSTEM = 'system'


# ═══════════════════════════════════════════════════════════════════════
#  PostgreSQL Connection Resilience Parameters
# ═══════════════════════════════════════════════════════════════════════

_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_MS = 120_000
_IDLE_IN_TRANSACTION_S = 300
_TCP_KEEPALIVES_IDLE_S = 30
_TCP_KEEPALIVES_INTERVAL_S = 10
_TCP_KEEPALIVES_COUNT = 3
_IDLE_CHECK_S = 30
_MAX_CONN_AGE_S = 600

# Maximum total application-side connections (semaphore-guarded)
# Tunable via env vars for high-concurrency deployments (1000+ users)
_MAX_TOTAL_CONNS = int(os.environ.get('CHATUI_DB_MAX_CONNS', '200'))
_CONN_ACQUIRE_TIMEOUT_S = int(os.environ.get('CHATUI_DB_ACQUIRE_TIMEOUT', '30'))
_conn_semaphore = threading.BoundedSemaphore(_MAX_TOTAL_CONNS)
_conn_count = 0
_conn_count_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════
#  SQLite-only wrappers (used when _BACKEND == 'sqlite')
# ═══════════════════════════════════════════════════════════════════════

class _SqliteDictRow:
    """A row wrapper that supports both dict-like (row['col']) and index access (row[0])."""
    __slots__ = ('_data', '_keys', '_values')

    def __init__(self, cursor, values):
        self._keys = [desc[0] for desc in cursor.description]
        self._values = tuple(values)
        self._data = dict(zip(self._keys, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data.values())

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return f'DictRow({self._data})'

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._data.get(key, default)


class _SqliteCursorWrapper:
    """Wraps a sqlite3 cursor to return DictRow objects."""

    def __init__(self, real_cursor, conn):
        self._cursor = real_cursor
        self._conn = conn
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        try:
            if params:
                self._cursor.execute(sql, params)
            else:
                self._cursor.execute(sql)
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            _sql_upper = sql[:30].lstrip().upper()
            if _sql_upper.startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP')):
                self._conn._dirty = True
        except Exception as e:
            logger.debug('[DB] SQL error: %s\n  SQL: %.200s\n  Params: %.200s',
                         e, sql, str(params)[:200] if params else 'None')
            raise
        return self

    def executemany(self, sql, params_list):
        self._cursor.executemany(sql, params_list)
        self.description = self._cursor.description
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._cursor.description:
            return _SqliteDictRow(self._cursor, row)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows or not self._cursor.description:
            return rows
        return [_SqliteDictRow(self._cursor, r) for r in rows]

    def __iter__(self):
        while True:
            row = self._cursor.fetchone()
            if row is None:
                break
            if self._cursor.description:
                yield _SqliteDictRow(self._cursor, row)
            else:
                yield row

    def close(self):
        self._cursor.close()


class _SqliteConnectionWrapper:
    """SQLite connection wrapper providing the same API as PgConnection."""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._closed = False
        self._dirty = False
        self._created_at = time.monotonic()
        self._last_used = time.monotonic()
        self.row_factory = None

    @property
    def raw(self):
        """Access the underlying sqlite3 connection for special operations."""
        return self._conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        wrapper = _SqliteCursorWrapper(cur, self)
        return wrapper.execute(sql, params)

    def executemany(self, sql, params_list):
        cur = self._conn.cursor()
        wrapper = _SqliteCursorWrapper(cur, self)
        return wrapper.executemany(sql, params_list)

    def executescript(self, sql):
        """Execute multiple SQL statements separated by semicolons."""
        self._conn.executescript(sql)
        self._dirty = True

    def commit(self):
        self._conn.commit()
        self._dirty = False

    def rollback(self):
        self._conn.rollback()
        self._dirty = False

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except Exception as e:
                logger.debug('[DB] Error closing SQLite connection: %s', e)

    def cursor(self):
        cur = self._conn.cursor()
        return _SqliteCursorWrapper(cur, self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ═══════════════════════════════════════════════════════════════════════
#  Conditional imports — PG wrappers loaded only when PG is active
# ═══════════════════════════════════════════════════════════════════════

# These will be set during backend detection below
DictRow = _SqliteDictRow          # default
PgCursor = _SqliteCursorWrapper   # default
PgConnection = _SqliteConnectionWrapper  # default

# SQL translation — no-op for SQLite, real for PG
def _translate_sql_noop(sql):
    """No-op SQL translation for SQLite backend."""
    return sql, False

translate_sql = _translate_sql_noop


def _json_dumps_sqlite(obj, **kwargs):
    """JSON serializer for SQLite — no special handling needed."""
    kwargs.setdefault('ensure_ascii', False)
    return json.dumps(obj, **kwargs)


def _strip_null_bytes_noop(obj):
    """No-op for SQLite."""
    return obj


json_dumps_pg = _json_dumps_sqlite
strip_null_bytes_deep = _strip_null_bytes_noop


# ═══════════════════════════════════════════════════════════════════════
#  SQLite Connection Factory
# ═══════════════════════════════════════════════════════════════════════

# SQLite busy timeout — higher values reduce "database is locked" under concurrency
_BUSY_TIMEOUT_MS = int(os.environ.get('CHATUI_SQLITE_BUSY_TIMEOUT_MS', '30000'))

# SQLite connection pool (connections are cheap but file-handle churn adds up at 1000 users)
_sqlite_pool = []
_sqlite_pool_lock = threading.Lock()
_SQLITE_POOL_MAX = int(os.environ.get('CHATUI_SQLITE_POOL_MAX', '20'))


def _new_sqlite_connection():
    """Create a new SQLite connection with WAL mode and optimal settings."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(
        DB_PATH,
        timeout=_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
        isolation_level='DEFERRED',
    )
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA cache_size=-8000')
    conn.execute('PRAGMA mmap_size=268435456')
    # Reduce WAL checkpoint frequency — fewer I/O stalls under write-heavy load
    conn.execute('PRAGMA wal_autocheckpoint=1000')

    return _SqliteConnectionWrapper(conn)


# ═══════════════════════════════════════════════════════════════════════
#  PostgreSQL Connection Factory
# ═══════════════════════════════════════════════════════════════════════

def _new_pg_connection():
    """Create a new psycopg2 connection with full resilience parameters.

    Guarded by a bounded semaphore to prevent overwhelming PG with too
    many simultaneous connections (the root cause of 'too many clients').
    """
    global _conn_count
    if PG_PORT == 0:
        raise RuntimeError(
            'PostgreSQL is not available (bootstrap failed). '
            'Install PostgreSQL (conda install -c conda-forge postgresql>=18) '
            'or set CHATUI_PG_HOST / CHATUI_PG_PORT to an existing server.'
        )

    acquired = _conn_semaphore.acquire(timeout=_CONN_ACQUIRE_TIMEOUT_S)
    if not acquired:
        with _conn_count_lock:
            current = _conn_count
        with _conn_pool_lock:
            pooled = len(_conn_pool)
        with _thread_conn_lock:
            tracked = len(_thread_conn_registry)
        logger.error('[DB] Connection semaphore timeout after %ds '
                     '(active=%d, max=%d, pooled=%d, tracked_threads=%d) '
                     '— probable connection leak or insufficient pool size. '
                     'Tune via CHATUI_DB_MAX_CONNS env var (current=%d).',
                     _CONN_ACQUIRE_TIMEOUT_S, current, _MAX_TOTAL_CONNS,
                     pooled, tracked, _MAX_TOTAL_CONNS)
        raise RuntimeError(
            f'Database connection pool exhausted ({current}/{_MAX_TOTAL_CONNS} '
            f'connections in use, {pooled} pooled, {tracked} thread-tracked). '
            f'Increase CHATUI_DB_MAX_CONNS (current={_MAX_TOTAL_CONNS}) or '
            f'check for unclosed thread-local connections.'
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

    try:
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
    except Exception:
        _conn_semaphore.release()
        raise
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

    with _conn_count_lock:
        _conn_count += 1

    from lib.database._wrappers import PgConnection as _PgConn
    pg_conn = _PgConn(conn)
    pg_conn._semaphore = _conn_semaphore
    return pg_conn


def _test_pg_connection(pg_conn):
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
#  Generic Connection Factory (dispatches by backend)
# ═══════════════════════════════════════════════════════════════════════

def _new_connection():
    """Create a new connection using the active backend."""
    if _BACKEND == 'pg':
        return _new_pg_connection()
    return _new_sqlite_connection()


def _test_connection(conn):
    """Test if a connection is alive."""
    if _BACKEND == 'pg':
        return _test_pg_connection(conn)
    # SQLite: just check not closed
    return conn is not None and not conn._closed


# ═══════════════════════════════════════════════════════════════════════
#  Backward-compat helpers
# ═══════════════════════════════════════════════════════════════════════

def _tune_connection(db):
    """No-op. Kept for backward compatibility."""
    return db


def _column_exists(conn, table, column):
    """Check if a column exists in a table (backend-aware)."""
    if _BACKEND == 'pg':
        cur = conn._conn.cursor()
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table, column))
        result = cur.fetchone() is not None
        return result
    else:
        cur = conn._conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cur.fetchall()]
        return column in columns


# ═══════════════════════════════════════════════════════════════════════
#  Connection Pool (PG only — SQLite connections are cheap)
# ═══════════════════════════════════════════════════════════════════════

_conn_pool = []
_conn_pool_lock = threading.Lock()
_CONN_POOL_MAX = int(os.environ.get('CHATUI_DB_POOL_MAX', '50'))


def _pool_get():
    """Get a healthy connection from the pool, or create a new one.

    Works for both PG and SQLite backends. SQLite connections are pooled
    to avoid file-handle churn under high concurrency (1000+ users).
    """
    if _BACKEND == 'pg':
        with _conn_pool_lock:
            while _conn_pool:
                conn = _conn_pool.pop()
                if _test_connection(conn):
                    conn._dirty = False
                    return conn
                try:
                    conn.close()
                except Exception as e:
                    logger.debug('[DB] Error closing dead pooled PG connection: %s', e)
    else:
        # SQLite pool
        with _sqlite_pool_lock:
            while _sqlite_pool:
                conn = _sqlite_pool.pop()
                if _test_connection(conn):
                    conn._dirty = False
                    return conn
                try:
                    conn.close()
                except Exception as e:
                    logger.debug('[DB] Error closing dead pooled SQLite connection: %s', e)
    return _new_connection()


def _pool_put(conn):
    """Return a connection to the pool for reuse.

    Works for both PG and SQLite backends. Connections that fail
    health checks or rollback are closed and discarded.
    """
    if conn is None or conn._closed:
        return
    if _BACKEND == 'pg':
        if conn._conn.closed:
            return
        try:
            conn._conn.rollback()
            conn._dirty = False
        except Exception as e:
            logger.debug('[DB] Rollback failed on PG pool return: %s', e)
            try:
                conn.close()
            except Exception as e2:
                logger.debug('[DB] Error closing PG connection after rollback failure: %s', e2)
            return
        with _conn_pool_lock:
            if len(_conn_pool) < _CONN_POOL_MAX:
                _conn_pool.append(conn)
                return
        try:
            conn.close()
        except Exception as e:
            logger.debug('[DB] Error closing excess pooled PG connection: %s', e)
    else:
        # SQLite pool — rollback any uncommitted state, then return to pool
        try:
            conn._conn.rollback()
            conn._dirty = False
        except Exception as e:
            logger.debug('[DB] Rollback failed on SQLite pool return: %s', e)
            try:
                conn.close()
            except Exception:
                pass
            return
        with _sqlite_pool_lock:
            if len(_sqlite_pool) < _SQLITE_POOL_MAX:
                _sqlite_pool.append(conn)
                return
        try:
            conn.close()
        except Exception as e:
            logger.debug('[DB] Error closing excess pooled SQLite connection: %s', e)


# ═══════════════════════════════════════════════════════════════════════
#  Request-Scoped Connections (Flask g)
# ═══════════════════════════════════════════════════════════════════════

def get_db(domain=DOMAIN_CHAT):
    """Get a request-scoped database connection (PG pooled or SQLite)."""
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
        logger.debug('[DB] Request-scoped connection for domain=%s (backend=%s)', domain, _BACKEND)
    return db


# ═══════════════════════════════════════════════════════════════════════
#  Thread-Local Connections
# ═══════════════════════════════════════════════════════════════════════

_thread_local = threading.local()

# Registry of all thread-local connections for reaping dead threads (PG only)
_thread_conn_registry = []
_thread_conn_lock = threading.Lock()


def _register_thread_conn(conn, domain):
    """Register a thread-local connection for dead-thread reaping (PG only)."""
    if _BACKEND != 'pg':
        return
    import weakref
    thread = threading.current_thread()
    ref = weakref.ref(thread)
    with _thread_conn_lock:
        _thread_conn_registry.append((ref, conn, domain))


def _reap_dead_thread_connections():
    """Close connections belonging to threads that have died (PG only)."""
    if _BACKEND != 'pg':
        return
    reaped = 0
    with _thread_conn_lock:
        alive = []
        for ref, conn, domain in _thread_conn_registry:
            thread = ref()
            if thread is None or not thread.is_alive():
                try:
                    if not conn._closed and not conn._conn.closed:
                        conn._conn.rollback()
                        conn.close()
                        reaped += 1
                except Exception as e:
                    logger.debug('[DB-Reaper] Error closing dead-thread conn '
                                 '(domain=%s): %s', domain, e)
            else:
                alive.append((ref, conn, domain))
        _thread_conn_registry[:] = alive
    if reaped:
        logger.info('[DB-Reaper] Closed %d connection(s) from dead threads '
                    '(remaining tracked: %d)', reaped, len(alive))


_REAPER_INTERVAL_S = 30  # Check every 30s for dead threads (was 60s)
_POOL_METRICS_INTERVAL_S = 300  # Log pool metrics every 5 minutes


def _conn_reaper_loop():
    """Background thread that periodically reaps dead-thread connections (PG only).

    Also logs connection pool metrics periodically for capacity monitoring.
    """
    logger.info('[DB-Reaper] Started (reap_interval=%ds, metrics_interval=%ds)',
                _REAPER_INTERVAL_S, _POOL_METRICS_INTERVAL_S)
    _last_metrics = time.monotonic()
    while True:
        try:
            time.sleep(_REAPER_INTERVAL_S)
            _reap_dead_thread_connections()

            # Periodic pool metrics for capacity monitoring
            now = time.monotonic()
            if now - _last_metrics >= _POOL_METRICS_INTERVAL_S:
                _last_metrics = now
                _log_pool_metrics()
        except Exception as e:
            logger.error('[DB-Reaper] Cycle failed: %s', e, exc_info=True)


def _log_pool_metrics():
    """Log connection pool usage metrics for capacity monitoring."""
    with _conn_count_lock:
        active = _conn_count
    with _conn_pool_lock:
        pooled = len(_conn_pool)
    with _thread_conn_lock:
        tracked_threads = len(_thread_conn_registry)
    logger.info('[DB-Pool] backend=%s active_conns=%d/%d pooled=%d/%d '
                'tracked_threads=%d',
                _BACKEND, active, _MAX_TOTAL_CONNS, pooled, _CONN_POOL_MAX,
                tracked_threads)


def get_thread_db(domain=DOMAIN_CHAT):
    """Return a thread-local database connection."""
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

    db = _new_connection()
    setattr(_thread_local, attr, db)
    _register_thread_conn(db, domain)
    logger.debug('[DB] New thread-local connection for domain=%s thread=%s (backend=%s)',
                 domain, threading.current_thread().name, _BACKEND)
    return db


# ═══════════════════════════════════════════════════════════════════════
#  Write-Retry Helper
# ═══════════════════════════════════════════════════════════════════════

def db_execute_with_retry(db, sql, params=(), *, commit=True, max_retries=3):
    """Execute a single SQL write with retry on contention or connection loss."""
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            db.execute(sql, params)
            if commit:
                db.commit()
            return
        except Exception as e:
            err_msg = str(e).lower()
            # Determine if retryable
            is_retryable = False
            if _BACKEND == 'sqlite':
                is_retryable = ('database is locked' in err_msg or 'busy' in err_msg)
            else:
                # PG: OperationalError, InterfaceError, SerializationFailure
                etype = type(e).__name__
                is_retryable = etype in ('OperationalError', 'InterfaceError', 'SerializationFailure')
                if is_retryable:
                    try:
                        db.rollback()
                    except Exception as _rb_err:
                        logger.debug('[DB-Retry] Rollback failed: %s', _rb_err)
                    # Try to reconnect for PG connection errors
                    if etype in ('OperationalError', 'InterfaceError') and hasattr(db, '_conn'):
                        try:
                            fresh = _new_pg_connection()
                            db._conn = fresh._conn
                            db._created_at = fresh._created_at
                            db._last_used = time.monotonic()
                            logger.info('[DB-Retry] Reconnected underlying PG connection (was: %s)', etype)
                        except Exception as re_err:
                            logger.warning('[DB-Retry] Reconnect failed: %s', re_err)

            if is_retryable and attempt < max_retries:
                delay = 0.5 * (2 ** attempt)
                logger.warning('[DB-Retry] SQL attempt %d/%d %s, retrying in %.1fs: %s — %.80s',
                               attempt + 1, max_retries, type(e).__name__, delay, e, sql)
                time.sleep(delay)
                last_err = e
            else:
                if _BACKEND == 'pg' and not is_retryable:
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
    """Flask teardown handler — return connections to pool (both PG and SQLite)."""
    for domain in (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM):
        key = f'_db_{domain}'
        db = g.pop(key, None)
        if db is not None:
            try:
                if exception:
                    db.rollback()
                elif getattr(db, '_dirty', False):
                    db.commit()
                else:
                    # Clean reads: rollback to release any implicit transaction
                    db.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Teardown rollback/commit failed: %s', _rb_err)
            _pool_put(db)


# ═══════════════════════════════════════════════════════════════════════
#  Warmup
# ═══════════════════════════════════════════════════════════════════════

def warmup_db():
    """Verify database connectivity."""
    conn = None
    try:
        conn = _new_connection()
        row = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()
        count = row[0] if row else 0
        logger.info('[DB] Warmup done: %d conversations, %s backend OK', count,
                    'PostgreSQL' if _BACKEND == 'pg' else 'SQLite')
    except Exception as e:
        logger.warning('[DB] Warmup failed (non-fatal): %s', e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
#  Graceful Shutdown
# ═══════════════════════════════════════════════════════════════════════

def shutdown_pool():
    """Drain the connection pool. Called from atexit in ANY process that
    imports this module — including short-lived Python subprocesses spawned
    via run_command. Intentionally does NOT stop the PG server, because
    a subprocess inheriting connections must not kill the long-lived PG
    used by the parent server.py. PG lifecycle is owned by server.py itself
    via ``stop_local_pg_if_owned()`` below.
    """
    if _BACKEND == 'pg':
        with _conn_pool_lock:
            drained = 0
            while _conn_pool:
                conn = _conn_pool.pop()
                try:
                    conn.close()
                    drained += 1
                except Exception:
                    pass
        logger.info('[DB] PG connection pool drained (%d connections)', drained)
    else:
        with _sqlite_pool_lock:
            drained = 0
            while _sqlite_pool:
                conn = _sqlite_pool.pop()
                try:
                    conn.close()
                    drained += 1
                except Exception:
                    pass
        if drained:
            logger.info('[DB] SQLite connection pool drained (%d connections)', drained)
        else:
            logger.debug('[DB] Shutdown called (SQLite pool was empty)')


def stop_local_pg_if_owned():
    """Stop the locally-running PG server if this process owns it.

    Invoked from ``server.py``'s shutdown hook — NOT from an atexit hook in
    this module — so short-lived Python subprocesses that import
    ``lib.database`` (e.g. agent-invoked ``python3 -c ...`` commands) never
    accidentally stop the PG server used by the long-running Flask app.

    Controlled by env var ``CHATUI_STOP_PG_ON_EXIT`` (default ``1``):
      - ``1`` / unset: stop local PG when server.py exits
      - ``0``: leave PG running (faster dev-restart cycles, but requires
        manual ``pg_ctl stop`` before switching hosts on shared FUSE pgdata)

    Never stops a REMOTE PG — that belongs to another machine.
    """
    if _BACKEND != 'pg':
        return
    _stop_on_exit = os.environ.get('CHATUI_STOP_PG_ON_EXIT', '1').lower() \
        not in ('0', 'false', 'no', 'off')
    if not _stop_on_exit:
        logger.info('[DB] CHATUI_STOP_PG_ON_EXIT=0 — leaving local PG running')
        return
    try:
        from lib.database._bootstrap import (
            _stop_pg as _boot_stop_pg,
            is_pg_owned_locally,
        )
        if is_pg_owned_locally():
            logger.info('[DB] Stopping local PostgreSQL (we own it) — '
                        'set CHATUI_STOP_PG_ON_EXIT=0 to keep it running '
                        'across server.py restarts')
            _boot_stop_pg(_PGDATA)
        else:
            logger.debug('[DB] Not stopping PG on exit (remote or attached, not owned by us)')
    except Exception as e:
        logger.warning('[DB] Failed to stop local PG on exit: %s', e)


atexit.register(shutdown_pool)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Init (delegates to _schema module)
# ═══════════════════════════════════════════════════════════════════════

def init_db():
    """Initialize all database schemas using the active backend."""
    if _BACKEND == 'pg':
        from lib.database._schema_pg import init_db as _pg_schema_init
        _pg_schema_init(_new_pg_connection, _STATEMENT_TIMEOUT_MS)
    else:
        from lib.database._schema_sqlite import init_db as _sqlite_schema_init
        _sqlite_schema_init(_new_sqlite_connection)


# ═══════════════════════════════════════════════════════════════════════
#  Backend Detection & Auto-Start (runs on import)
# ═══════════════════════════════════════════════════════════════════════

# Force SQLite via env var (for testing or explicit preference)
_FORCE_SQLITE = os.environ.get('CHATUI_DB_BACKEND', '').lower() == 'sqlite'

db_available = False
pg_available = False
_PGDATA = os.path.join(BASE_DIR, 'data', 'pgdata')

if _FORCE_SQLITE:
    _BACKEND = 'sqlite'
    db_available = True
    pg_available = False
    logger.info('[DB] SQLite backend (forced via CHATUI_DB_BACKEND=sqlite): %s '
                '(busy_timeout=%dms, pool_max=%d)',
                DB_PATH, _BUSY_TIMEOUT_MS, _SQLITE_POOL_MAX)
else:
    # Try PostgreSQL
    _pg_ok = False
    try:
        from lib.database._bootstrap import _ensure_pg_running as _boot_ensure
        _pg_result = _boot_ensure(_PGDATA, BASE_DIR, PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME)
        if _pg_result:
            PG_HOST = _pg_result['PG_HOST']
            PG_PORT = _pg_result['PG_PORT']
            PG_DSN = _pg_result['PG_DSN']
            _pg_ok = True
    except ImportError as e:
        logger.info('[DB] PG bootstrap unavailable (missing dependency: %s) — will try SQLite', e)
    except Exception as e:
        logger.warning('[DB] PG bootstrap failed: %s — will try SQLite', e)

    if _pg_ok:
        # Verify psycopg2 is importable
        try:
            import psycopg2  # noqa: F401
            _BACKEND = 'pg'
            db_available = True
            pg_available = True

            # Load PG-specific wrappers and replace defaults
            from lib.database._wrappers import (  # noqa: E402
                DictRow as _PgDictRow,
                PgConnection as _PgConn,
                PgCursor as _PgCur,
                json_dumps_pg as _pg_json_dumps,
                strip_null_bytes_deep as _pg_strip_null,
            )
            from lib.database._sql_translate import translate_sql as _pg_translate  # noqa: E402

            DictRow = _PgDictRow
            PgCursor = _PgCur
            PgConnection = _PgConn
            translate_sql = _pg_translate
            json_dumps_pg = _pg_json_dumps
            strip_null_bytes_deep = _pg_strip_null

            logger.info('[DB] PostgreSQL backend: %s:%d/%s '
                        '(max_conns=%d, pool_max=%d, acquire_timeout=%ds)',
                        PG_HOST, PG_PORT, PG_DBNAME,
                        _MAX_TOTAL_CONNS, _CONN_POOL_MAX, _CONN_ACQUIRE_TIMEOUT_S)

            # Start the reaper daemon thread (PG only)
            _reaper_thread = threading.Thread(target=_conn_reaper_loop, daemon=True,
                                              name='db-conn-reaper')
            _reaper_thread.start()

        except ImportError:
            logger.warning('[DB] psycopg2 not installed — falling back to SQLite')
            _pg_ok = False

    if not _pg_ok:
        _BACKEND = 'sqlite'
        db_available = True
        pg_available = False
        # Reset PG config to prevent accidental use
        PG_HOST = '127.255.255.255'
        PG_PORT = 0
        PG_DSN = 'host=127.255.255.255 port=0 dbname=_none_'
        logger.info('[DB] SQLite fallback backend: %s '
                    '(busy_timeout=%dms, pool_max=%d)',
                    DB_PATH, _BUSY_TIMEOUT_MS, _SQLITE_POOL_MAX)


# ═══════════════════════════════════════════════════════════════════════
#  Re-export from _schema for backward compat
#  (these are always needed regardless of backend)
# ═══════════════════════════════════════════════════════════════════════

if _BACKEND == 'pg':
    from lib.database._schema_pg import (  # noqa: E402, F401
        _column_exists as _schema_column_exists,
        _init_chat_schema,
        _init_system_schema,
        _init_trading_schema,
    )
else:
    from lib.database._schema_sqlite import (  # noqa: E402, F401
        _column_exists as _schema_column_exists,
        _init_chat_schema,
        _init_system_schema,
        _init_trading_schema,
    )
