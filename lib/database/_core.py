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

from lib.log import get_logger

logger = get_logger(__name__)


def _get_g():
    """Return the request-context global object, resolved LAZILY.

    Why not ``from flask import g`` at module top: ``server.py`` installs a
    Flask→Quart shim (``sys.modules['flask'] = quart``) at boot. A
    module-level ``from flask import g`` binds ``g`` at import time, so if
    this module is imported BEFORE the shim runs (common in unit tests that
    pull ``lib.database`` transitively), ``g`` is permanently bound to
    REAL Flask's proxy. The teardown handler below then touches that stale
    proxy under a Quart app context and raises
    ``RuntimeError: Working outside of application context`` — and, because
    the binding is process-global, it poisons every later server-backed
    test in the same pytest run. Importing ``g`` at call time always yields
    whatever ``flask`` currently resolves to (the shim's quart, in the app;
    real flask if genuinely unshimmed), so the proxy matches the live
    context. See .tofu/skills/test-server-shim-load-order.md.
    """
    from flask import g
    return g

# ── Per-namespace log leveling ──────────────────────────────────────────
# The database layer logs genuine failures (SQL errors, failed commits,
# self-heal deletes) so they reach app.log (INFO+) / error.log (WARNING+).
# Set ``TOFU_DB_LOG_LEVEL=DEBUG`` to additionally surface the verbose
# per-statement diagnostics (translated SQL, rollback noise) without
# flipping the whole app to DEBUG. Accepts a standard level name
# (DEBUG/INFO/WARNING/ERROR) or numeric value; invalid values are ignored.
from lib.env_compat import getenv_compat  # noqa: E402
_DB_LOG_LEVEL = getenv_compat('TOFU_DB_LOG_LEVEL', default='').strip().upper()
if _DB_LOG_LEVEL:
    import logging as _logging
    _resolved = getattr(_logging, _DB_LOG_LEVEL, None)
    if not isinstance(_resolved, int):
        try:
            _resolved = int(_DB_LOG_LEVEL)
        except (ValueError, TypeError) as e:
            logger.debug('[DB] TOFU_DB_LOG_LEVEL=%r not an int level: %s', _DB_LOG_LEVEL, e)
            _resolved = None
    if isinstance(_resolved, int):
        _logging.getLogger('lib.database').setLevel(_resolved)
        logger.info('[DB] lib.database log level set to %s via TOFU_DB_LOG_LEVEL', _DB_LOG_LEVEL)
    else:
        logger.warning('[DB] Ignoring invalid TOFU_DB_LOG_LEVEL=%r', _DB_LOG_LEVEL)

# Slow-query threshold (milliseconds). Any statement whose execute() exceeds
# this is logged at WARNING with the (truncated) SQL — invaluable for tracing
# contention/lock waits. Set to 0 to disable. Default 2000 ms.
try:
    _SLOW_QUERY_MS = int(getenv_compat('TOFU_DB_SLOW_QUERY_MS', default='2000'))
except (ValueError, TypeError) as e:
    logger.debug('[DB] Invalid TOFU_DB_SLOW_QUERY_MS, defaulting to 2000ms: %s', e)
    _SLOW_QUERY_MS = 2000

# ═══════════════════════════════════════════════════════════════════════
#  Backend Detection
# ═══════════════════════════════════════════════════════════════════════

# Which backend is active: 'pg' or 'sqlite'
_BACKEND = 'sqlite'  # default, upgraded to 'pg' below if possible


# ═══════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# SQLite path (used as fallback): ``data/tofu.db``.
from lib.env_compat import getenv_compat  # noqa: E402

_DB_DIR = os.path.join(BASE_DIR, 'data')
_DEFAULT_DB_FILE = os.path.join(_DB_DIR, 'tofu.db')
_explicit_db_path = getenv_compat('TOFU_DB_PATH', default='')
DB_PATH = _explicit_db_path or _DEFAULT_DB_FILE

# PostgreSQL config
PG_HOST = getenv_compat('TOFU_PG_HOST', default='127.0.0.1')
PG_PORT = int(getenv_compat('TOFU_PG_PORT', default='15432'))
# Default DB name is now ``tofu``. Live deployments still using the old
# ``chatui`` DB must run ``pg_dump chatui | psql tofu`` (or set
# ``TOFU_PG_DBNAME=chatui`` to keep the existing database in place
# until the dump/restore happens).
PG_DBNAME = getenv_compat('TOFU_PG_DBNAME', default='tofu')
PG_USER = getenv_compat('TOFU_PG_USER', default='')
PG_PASSWORD = getenv_compat('TOFU_PG_PASSWORD', default='')

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
# Auto-release a thread-local connection back to the pool once it has been
# idle this long with NO open transaction. This is the call-site-agnostic
# safety net for the recurring thread-local leak: instead of requiring every
# long-lived worker (message_queue poller, billing janitor, daily_report,
# paper/trading engines, …) to remember a close_thread_db() in a finally, the
# reaper reclaims any parked connection automatically. The owning thread's
# next get_thread_db() health-check reconnects transparently. Set to 0 to
# disable. Override via TOFU_DB_IDLE_RELEASE_S.
_IDLE_RELEASE_S = int(getenv_compat('TOFU_DB_IDLE_RELEASE_S', default='120'))

# Maximum total application-side connections (semaphore-guarded).
# Default 1000 to support 1000 concurrent DB users out of the box. The PG
# server's own ``max_connections`` is provisioned higher than this (see
# _MANAGED_PG_MAX_CONNECTIONS in lib/database/_bootstrap.py) so the app-side
# semaphore — not PG's hard limit — is always the binding constraint, and an
# overload surfaces as a clean queue/timeout instead of a PG "too many
# clients" FATAL. Tunable via env for smaller / larger deployments.
_MAX_TOTAL_CONNS = int(getenv_compat('TOFU_DB_MAX_CONNS', default='1000'))
_CONN_ACQUIRE_TIMEOUT_S = int(getenv_compat('TOFU_DB_ACQUIRE_TIMEOUT', default='30'))
_conn_semaphore = threading.BoundedSemaphore(_MAX_TOTAL_CONNS)
_conn_count = 0
_conn_count_lock = threading.Lock()

# ── PG self-heal / auto-rebootstrap state ──
# When the locally-owned PG crashes silently (symptoms below), try to
# re-run ``_ensure_pg_running`` ONCE and retry the connect. Multiple
# concurrent broken connections are coalesced behind this lock/cooldown
# so we don't stampede ``pg_ctl start``. Override via
# ``TOFU_PG_REBOOT_COOLDOWN_S`` env var.
#
# Recognised "PG is dead / needs a restart" error signatures:
#
#   1. "Connection refused"
#      Postmaster is completely dead (crash, OOM-kill, host reboot).
#      Historical case — fixed 2026-04-24.
#
#   2. "could not open shared memory segment"  /  "No space left on device"
#      accompanied by "/PostgreSQL.\d+" path.
#      Postmaster is ALIVE and TCP-accepts, but every new backend child
#      FATALs during startup because /dev/shm/PostgreSQL.* has been
#      wiped (common in containerised deployments where the container's
#      /dev/shm is cleaned or the container was paused/checkpointed).
#      Symptom: pg_isready says OK, but every real query raises FATAL.
#      Recovery: force-stop the zombie postmaster, then start fresh.
#
# _PG_DEAD_SIGNATURES lists substrings; any match triggers self-heal.
# _PG_ZOMBIE_SIGNATURES is the subset that needs a force-stop BEFORE
# re-ensuring (because the postmaster is still listening on TCP and
# would otherwise be silently reused by _ensure_pg_running).
_PG_DEAD_SIGNATURES = (
    'Connection refused',
    'could not open shared memory segment',  # /dev/shm wiped
    'server closed the connection unexpectedly',
)
_PG_ZOMBIE_SIGNATURES = (
    'could not open shared memory segment',
)

_PG_REBOOT_COOLDOWN_S = int(getenv_compat('TOFU_PG_REBOOT_COOLDOWN_S', default='60'))
# Exponential backoff: consecutive FAILED reboot attempts escalate the
# cooldown so a persistent issue (e.g. WAL corruption, another host
# stomping on our pgdata) doesn't spam pg_ctl start / postgresql.log
# forever. Resets to 1x on a successful reboot.
_PG_REBOOT_BACKOFF_MULTIPLIERS = (1, 5, 30, 30)  # 1x, 5x, 30x, 30x+ of base
_pg_reboot_lock = threading.Lock()
_last_pg_reboot_attempt_ts = 0.0  # monotonic seconds; 0 = never
_pg_consecutive_failed_reboots = 0


def _pg_error_is_dead(err_txt):
    """Return True if err_txt matches any known "PG is dead" signature."""
    if not err_txt:
        return False
    return any(sig in err_txt for sig in _PG_DEAD_SIGNATURES)


def _pg_error_is_zombie(err_txt):
    """Return True if err_txt indicates PG is TCP-alive but FATALing queries."""
    if not err_txt:
        return False
    return any(sig in err_txt for sig in _PG_ZOMBIE_SIGNATURES)


def _force_stop_zombie_pg():
    """Force-stop the local postmaster so a fresh one can take over.

    Used when the postmaster is TCP-alive but every backend FATALs on
    startup (e.g. ``/dev/shm/PostgreSQL.*`` wiped). In that state
    ``pg_isready`` still returns OK, so plain ``_ensure_pg_running``
    would silently reuse the zombie. We must kill it first.

    Tries in order:
      1. ``pg_ctl stop -m immediate`` (clean but forceful)
      2. Kill the PID from ``postmaster.pid`` directly
      3. Remove the stale pidfile so the next start isn't blocked
    """
    try:
        from lib.database._bootstrap import _find_pg_binary
    except ImportError as e:
        logger.debug('[DB] _find_pg_binary import failed: %s', e)
        return
    import subprocess
    import signal
    pgdata = _PGDATA
    # 1. pg_ctl stop -m immediate
    try:
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'immediate', '-w', '-t', '10'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            logger.info('[DB] Force-stopped zombie PG via pg_ctl -m immediate')
            return
        logger.warning('[DB] pg_ctl stop -m immediate returned rc=%d: %s',
                       result.returncode, (result.stderr or '').strip()[:300])
    except FileNotFoundError as e:
        logger.warning('[DB] pg_ctl not found for zombie-stop: %s', e)
    except Exception as e:
        logger.warning('[DB] pg_ctl stop -m immediate failed: %s', e)

    # 2. Fall back to signalling PID from postmaster.pid
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    pid = None
    try:
        with open(pidfile) as f:
            pid = int(f.readline().strip())
    except FileNotFoundError:
        logger.debug('[DB] No postmaster.pid — zombie PG likely already dead')
    except Exception as e:
        logger.warning('[DB] Could not read postmaster.pid for zombie-stop: %s', e)
    if pid:
        for sig_name, sig in (('SIGQUIT', signal.SIGQUIT), ('SIGKILL', signal.SIGKILL)):
            try:
                os.kill(pid, sig)
                logger.info('[DB] Sent %s to zombie PG PID=%d', sig_name, pid)
                time.sleep(1)
                try:
                    os.kill(pid, 0)  # still alive?
                except ProcessLookupError:
                    logger.info('[DB] Zombie PG PID=%d terminated (%s)', pid, sig_name)
                    break
            except ProcessLookupError:
                logger.debug('[DB] Zombie PG PID=%d already gone', pid)
                break
            except PermissionError as e:
                logger.warning('[DB] Cannot signal PID %d (permission): %s', pid, e)
                break
            except Exception as e:
                logger.warning('[DB] kill(%d, %s) failed: %s', pid, sig_name, e)

    # 3. Remove stale pidfile so _ensure_pg_running doesn't bail
    try:
        if os.path.exists(pidfile):
            os.remove(pidfile)
            logger.info('[DB] Removed stale postmaster.pid after zombie-stop')
    except FileNotFoundError as _e_audit:
        logger.debug('[_core] _force_stop_zombie_pg caught %s: %s', type(_e_audit).__name__, _e_audit)
        pass
    except Exception as e:
        logger.warning('[DB] Could not remove postmaster.pid after zombie-stop: %s', e)


def _maybe_reboot_pg(reason, force_stop_first=False):
    """Attempt to re-bootstrap the locally-owned PG, guarded by a cooldown.

    Only does anything when:
      • Active backend is PG, AND
      • This process OWNS the local PG (started it or attached at import).

    Args:
        reason: short text (used in logs/audit).
        force_stop_first: if True, stop the (possibly TCP-alive) zombie
            postmaster before calling ``_ensure_pg_running``. Required
            when the failure mode is "postmaster up but backends FATAL"
            (e.g. missing shared memory segments) because otherwise
            pg_isready would report OK and the bootstrap would no-op.

    Returns:
        True if a reboot attempt was made (whether it succeeded or not),
        False if skipped due to cooldown or because we don't own PG.

    Concurrent callers are serialised; only the first one within a
    ``_PG_REBOOT_COOLDOWN_S`` window performs the bootstrap call.
    """
    global _last_pg_reboot_attempt_ts, _pg_consecutive_failed_reboots
    if _BACKEND != 'pg':
        return False
    try:
        from lib.database._bootstrap import is_pg_owned_locally
    except ImportError as e:
        logger.debug('[DB] PG bootstrap module import failed during reboot: %s', e)
        return False
    if not is_pg_owned_locally():
        logger.debug('[DB] Broken PG but not locally-owned — skipping self-heal')
        return False

    now = time.monotonic()
    with _pg_reboot_lock:
        # Compute current cooldown based on consecutive failed attempts.
        # 0 failures → 1x base, 1 failure → 5x, 2 failures → 30x, etc.
        # This keeps log/postgresql.log spam bounded when a deeper
        # problem (e.g. WAL corruption, another host clobbering our
        # pgdata) prevents PG from coming up.
        idx = min(_pg_consecutive_failed_reboots,
                  len(_PG_REBOOT_BACKOFF_MULTIPLIERS) - 1)
        effective_cooldown = (_PG_REBOOT_COOLDOWN_S
                              * _PG_REBOOT_BACKOFF_MULTIPLIERS[idx])
        # Re-check under the lock (double-checked locking pattern)
        if (now - _last_pg_reboot_attempt_ts) < effective_cooldown:
            logger.debug('[DB] PG self-heal suppressed by cooldown '
                         '(%.1fs since last attempt, cooldown=%ds, '
                         'consecutive_failures=%d)',
                         now - _last_pg_reboot_attempt_ts,
                         effective_cooldown,
                         _pg_consecutive_failed_reboots)
            return False
        _last_pg_reboot_attempt_ts = now

        logger.error('[DB] PG appears dead (%s) — attempting re-bootstrap '
                     'once (cooldown=%ds, force_stop=%s, prior_failures=%d)',
                     reason, effective_cooldown, force_stop_first,
                     _pg_consecutive_failed_reboots)
        try:
            from lib.log import audit_log as _audit
            _audit('pg_auto_restart', reason=str(reason)[:300],
                   cooldown_s=effective_cooldown,
                   prior_failures=_pg_consecutive_failed_reboots,
                   force_stop=bool(force_stop_first))
        except Exception as _audit_err:
            logger.debug('[DB] audit_log for pg_auto_restart failed: %s',
                         _audit_err)

        # Zombie case: postmaster TCP-accepts but backends FATAL.
        # Force-stop it so the subsequent _ensure_pg_running starts fresh.
        if force_stop_first:
            try:
                _force_stop_zombie_pg()
            except Exception as e:
                logger.warning('[DB] force_stop_zombie_pg raised: %s', e, exc_info=True)

        try:
            from lib.database._bootstrap import _ensure_pg_running
            result = _ensure_pg_running(_PGDATA, BASE_DIR, PG_HOST, PG_PORT,
                                        PG_USER, PG_PASSWORD, PG_DBNAME)
            if result:
                logger.info('[DB] PG re-bootstrap succeeded: host=%s port=%s',
                            result.get('PG_HOST'), result.get('PG_PORT'))
                # Reset the failure counter so the next problem starts
                # at the normal cooldown.
                _pg_consecutive_failed_reboots = 0
                # Also drain the pool — existing pooled connections point
                # at the dead postmaster and would keep failing.
                try:
                    _drain_pg_pool()
                except Exception as e:
                    logger.debug('[DB] pool drain after reboot failed: %s', e)
            else:
                _pg_consecutive_failed_reboots += 1
                next_idx = min(_pg_consecutive_failed_reboots,
                               len(_PG_REBOOT_BACKOFF_MULTIPLIERS) - 1)
                logger.warning('[DB] PG re-bootstrap returned None — PG may '
                               'still be down (consecutive failures=%d, next '
                               'attempt allowed in %ds)',
                               _pg_consecutive_failed_reboots,
                               _PG_REBOOT_COOLDOWN_S
                               * _PG_REBOOT_BACKOFF_MULTIPLIERS[next_idx])
            return True
        except Exception as e:
            _pg_consecutive_failed_reboots += 1
            logger.error('[DB] PG re-bootstrap raised: %s '
                         '(consecutive failures=%d)', e,
                         _pg_consecutive_failed_reboots, exc_info=True)
            return True  # we did ATTEMPT — cooldown still applies


def _drain_pg_pool():
    """Close and discard all pooled PG connections.

    Called after a successful PG re-bootstrap: pooled connections point
    at the dead/replaced postmaster and would otherwise keep failing.
    No-op on SQLite backend.
    """
    if _BACKEND != 'pg':
        return
    drained = 0
    with _conn_pool_lock:
        while _conn_pool:
            c = _conn_pool.pop()
            try:
                c.close()
            except Exception as e:
                logger.debug('[DB] Error closing pooled conn during drain: %s', e)
            drained += 1
    if drained:
        logger.info('[DB] Drained %d stale pooled connections after PG reboot', drained)


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
        _t0 = time.monotonic()
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
            logger.error('[DB] SQL execution failed (%s): %.120s', type(e).__name__, e)
            logger.debug('[DB] SQL error detail: %s\n  SQL: %.200s\n  Params: %.200s',
                         e, sql, str(params)[:200] if params else 'None')
            raise
        if _SLOW_QUERY_MS:
            _elapsed_ms = (time.monotonic() - _t0) * 1000
            if _elapsed_ms >= _SLOW_QUERY_MS:
                logger.warning('[DB] Slow query %.0fms (threshold %dms): %.150s',
                               _elapsed_ms, _SLOW_QUERY_MS, sql)
        return self

    def executemany(self, sql, params_list):
        try:
            self._cursor.executemany(sql, params_list)
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            _sql_upper = sql[:30].lstrip().upper()
            if _sql_upper.startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP')):
                self._conn._dirty = True
        except Exception as e:
            logger.error('[DB] SQL executemany failed (%s): %.120s', type(e).__name__, e)
            logger.debug('[DB] executemany error detail: %s\n  SQL: %.200s', e, sql)
            raise
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
_BUSY_TIMEOUT_MS = int(getenv_compat('TOFU_SQLITE_BUSY_TIMEOUT_MS', default='30000'))

# SQLite connection pool (connections are cheap but file-handle churn adds up at 1000 users)
_sqlite_pool = []
_sqlite_pool_lock = threading.Lock()
_SQLITE_POOL_MAX = int(getenv_compat('TOFU_SQLITE_POOL_MAX', default='20'))


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
    # mmap is disabled: when the DB lives on a FUSE mount (beegfs-fuse, NFS,
    # etc.) and the backend hiccups, already-mapped pages can become invalid
    # and the next access raises SIGBUS, killing the whole process. Plain
    # pread() returns EIO and is recoverable. See logs/faulthandler.log
    # entries from 2026-05-28 for prior crashes traced to FUSE I/O.
    conn.execute('PRAGMA mmap_size=0')
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
            'or set TOFU_PG_HOST / TOFU_PG_PORT to an existing server.'
        )

    # Two-phase acquire: spend the bulk of the budget on a first attempt;
    # if that fails, proactively reap connections held by threads that have
    # already died (the reaper daemon only runs every 30s, so under a burst
    # we may be waiting on slots that are reclaimable RIGHT NOW), then retry
    # with the remaining budget. This converts a transient leak-induced
    # timeout into a recoverable stall instead of a hard 500.
    _first_wait = max(1.0, _CONN_ACQUIRE_TIMEOUT_S * 0.6)
    acquired = _conn_semaphore.acquire(timeout=_first_wait)
    if not acquired:
        reclaimed = 0
        try:
            reclaimed = _reap_dead_thread_connections()
        except Exception as _reap_err:
            logger.debug('[DB] Inline reap during acquire failed: %s', _reap_err)
        if reclaimed:
            logger.warning('[DB] Semaphore pressure: reclaimed %d dead-thread '
                           'connection(s) inline, retrying acquire', reclaimed)
        _retry_wait = max(1.0, _CONN_ACQUIRE_TIMEOUT_S - _first_wait)
        acquired = _conn_semaphore.acquire(timeout=_retry_wait)
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
                     'Tune via TOFU_DB_MAX_CONNS env var (current=%d).',
                     _CONN_ACQUIRE_TIMEOUT_S, current, _MAX_TOTAL_CONNS,
                     pooled, tracked, _MAX_TOTAL_CONNS)
        raise RuntimeError(
            f'Database connection pool exhausted ({current}/{_MAX_TOTAL_CONNS} '
            f'connections in use, {pooled} pooled, {tracked} thread-tracked). '
            f'Increase TOFU_DB_MAX_CONNS (current={_MAX_TOTAL_CONNS}) or '
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

    _connect_kwargs = dict(
        connect_timeout=_CONNECT_TIMEOUT_S,
        keepalives=1,
        keepalives_idle=_TCP_KEEPALIVES_IDLE_S,
        keepalives_interval=_TCP_KEEPALIVES_INTERVAL_S,
        keepalives_count=_TCP_KEEPALIVES_COUNT,
        application_name='tofu',
        gssencmode='disable',
    )
    try:
        try:
            conn = psycopg2.connect(PG_DSN, **_connect_kwargs)
        except psycopg2.OperationalError as e:
            err_txt = str(e)
            # Self-heal on recognised "PG is dead" signatures. Anything
            # else (auth failure, bad host, etc.) re-raises immediately.
            if not _pg_error_is_dead(err_txt):
                raise
            # Zombie postmaster (shm wiped) needs a force-stop first,
            # otherwise pg_isready in _ensure_pg_running will report OK
            # and the bootstrap will silently no-op.
            is_zombie = _pg_error_is_zombie(err_txt)
            attempted = _maybe_reboot_pg(err_txt[:200], force_stop_first=is_zombie)
            if not attempted:
                # Cooldown suppressed reboot OR we don't own PG — re-raise.
                raise
            # One-shot retry after re-bootstrap
            logger.info('[DB] Retrying psycopg2.connect after PG re-bootstrap')
            conn = psycopg2.connect(PG_DSN, **_connect_kwargs)
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
_CONN_POOL_MAX = int(getenv_compat('TOFU_DB_POOL_MAX', default='100'))


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
            except Exception as ce:
                logger.debug('[DB] Close after rollback failure: %s', ce)
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
    g = _get_g()
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
    """Register a thread-local connection for dead-thread reaping (PG only).

    Drops any prior registry entry for the SAME (thread, domain) first.
    A long-lived worker thread (e.g. an ``asyncio.to_thread`` pool thread)
    reconnects whenever its connection fails a health check; without this
    de-dup the registry grows one stale tuple per reconnect, inflating the
    ``tracked_threads`` metric far beyond the live connection count and
    pinning dead PgConnection objects via strong references.
    """
    if _BACKEND != 'pg':
        return
    import weakref
    thread = threading.current_thread()
    ref = weakref.ref(thread)
    with _thread_conn_lock:
        _thread_conn_registry[:] = [
            (r, c, d) for (r, c, d) in _thread_conn_registry
            if not (r() is thread and d == domain)
        ]
        _thread_conn_registry.append((ref, conn, domain))


def _reap_dead_thread_connections():
    """Close connections belonging to threads that have died (PG only).

    Returns the number of connections reclaimed (0 on the SQLite backend).
    """
    if _BACKEND != 'pg':
        return 0
    reaped = 0
    idle_released = 0
    now = time.monotonic()
    try:
        import psycopg2.extensions as _pgext
        _TX_IDLE = _pgext.TRANSACTION_STATUS_IDLE
    except Exception as e:
        logger.debug('[DB] psycopg2.extensions unavailable, assuming TX idle=0: %s', e)
        _TX_IDLE = 0
    with _thread_conn_lock:
        alive = []
        for ref, conn, domain in _thread_conn_registry:
            thread = ref()
            if thread is None or not thread.is_alive():
                # ALWAYS close the wrapper (not just when the underlying
                # psycopg2 conn is still open). PgConnection.close() is what
                # releases the semaphore slot, and it is idempotent + tolerates
                # an already-closed psycopg2 conn. If PG already killed the
                # connection (idle_in_transaction_session_timeout) the inner
                # conn is .closed, but the slot is still held by this wrapper —
                # skipping close() here is exactly what leaks the semaphore
                # until the pool drains to zero while PG itself is idle.
                try:
                    if not conn._closed:
                        try:
                            if not conn._conn.closed:
                                conn._conn.rollback()
                        except Exception as _rb_err:
                            logger.debug('[DB-Reaper] rollback during reap '
                                         'failed (domain=%s): %s', domain, _rb_err)
                        conn.close()
                        reaped += 1
                except Exception as e:
                    logger.debug('[DB-Reaper] Error closing dead-thread conn '
                                 '(domain=%s): %s', domain, e)
            else:
                # Thread is still alive, but its underlying psycopg2 conn may
                # already be DEAD — PG closes idle-in-transaction backends after
                # idle_in_transaction_session_timeout. The wrapper still holds a
                # semaphore slot that the live thread won't release until it next
                # calls get_thread_db() (which may be never for a sleeping daemon
                # poller). Closing an already-dead wrapper is safe (no in-flight
                # query) and idempotent — the owning thread's next get_thread_db()
                # health-check sees _closed and reconnects cleanly. Drop the stale
                # entry; reconnect re-registers via _register_thread_conn's dedup.
                try:
                    if not conn._closed and conn._conn.closed:
                        conn.close()
                        reaped += 1
                        continue
                except Exception as e:
                    logger.debug('[DB-Reaper] Error reclaiming dead conn on live '
                                 'thread (domain=%s): %s', domain, e)

                # ── Idle-release safety net (call-site-agnostic) ──────────
                # A live thread parking an IDLE connection (no open
                # transaction) past _IDLE_RELEASE_S is the recurring
                # thread-local leak. Reclaim it WITHOUT touching the owning
                # thread: close the wrapper (frees the semaphore slot) and
                # drop the registry entry. The thread's next get_thread_db()
                # health-check sees _closed and reconnects transparently.
                # GUARD: only when status is IDLE — never reclaim a conn with
                # an open/aborted transaction (that path is owned by PG's
                # idle_in_transaction_session_timeout) so we can't lose a
                # pending write or interrupt an in-flight query.
                if _IDLE_RELEASE_S > 0:
                    try:
                        if (not conn._closed
                                and not conn._conn.closed
                                and (now - conn._last_used) >= _IDLE_RELEASE_S
                                and conn._conn.get_transaction_status() == _TX_IDLE):
                            conn.close()
                            idle_released += 1
                            continue
                    except Exception as e:
                        logger.debug('[DB-Reaper] Idle-release probe failed on '
                                     'live thread (domain=%s): %s', domain, e)

                alive.append((ref, conn, domain))
        _thread_conn_registry[:] = alive
    if reaped:
        logger.info('[DB-Reaper] Closed %d connection(s) from dead threads '
                    '(remaining tracked: %d)', reaped, len(alive))
    if idle_released:
        logger.info('[DB-Reaper] Auto-released %d idle connection(s) from live '
                    'threads back to the pool (remaining tracked: %d)',
                    idle_released, len(alive))
    return reaped + idle_released


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

    # ── Leak guard ──────────────────────────────────────────────────
    # A connection LEAK shows up as tracked_threads ≫ active_conns: many
    # worker threads each pin a thread-local connection they never release
    # (the historical 539-tracked / 185-active signature). Genuine load,
    # by contrast, keeps the two roughly in step. Surface the divergence
    # loudly (WARNING + audit) so it's diagnosable from logs/error.log
    # before the semaphore is exhausted — bumping TOFU_DB_MAX_CONNS only
    # delays a real leak, it does not fix it.
    if _BACKEND == 'pg' and tracked_threads >= max(50, _MAX_TOTAL_CONNS // 2) \
            and tracked_threads > active * 3:
        logger.warning('[DB-Pool] Possible connection leak: tracked_threads=%d '
                       '≫ active_conns=%d (max=%d). Long-lived worker threads '
                       'may be holding thread-local connections without calling '
                       'close_thread_db() in a finally.',
                       tracked_threads, active, _MAX_TOTAL_CONNS)
        try:
            from lib.log import audit_log
            audit_log('db_conn_leak_suspected', tracked_threads=tracked_threads,
                      active_conns=active, max_conns=_MAX_TOTAL_CONNS,
                      pooled=pooled)
        except Exception as _ae:
            logger.debug('[DB-Pool] audit_log for leak guard failed: %s', _ae)


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


def _unregister_thread_conn(thread, domain=None):
    """Drop registry entries for ``thread`` (optionally a single domain)."""
    if _BACKEND != 'pg':
        return
    with _thread_conn_lock:
        _thread_conn_registry[:] = [
            (r, c, d) for (r, c, d) in _thread_conn_registry
            if not (r() is thread and (domain is None or d == domain))
        ]


def close_thread_db(domain=None):
    """Release this thread's thread-local DB connection(s) back to the pool.

    Long-lived worker threads (the ``asyncio.to_thread`` default pool, daemon
    task threads, etc.) otherwise pin one connection EACH for their whole
    lifetime via ``get_thread_db``. Under high concurrency that exhausts the
    connection semaphore even though the threads are idle between tasks. Call
    this in a ``finally`` at the end of any unit of work (e.g. ``run_task``)
    so the connection is returned to the shared pool for reuse instead of
    being held until the thread dies.

    Args:
        domain: Specific domain to release, or ``None`` for all domains.
    """
    domains = (domain,) if domain else (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM)
    thread = threading.current_thread()
    for d in domains:
        attr = f'db_{d}'
        db = getattr(_thread_local, attr, None)
        if db is None:
            continue
        setattr(_thread_local, attr, None)
        _unregister_thread_conn(thread, d)
        try:
            _pool_put(db)
        except Exception as e:
            logger.debug('[DB] close_thread_db: pool_put failed for domain=%s: %s', d, e)
            try:
                db.close()
            except Exception as _ce:
                logger.debug('[DB] close_thread_db: close fallback failed: %s', _ce)


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
                            old_raw = db._conn
                            fresh = _new_pg_connection()
                            # Adopt fresh's raw connection AND its semaphore slot.
                            # The slot + global _conn_count tied to the dead raw
                            # connection must be released here, otherwise every
                            # reconnect permanently leaks one pool slot and one
                            # PG backend (fresh kept its own slot/count).
                            try:
                                old_raw.close()
                            except Exception as _c_err:
                                logger.debug('[DB-Retry] Closing dead raw conn failed: %s', _c_err)
                            old_sem = getattr(db, '_semaphore', None)
                            if old_sem is not None:
                                try:
                                    old_sem.release()
                                except ValueError:
                                    logger.debug('[DB-Retry] Old semaphore already released')
                                import lib.database._core as _core_mod
                                with _conn_count_lock:
                                    _core_mod._conn_count = max(0, _core_mod._conn_count - 1)
                            db._conn = fresh._conn
                            db._semaphore = fresh._semaphore
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
    g = _get_g()
    for domain in (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM):
        key = f'_db_{domain}'
        db = g.pop(key, None)
        if db is not None:
            _was_dirty = getattr(db, '_dirty', False)
            try:
                if exception:
                    db.rollback()
                elif _was_dirty:
                    db.commit()
                else:
                    # Clean reads: rollback to release any implicit transaction
                    db.rollback()
            except Exception as _rb_err:
                if _was_dirty and not exception:
                    # A failed commit on a dirty connection = silent data loss.
                    logger.error('[DB] Teardown COMMIT FAILED for domain=%s — pending writes may be lost: %s',
                                 domain, _rb_err, exc_info=True)
                else:
                    logger.debug('[DB] Teardown rollback failed for domain=%s: %s', domain, _rb_err)
            _pool_put(db)


# ═══════════════════════════════════════════════════════════════════════
#  Warmup
# ═══════════════════════════════════════════════════════════════════════

def heal_toast_corruption():
    """Auto-heal TOAST-chunk corruption in the ``conversations`` table.

    Background
    ----------
    When a PostgreSQL cluster is copied between machines (e.g. a naïve
    ``rsync`` / ``tar`` of ``data/pgdata/`` via a FUSE mount) while the
    source PG is live, the destination can end up with rows whose
    out-of-line TOAST chunks were never flushed to the destination
    filesystem.  Any SELECT over the affected row — and, crucially, any
    INSERT whose index entry points at the missing chunk — fails with:

        ERROR: missing chunk number 0 for toast value N in pg_toast_XXXXX

    INSERTs time out after ``statement_timeout`` seconds (default 120 s),
    which breaks the user's "Send" button (every ``PUT /api/conversations``
    ends in 500).  ``export.py`` has been updated to use ``pg_dumpall``
    instead of raw-copying pgdata/, so this bug should never appear on
    fresh exports — but existing deployments that were seeded from a
    hot-copy still carry the damage.  This function detects and repairs
    it automatically on every startup so the user never notices.

    Strategy
    --------
    1.  Probe the table with a fast ``COUNT(*)``.  On success → no
        corruption, return quietly.
    2.  On toast failure → iterate all conversation IDs (the id column
        has no TOAST) and for each one probe a SELECT of the TOASTed
        columns under a short statement_timeout.  Any row that errors
        with "missing chunk" is unrecoverable.
    3.  DELETE unrecoverable rows by id (deletes only touch the heap &
        index, not the missing TOAST chunk → always succeed).
    4.  ``VACUUM (FULL) conversations`` + ``REINDEX TABLE conversations``
        to reclaim space and rebuild indexes free of dangling pointers.
    5.  ``audit_log`` every deleted id + the summary stats.

    PG-only; silent no-op on SQLite.
    """
    if _BACKEND != 'pg':
        return
    try:
        from lib.log import audit_log  # local import — avoid circulars
    except Exception as e:  # pragma: no cover
        logger.debug('[DB:heal] audit_log unavailable: %s', e)
        audit_log = None

    conn = None
    try:
        conn = _new_connection()
        cur = conn.cursor()
        # Step 1 — fast health probe. If this succeeds, we're done.
        try:
            cur.execute('SELECT COUNT(*) FROM conversations')
            cur.fetchone()
            logger.debug('[DB:heal] conversations table is healthy — no TOAST corruption')
            return
        except Exception as probe_exc:
            msg = str(probe_exc)
            if 'missing chunk' not in msg and 'toast' not in msg.lower():
                # Some other error — not our problem to fix here.
                logger.debug('[DB:heal] health probe raised non-TOAST error: %s', probe_exc)
                return
            logger.warning('[DB:heal] TOAST corruption detected in conversations: %s — '
                           'entering self-heal path', msg)
            # Abort the failed transaction so we can issue new queries.
            try:
                conn.rollback()
            except Exception as _e:
                logger.debug('[DB:heal] rollback after probe failed: %s', _e)

        # Step 2 — enumerate corrupt rows. Scan ids only (id column never TOASTs),
        # then probe the TOASTed columns one row at a time under a short
        # statement_timeout so a hang on a dead row can't stall startup.
        ids_to_check = []
        try:
            cur.execute('SELECT id FROM conversations ORDER BY id')
            ids_to_check = [row[0] for row in cur.fetchall()]
        except Exception as e:
            logger.error('[DB:heal] Could not enumerate conversation ids '
                         '(very unusual): %s', e, exc_info=True)
            try:
                conn.rollback()
            except Exception as _e:
                logger.debug('[DB:heal] rollback after id scan failed: %s', _e)
            return

        logger.info('[DB:heal] Scanning %d conversations for TOAST corruption…',
                    len(ids_to_check))
        corrupt_ids = []
        for cid in ids_to_check:
            try:
                cur.execute('SET LOCAL statement_timeout = 5000')  # 5 s
                cur.execute('SELECT length(messages::text), length(settings::text) '
                            'FROM conversations WHERE id = %s', (cid,))
                cur.fetchone()
                # No error → row is readable.
                conn.commit()
            except Exception as row_exc:
                row_msg = str(row_exc)
                try:
                    conn.rollback()
                except Exception as _e:
                    logger.debug('[DB:heal] rollback after row probe failed: %s', _e)
                if 'missing chunk' in row_msg or 'toast' in row_msg.lower():
                    corrupt_ids.append(cid)
                    logger.warning('[DB:heal] Corrupt conversation id=%s: %s', cid, row_msg)
                else:
                    # Unexpected error — log but don't delete the row.
                    logger.debug('[DB:heal] Non-TOAST error probing id=%s: %s', cid, row_exc)

        if not corrupt_ids:
            logger.info('[DB:heal] No individually-corrupt rows found — '
                        'global probe may have tripped on a transient issue, skipping heal')
            return

        # Step 3 — delete corrupt rows. DELETE touches the heap tuple + index
        # entries only, never the missing TOAST chunk, so it always succeeds.
        logger.warning('[DB:heal] Deleting %d unrecoverable conversations: %s',
                       len(corrupt_ids), corrupt_ids[:10])
        deleted = 0
        for cid in corrupt_ids:
            try:
                cur.execute('DELETE FROM conversations WHERE id = %s', (cid,))
                conn.commit()
                deleted += 1
                if audit_log is not None:
                    try:
                        audit_log('toast_corruption_heal_delete', conversation_id=cid)
                    except Exception as _e:
                        logger.debug('[DB:heal] audit_log failed: %s', _e)
            except Exception as del_exc:
                logger.error('[DB:heal] Could not delete corrupt id=%s: %s',
                             cid, del_exc, exc_info=True)
                try:
                    conn.rollback()
                except Exception as _e:
                    logger.debug('[DB:heal] rollback after delete failed: %s', _e)

        # Step 4 — reclaim space & rebuild indexes so future INSERTs don't
        # get stuck on dangling index pointers into the vanished TOAST chunks.
        # VACUUM FULL requires autocommit mode (no open transaction block),
        # so we flip the underlying psycopg2 connection's autocommit flag.
        raw = getattr(conn, 'raw', conn)  # unwrap PgConnection if present
        prev_autocommit = getattr(raw, 'autocommit', False)
        try:
            raw.autocommit = True
        except Exception as _e:
            logger.debug('[DB:heal] set autocommit=True failed: %s', _e)
        try:
            logger.info('[DB:heal] VACUUM FULL conversations …')
            cur.execute('VACUUM (FULL) conversations')
        except Exception as vac_exc:
            logger.warning('[DB:heal] VACUUM FULL failed: %s — '
                           'continuing with REINDEX', vac_exc)
        try:
            logger.info('[DB:heal] REINDEX TABLE conversations …')
            cur.execute('REINDEX TABLE conversations')
        except Exception as rx_exc:
            logger.warning('[DB:heal] REINDEX failed: %s', rx_exc)
        try:
            raw.autocommit = prev_autocommit
        except Exception as _e:
            logger.debug('[DB:heal] restore autocommit=%s failed: %s',
                         prev_autocommit, _e)

        logger.info('[DB:heal] Auto-heal complete: deleted %d corrupt rows, '
                    'vacuumed+reindexed conversations', deleted)
        if audit_log is not None:
            try:
                audit_log('toast_corruption_heal_complete',
                          deleted=deleted, total_scanned=len(ids_to_check))
            except Exception as _e:
                logger.debug('[DB:heal] audit_log summary failed: %s', _e)
    except Exception as e:
        logger.error('[DB:heal] Unexpected failure during TOAST heal: %s', e, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _e:
                logger.debug('[DB:heal] close failed: %s', _e)


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
            except Exception as ce:
                logger.debug('[DB] Warmup conn close failed: %s', ce)


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
                except Exception as e:
                    logger.debug('[DB] PG conn close during shutdown failed: %s', e)
        logger.info('[DB] PG connection pool drained (%d connections)', drained)
    else:
        with _sqlite_pool_lock:
            drained = 0
            while _sqlite_pool:
                conn = _sqlite_pool.pop()
                try:
                    conn.close()
                    drained += 1
                except Exception as e:
                    logger.debug('[DB] SQLite conn close during shutdown failed: %s', e)
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

    Controlled by env var ``TOFU_STOP_PG_ON_EXIT`` (legacy:
    ``TOFU_STOP_PG_ON_EXIT``; default ``1``):
      - ``1`` / unset: stop local PG when server.py exits
      - ``0``: leave PG running (faster dev-restart cycles, but requires
        manual ``pg_ctl stop`` before switching hosts on shared FUSE pgdata)

    Never stops a REMOTE PG — that belongs to another machine.
    """
    if _BACKEND != 'pg':
        return
    _stop_on_exit = getenv_compat('TOFU_STOP_PG_ON_EXIT',
                                  default='1').lower() \
        not in ('0', 'false', 'no', 'off')
    try:
        from lib.database._bootstrap import (
            _stop_pg as _boot_stop_pg,
            is_pg_owned_locally,
            stop_heartbeat,
        )
    except Exception as e:
        logger.warning('[DB] Failed to import shutdown helpers: %s', e)
        return

    if not _stop_on_exit:
        # PG stays up, but the heartbeat must stop so other hosts on the
        # same shared pgdata can safely take over if this process exits.
        logger.info('[DB] TOFU_STOP_PG_ON_EXIT=0 — leaving local PG running, '
                    'but clearing tofu heartbeat so peers can take over')
        try:
            stop_heartbeat(_PGDATA)
        except Exception as e:
            logger.warning('[DB] Failed to clear heartbeat on exit: %s', e)
        return

    try:
        if is_pg_owned_locally():
            logger.info('[DB] Stopping local PostgreSQL (we own it) — '
                        'set TOFU_STOP_PG_ON_EXIT=0 to keep it running '
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

def _register_optional_domains():
    """Wire optional DB-domain schema initializers before init_db() runs.

    Optional DB domains (e.g. the ``trading`` tables, now owned by the
    standalone ``tofu-trading`` package) register via the ``tofu.schema``
    entry-point group. Core defines no optional domains itself.

    Idempotent: safe to call on every ``init_db()``.
    """
    from lib.database.schema_registry import discover_schema_plugins
    discover_schema_plugins()


def init_db():
    """Initialize all database schemas using the active backend."""
    _register_optional_domains()
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
_FORCE_SQLITE = getenv_compat('TOFU_DB_BACKEND', default='').lower() == 'sqlite'

db_available = False
pg_available = False
_PGDATA = os.path.join(BASE_DIR, 'data', 'pgdata')

if _FORCE_SQLITE:
    _BACKEND = 'sqlite'
    db_available = True
    pg_available = False
    logger.info('[DB] SQLite backend (forced via TOFU_DB_BACKEND=sqlite): %s '
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
            logger.info('[DB] PG self-heal active: _new_pg_connection retries '
                        'once via _ensure_pg_running on "Connection refused" '
                        '(cooldown=%ds, env=TOFU_PG_REBOOT_COOLDOWN_S)',
                        _PG_REBOOT_COOLDOWN_S)

            # Start the reaper daemon thread (PG only)
            _reaper_thread = threading.Thread(target=_conn_reaper_loop, daemon=True,
                                              name='db-conn-reaper')
            _reaper_thread.start()

        except ImportError:
            logger.warning('[DB] psycopg2 not installed — falling back to SQLite')
            _pg_ok = False

    if not _pg_ok:
        # ── Fail-loud guard against silent data hiding ─────────────────
        # If a real pgdata/ cluster EXISTS on disk (PG was used before, so it
        # holds the user's conversations) but bootstrap just failed, falling
        # back to a fresh/near-empty SQLite makes it look like "all data was
        # lost" — the exact 2026-06-04 incident (corrupt WAL → PG won't boot →
        # 3 servers silently served a 2-conversation SQLite). The PG data is
        # actually intact; PG simply couldn't start. Surface this LOUDLY, and
        # in strict mode (TOFU_DB_STRICT_PG=1) refuse to start rather than
        # serve from the wrong, empty database.
        _pgdata_exists = False
        try:
            _pgdata_exists = (
                os.path.isfile(os.path.join(_PGDATA, 'PG_VERSION'))
                or os.path.isfile(os.path.join(_PGDATA, 'postgresql.conf'))
            )
        except Exception as _pe:
            logger.debug('[DB] pgdata existence probe failed: %s', _pe)
        if _pgdata_exists:
            logger.critical(
                '[DB] PostgreSQL cluster EXISTS at %s but bootstrap FAILED — '
                'NOT falling back transparently. Your conversations are most '
                'likely intact inside PG (it just failed to start, e.g. WAL '
                'corruption or a stale lock). Serving the SQLite fallback now '
                'would show a near-empty DB and look like data loss. '
                'Check logs/postgresql.log and recover PG before trusting the '
                'SQLite contents. Set TOFU_DB_STRICT_PG=1 to refuse startup '
                'in this situation instead of falling back.', _PGDATA)
            try:
                from lib.log import audit_log
                audit_log('pg_bootstrap_failed_with_existing_cluster',
                          pgdata=_PGDATA, fallback='sqlite')
            except Exception as _ae:
                logger.debug('[DB] audit_log for failed PG bootstrap failed: %s', _ae)
            if getenv_compat('TOFU_DB_STRICT_PG', default='').lower() in ('1', 'true', 'yes'):
                raise RuntimeError(
                    f'PostgreSQL cluster at {_PGDATA} exists but failed to start, '
                    'and TOFU_DB_STRICT_PG is set — refusing to fall back to an '
                    'empty SQLite and risk masking the real data. Recover PG '
                    '(see logs/postgresql.log) or unset TOFU_DB_STRICT_PG.')

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
    )
else:
    from lib.database._schema_sqlite import (  # noqa: E402, F401
        _column_exists as _schema_column_exists,
        _init_chat_schema,
        _init_system_schema,
    )
