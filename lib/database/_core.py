"""Legacy in-process database facade during migration to ``storage.v1``.

SQLite defaults and PostgreSQL is explicitly selected; either selection fails
closed. This module is not a permanent connection owner: repositories migrate
to the process-isolated Storage Sidecar before production cutover.

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
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import weakref

from lib.log import get_logger

logger = get_logger(__name__)


def _get_g():
    """Return the request-context global object, resolved LAZILY.

    Why not ``from quart import g`` at module top: request context proxies are
    bound to the current async context. A
    module-level ``from quart import g`` binds ``g`` at import time, so if
    this module is imported BEFORE the shim runs (common in unit tests that
    pull ``lib.database`` transitively), ``g`` is permanently bound to
    an inactive Quart proxy. The teardown handler below then touches it
    proxy under a Quart app context and raises
    ``RuntimeError: Working outside of application context`` — and, because
    the binding is process-global, it poisons every later server-backed
    test in the same pytest run. Importing ``g`` at call time always yields
    the live Quart proxy, so it matches the active context.
    """
    from quart import g
    return g


def _has_app_context():
    """Whether this execution context owns a Flask/Quart ``g`` object.

    Resolve lazily for the same shim-order reason as :func:`_get_g`. Plain
    worker threads and early bootstrap paths simply retain the historical
    thread-local connection behaviour.
    """
    try:
        from quart import has_app_context
        return bool(has_app_context())
    except (ImportError, RuntimeError) as exc:
        logger.debug('[DB] app-context probe unavailable: %s', exc)
        return False

# ── Per-namespace log leveling ──────────────────────────────────────────
# The database layer logs genuine failures (SQL errors, failed commits,
# self-heal deletes) so they reach app.log (INFO+) / error.log (WARNING+).
# Set ``TOFU_DB_LOG_LEVEL=DEBUG`` to additionally surface the verbose
# per-statement diagnostics (translated SQL, rollback noise) without
# flipping the whole app to DEBUG. Accepts a standard level name
# (DEBUG/INFO/WARNING/ERROR) or numeric value; invalid values are ignored.
from lib.env_compat import getenv_compat  # noqa: E402


def _bounded_env_int(name, default, minimum, maximum):
    """Read an integer knob without allowing a typo to brick DB import."""
    raw = getenv_compat(name, default=str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[DB] Invalid %s=%r; using %d', name, raw, default)
        return default
    if value < minimum or value > maximum:
        clamped = min(max(value, minimum), maximum)
        logger.warning('[DB] %s=%s outside [%d, %d]; clamped to %d',
                       name, value, minimum, maximum, clamped)
        return clamped
    return value


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
_SLOW_QUERY_MS = _bounded_env_int(
    'TOFU_DB_SLOW_QUERY_MS', 2000, 0, 3_600_000)
# ── Opt-in SQL-error log suppression ─────────────────────────────────────
# Some callers run a query that is EXPECTED to fail as part of normal control
# flow — e.g. an existence probe for a table that may not be created yet
# (lib/billing/janitor.py's startup gate polls ``SELECT 1 FROM billing_ledger``
# until the schema is bootstrapped). Such a probe still raises (the caller
# handles it), but the cursor wrappers would otherwise log every failure at
# ERROR, spamming error.log with self-recovering "no such table" noise. A
# caller wraps the probe in ``with suppress_sql_error_log():`` to demote that
# one query's failure log to DEBUG. Thread-local so it never affects other
# threads' genuine errors.
_sql_log_local = threading.local()


def _sql_errors_suppressed() -> bool:
    return getattr(_sql_log_local, 'suppress', False)


# ── Process-level "PG is stopping" awareness ──
# During a clean server shutdown the local postmaster is stopped (see
# stop_local_pg_if_owned). Any DB write still in flight on a background daemon
# (commit-round / profile-consolidation / a just-finished run_task's finalize
# chain) then fails with "the database system is shutting down" /
# "server closed the connection unexpectedly". Those are EXPECTED shutdown-race
# errors, not bugs — but every call site would otherwise log a full ERROR
# traceback, producing the scary multi-traceback restart log. When this flag is
# set, the DB layer degrades those specific connection errors to a single
# concise INFO line (no stack). Genuine errors while NOT stopping are untouched.
_PG_STOPPING = False

# Connection errors that are EXPECTED once the postmaster is stopping.
_PG_SHUTDOWN_SIGNATURES = (
    'the database system is shutting down',
    'server closed the connection unexpectedly',
    'terminating connection due to administrator command',
    'the database system is in recovery mode',
)


def mark_pg_stopping():
    """Flag that a local-PG stop is in progress, so the DB layer treats
    connection failures as expected shutdown races (single INFO line, no
    traceback) rather than genuine ERRORs. Idempotent."""
    global _PG_STOPPING
    _PG_STOPPING = True


def pg_is_stopping() -> bool:
    return _PG_STOPPING


def _pg_error_is_shutdown(err_txt) -> bool:
    """True if err_txt is an expected PG-shutting-down / connection-dropped
    signature (matched only while a stop is in progress)."""
    if not err_txt:
        return False
    return any(sig in err_txt for sig in _PG_SHUTDOWN_SIGNATURES)


def _sql_error_is_expected_shutdown(err_txt) -> bool:
    """True when a stop is in progress AND the error is a shutdown signature —
    the condition under which a SQL/connection failure should log as a single
    concise INFO line instead of a full ERROR traceback."""
    return _PG_STOPPING and _pg_error_is_shutdown(err_txt)


def is_expected_shutdown_error(exc) -> bool:
    """Public predicate for UPPER-layer catch sites.

    A background finalize write (checkpoint / result-sync / commit-round /
    profile-consolidation / killed-recovery re-stamp) that races the postmaster
    stop fails with a connection error that ORIGINATES at ``psycopg2.connect``
    and propagates all the way up — so the DB layer cannot reformat the
    caller's own ``exc_info=True`` log line. Those catch sites call THIS single
    shared predicate: when it returns True they log a concise one-liner instead
    of a full traceback. Centralises the "is this an expected shutdown race?"
    decision in one place (never re-implemented per site). Returns True only
    while a stop is in progress AND the error text is a shutdown signature."""
    try:
        return _sql_error_is_expected_shutdown(str(exc))
    except Exception as e:
        logger.debug('[DB] is_expected_shutdown_error probe failed: %s', e)
        return False


def log_db_finalize_error(log, level, exc, label):
    """Log a background/finalize DB-write failure with shutdown awareness.

    Background finalize writes (checkpoint / result-sync / commit-round /
    profile-consolidation / killed-recovery re-stamp / queue auto-dispatch)
    each catch their own DB exception and log it with ``exc_info=True``. During
    a clean shutdown those are EXPECTED races, not bugs — this helper degrades
    them to a single concise INFO line (no traceback) so the restart log stays
    readable. When it is NOT an expected shutdown race, it logs at ``level``
    (``'error'``/``'warning'``) WITH the traceback, exactly as before.

    ``label`` is the pre-formatted context prefix (the caller interpolates its
    own ids). Runs only on an exception path, so eager formatting is fine.
    """
    if is_expected_shutdown_error(exc):
        log.info('%s — DB write aborted during shutdown (expected: %s)',
                 label, type(exc).__name__)
        return
    getattr(log, level, log.warning)('%s: %s', label, exc, exc_info=True)


class suppress_sql_error_log:
    """Context manager: demote SQL-execution-failure logs to DEBUG on this
    thread for queries that are expected to fail as normal control flow.

    The exception is still raised — only the ERROR-level log line is
    suppressed (re-emitted at DEBUG so it remains visible under
    ``TOFU_DB_LOG_LEVEL=DEBUG``).
    """

    def __enter__(self):
        self._prev = getattr(_sql_log_local, 'suppress', False)
        _sql_log_local.suppress = True
        return self

    def __exit__(self, *exc):
        _sql_log_local.suppress = self._prev
        return False

# ═══════════════════════════════════════════════════════════════════════
#  Backend Detection
# ═══════════════════════════════════════════════════════════════════════

# Which backend is active: 'pg' or 'sqlite'
_BACKEND = 'sqlite'  # default, upgraded to 'pg' below if possible


def _selected_backend() -> str:
    """Resolve the one exact backend selector; invalid values fail closed."""
    backend = (os.environ.get('TOFU_DB_BACKEND') or 'sqlite').strip().lower()
    if backend not in {'sqlite', 'postgres'}:
        raise RuntimeError(
            'TOFU_DB_BACKEND must be exactly sqlite or postgres; refusing '
            'backend inference or fallback')
    return backend


def _pg_explicitly_selected() -> bool:
    return _selected_backend() == 'postgres'


def _require_pg() -> bool:
    """Compatibility predicate backed only by the canonical selector."""
    return _pg_explicitly_selected()


def _assert_sqlite_fallback_has_no_pg_history(has_pg_history: bool,
                                               pgdata: str = '') -> None:
    """Refuse a SQLite fallback when recoverable PostgreSQL history exists.

    Serving an unrelated SQLite file is data divergence, even for a
    single-user deployment.  Fresh hosts with no PG history still fall back to
    zero-config SQLite exactly as before.
    """
    if not has_pg_history:
        return
    raise RuntimeError(
        f'PostgreSQL history exists at {pgdata or "the configured data root"} '
        'but PostgreSQL is unavailable — refusing to serve a divergent SQLite '
        'database. Recover PostgreSQL or complete a verified PG-to-SQLite '
        'migration first.')


def _pg_require_wait_s() -> float:
    """Bounded seconds to wait for PG to come up before fail-closing.

    Returns 0.0 unless PostgreSQL is the exact selected backend. For that
    selection, default to 60s (tunable via ``TOFU_PG_REQUIRE_WAIT_S``), because
    on a self-triggered re-exec the app-owned postmaster is torn down with the
    old process and returns within seconds; a single 5s probe that then aborts
    turns that transient race into a hard boot failure."""
    if not _require_pg():
        return 0.0
    from lib.env_compat import getenv_compat as _ge
    raw = (_ge('TOFU_PG_REQUIRE_WAIT_S', default='') or '').strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        logger.warning('[DB] TOFU_PG_REQUIRE_WAIT_S=%r is not a number — '
                       'defaulting to 60s', raw)
        return 60.0


def _retry_until(attempt, deadline_s: float, *, sleep=None, monotonic=None,
                 base_backoff_s: float = 1.0, max_backoff_s: float = 8.0):
    """Call ``attempt()`` until it returns a truthy result or the deadline.

    Generic bounded-retry-with-exponential-backoff primitive. ``sleep`` and
    ``monotonic`` are injectable for deterministic tests. When ``deadline_s``
    is <= 0 this is exactly ONE attempt with no sleep (the byte-identical
    single-box path). Total sleeping is clamped so it never overshoots the
    deadline. Returns the first truthy result, else None."""
    import time as _time
    sleep = sleep or _time.sleep
    monotonic = monotonic or _time.monotonic
    start = monotonic()
    backoff = base_backoff_s
    while True:
        result = attempt()
        if result:
            return result
        elapsed = monotonic() - start
        remaining = deadline_s - elapsed
        if remaining <= 0:
            return None
        wait = min(backoff, max_backoff_s, remaining)
        if wait <= 0:
            return None
        sleep(wait)
        backoff *= 2


def _assert_pg_available_or_raise(pg_ok: bool, pgdata: str = '') -> None:
    """Fail closed when the exact selected PostgreSQL backend is unavailable."""
    if pg_ok or not _require_pg():
        return
    try:
        from lib.log import audit_log
        audit_log('pg_required_but_unavailable', pgdata=pgdata)
    except Exception as _ae:
        logger.debug('[DB] audit_log for selected PostgreSQL failed: %s', _ae)
    logger.critical(
        '[DB] PostgreSQL is required/selected but unavailable — REFUSING to '
        'start or switch to SQLite. Provision/repair the selected PG backend '
        '(see logs/storage-postgresql.log) or explicitly select SQLite before startup.')
    raise RuntimeError(
        'PostgreSQL is explicitly required/selected and unavailable — refusing to fall '
        'back to SQLite for a scale-declared deployment.')


# ═══════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════

# The WRITABLE base dir (parent of the data/ + logs/ roots). In a frozen
# desktop build this is a user-writable location rather than the read-only
# _internal/ bundle — see lib/runtime_paths. base_dir is also handed to the
# PG bootstrap (which places data/pg_backups + logs/postgresql.log under it).
from lib.runtime_paths import data_root  # noqa: E402

_DB_DIR = data_root()
BASE_DIR = os.path.dirname(_DB_DIR)

# SQLite path (used as fallback): ``data/tofu.db``.
from lib.env_compat import getenv_compat  # noqa: E402

_DEFAULT_DB_FILE = os.path.join(_DB_DIR, 'tofu.db')
_explicit_db_path = getenv_compat('TOFU_DB_PATH', default='')
DB_PATH = _explicit_db_path or _DEFAULT_DB_FILE

# Install before route/plugin discovery. The interposer starts with this exact
# authority path; data-layer repositories may register auxiliary authorities
# later. Unregistered dependency-private SQLite files remain available.
from lib.database.sqlite_driver_guard import (  # noqa: E402
    allow_sqlite_driver_connection,
    install_sqlite_driver_guard,
)
if (os.environ.get('TOFU_SERVER_PROCESS') == '1'
        and 'pytest' not in sys.modules):
    install_sqlite_driver_guard(DB_PATH)

# PostgreSQL config
PG_HOST = getenv_compat('TOFU_PG_HOST', default='127.0.0.1')
PG_PORT = _bounded_env_int('TOFU_PG_PORT', 15432, 0, 65535)
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
_IDLE_IN_TRANSACTION_S = _bounded_env_int(
    'TOFU_DB_IDLE_IN_TRANSACTION_S', 120, 30, 3600)


def _pg_via_pooler():
    """True when the runtime PG connection goes through a transaction-pooling
    tier (PgBouncer). Env-gated OFF by default → single-box/desktop behaviour
    is byte-identical. See docs/EPIC_D §5a (managed PgBouncer HA, transaction
    pooling) and the D2 session-state audit.
    """
    return os.environ.get('TOFU_PG_VIA_POOLER', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _pg_session_setup_plan(via_pooler):
    """Decide HOW to apply the two connection-scoped timeout GUCs.

    Under transaction pooling a server backend is returned to the pool at every
    COMMIT, so a connect-time ``SET SESSION`` (a) silently no-ops for the setter
    (a later transaction may land on a different backend) and (b) LEAKS the GUC
    to the next unrelated pool borrower. The pooler-safe form ships the SAME two
    timeout values as libpq *startup* options (``-c statement_timeout=…``) so
    they ride the server backend for its whole pooled lifetime and are part of
    the pool key — never leaking, never no-op'ing — and skips the SET SESSION.

    Returns ``{'emit_set_session': bool, 'options': str|None}``. With
    ``via_pooler`` False the plan is byte-identical to the legacy path
    (SET SESSION emitted, no libpq ``options``).
    """
    if not via_pooler:
        return {'emit_set_session': True, 'options': None}
    # Carry the SAME units as the legacy SET SESSION so the enforced timeouts
    # are identical — only the delivery mechanism changes. libpq's default unit
    # for these GUCs is ms, so idle_in_transaction MUST keep its 's' suffix
    # (a bare 120 would mean 120ms, not 120s).
    options = (f'-c statement_timeout={_STATEMENT_TIMEOUT_MS}ms '
               f'-c idle_in_transaction_session_timeout={_IDLE_IN_TRANSACTION_S}s')
    return {'emit_set_session': False, 'options': options}


def _pg_admin_dsn():
    """DSN for the DIRECT (pooler-bypassing) admin lane.

    Multi-statement DDL migrations and ``VACUUM (FULL)`` / ``REINDEX`` cannot
    run through a transaction pooler (VACUUM FULL is illegal inside a pooled
    transaction block; a DDL ``SET SESSION`` + restore straddles a connection
    recycle), so those paths must reach a genuinely-direct PG endpoint. Set
    ``TOFU_PG_DIRECT_DSN`` to the real backend when the runtime DSN points at a
    pooler (PgBouncer). Defaults to the normal ``PG_DSN`` when unset — so a
    non-pooler deployment (the single-box default) is unaffected.
    """
    return os.environ.get('TOFU_PG_DIRECT_DSN', '').strip() or PG_DSN


def _pg_connect_target(admin=False):
    """Resolve (dsn, session_plan) for a new PG connection.

    Normal connections follow the pooler decision (``_pg_via_pooler`` →
    startup-options vs SET SESSION). An ``admin`` connection ALWAYS uses a
    real session (SET SESSION, never the pooler startup-options form) and,
    when pooling is on, connects to the direct DSN so it bypasses the pooler.

    With pooling OFF this returns exactly the legacy target for BOTH modes
    (normal DSN + SET SESSION), so single-box behaviour is byte-identical and
    ``admin`` is a no-op.
    """
    if admin:
        return _pg_admin_dsn(), {'emit_set_session': True, 'options': None}
    return PG_DSN, _pg_session_setup_plan(_pg_via_pooler())
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
_IDLE_RELEASE_S = _bounded_env_int(
    'TOFU_DB_IDLE_RELEASE_S', 120, 0, 86_400)

# Maximum total application-side connections (semaphore-guarded).
# Tofu's zero-config contract is a personal, single-process server, so the
# default is intentionally modest. Hundreds of mostly-idle backends make a
# one-user restart storm *less* reliable (more memory, locks and context
# switching) and historically hid thread-local leaks. Multi-user operators can
# raise the explicit env knob; overload still surfaces as a clean 503 rather
# than PostgreSQL's hard "too many clients" FATAL.


def _pool_knob(name, default):
    """Read a POOL-CRITICAL env knob with the .env FILE as authoritative.

    Why not plain getenv_compat: both launchers (server.py / bootstrap.py)
    load .env with ``if key not in os.environ`` — a *fill-if-absent* policy.
    That means a value inherited from the container / IDE launch environment
    SHADOWS the operator's .env file. For most knobs that's fine, but for the
    pool ceiling / acquire timeout it is a silent-revert footgun: a stale
    exported ``TOFU_DB_MAX_CONNS=400`` keeps overriding an intended .env=800
    on EVERY restart (self-update, crash-reboot, container respawn), so the
    storm-hardening never actually arms. (Observed 2026-07-16.)

    Policy for these specific knobs: the .env file wins. If a live shell/env
    export DISAGREES with the file, log a loud WARNING with BOTH values so the
    drift is never silent, then use the file value. If there is no .env entry,
    fall back to the environment (getenv_compat) exactly as before.

    Deliberately narrow: only the pool knobs opt in, so intentional exports of
    other TOFU_* vars keep their normal fill-if-absent precedence.
    """
    env_val = getenv_compat(name, default=None)
    file_val = None
    try:
        env_path = os.path.join(BASE_DIR, '.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    if k.strip() == name:
                        file_val = v.strip()
                        # keep last non-comment assignment (mirrors shell semantics)
    except Exception as e:
        logger.debug('[DB] could not read .env for %s: %s', name, e)

    if file_val is not None:
        if env_val is not None and str(env_val).strip() != file_val:
            logger.warning(
                '[DB] pool knob %s: shell/env export (%s) DISAGREES with .env '
                'file (%s) — using .env value %s (file is authoritative for '
                'pool knobs; unset the stale export at its source to silence)',
                name, env_val, file_val, file_val)
        return file_val
    return env_val if env_val is not None else default


def _bounded_pool_int(name, default, *, minimum, maximum):
    """Parse a pool knob without allowing bad config to brick startup."""
    raw = _pool_knob(name, default=str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[DB] Invalid %s=%r — using %d', name, raw, default)
        value = default
    if value < minimum or value > maximum:
        clamped = min(max(value, minimum), maximum)
        logger.warning('[DB] %s=%s outside safe range [%d, %d] — clamped to %d',
                       name, value, minimum, maximum, clamped)
        value = clamped
    return value


_MAX_TOTAL_CONNS = _bounded_pool_int(
    'TOFU_DB_MAX_CONNS', 64, minimum=4, maximum=4096)
_CONN_ACQUIRE_TIMEOUT_S = _bounded_pool_int(
    'TOFU_DB_ACQUIRE_TIMEOUT', 8, minimum=1, maximum=300)
_conn_semaphore = threading.BoundedSemaphore(_MAX_TOTAL_CONNS)
_conn_count = 0
_conn_count_lock = threading.Lock()


class PoolExhaustedError(RuntimeError):
    """Raised when the connection semaphore times out (pool saturated).

    A DISTINCT type (not a bare RuntimeError) so request handlers can catch
    it specifically and shed load with an HTTP 503 + Retry-After — a
    *transient overload* signal — instead of letting it surface as a generic
    uncaught 500. A 500 makes the frontend poll loop treat the response as a
    hard failure and retry harder, turning a momentary reconnection burst
    (e.g. after a restart, when hundreds of tabs re-poll at once) into a
    self-amplifying thundering-herd storm. 503 tells the client to back off.

    Carries the pool snapshot at failure time for diagnosability.
    """

    def __init__(self, message, *, active=0, max_conns=0, pooled=0, tracked=0):
        super().__init__(message)
        self.active = active
        self.max_conns = max_conns
        self.pooled = pooled
        self.tracked = tracked

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
    # FUSE-stall / container-IP-flap: a locally-owned postmaster stops
    # answering 127.0.0.1 and psycopg2 reports a connect timeout rather than
    # "Connection refused". Without this signature the self-heal never fired
    # and every DB call spun on timeouts forever (scheduler + MCP keepalive
    # were the first to surface it). _maybe_reboot_pg() is gated on
    # is_pg_owned_locally(), so this only ever reboots PG WE own — a timeout
    # against a genuine remote-owner DSN is left to the bootstrap layer.
    'timeout expired',
    'could not connect to server',
    'Operation timed out',
)
_PG_ZOMBIE_SIGNATURES = (
    'could not open shared memory segment',
)

_PG_REBOOT_COOLDOWN_S = _bounded_env_int(
    'TOFU_PG_REBOOT_COOLDOWN_S', 60, 0, 3600)
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


class SQLiteWriteDisciplineError(RuntimeError):
    """A write attempted outside the data layer's SQLite transaction rules.

    This is deliberately distinct from ``sqlite3.OperationalError``: callers
    must fix the transaction boundary instead of retrying an operation whose
    WAL snapshot can no longer be upgraded safely.
    """


class _SqliteCursorWrapper:
    """Wraps a sqlite3 cursor to return DictRow objects."""

    def __init__(self, real_cursor, conn):
        self._cursor = real_cursor
        self._conn = conn
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        _t0 = time.monotonic()
        _is_write = _sqlite_statement_is_write(sql)
        try:
            if _is_write:
                self._conn._assert_write_discipline(sql)
                self._conn._acquire_write_lane(sql)
            self._conn._operation_active = True
            try:
                if params:
                    self._cursor.execute(sql, params)
                else:
                    self._cursor.execute(sql)
            finally:
                self._conn._operation_active = False
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            if _is_write:
                self._conn._dirty = True
            self._conn._release_write_lane_if_transaction_ended()
        except Exception as e:
            self._conn._release_write_lane_if_transaction_ended()
            if _sql_errors_suppressed():
                logger.debug('[DB] SQL execution failed (%s) [suppressed]: %.120s', type(e).__name__, e)
            else:
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
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        _is_write = _sqlite_statement_is_write(sql)
        try:
            if _is_write:
                self._conn._assert_write_discipline(sql)
                self._conn._acquire_write_lane(sql)
            self._conn._operation_active = True
            try:
                self._cursor.executemany(sql, params_list)
            finally:
                self._conn._operation_active = False
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            if _is_write:
                self._conn._dirty = True
            self._conn._release_write_lane_if_transaction_ended()
        except Exception as e:
            self._conn._release_write_lane_if_transaction_ended()
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


# ── Global commit counter (docs/STORAGE_REDESIGN.md §8 acceptance metric) ──
# Approximate by design (no lock — the GIL makes += good enough for a gauge).
_COMMIT_TOTAL = 0

# SQLite has one physical writer slot even in WAL mode.  Letting every pooled
# connection race for that file lock is what turns one leaked/long transaction
# into a process-wide ``database is locked`` cascade.  This process-level lane
# queues writers *before* SQLite is touched; the winning connection keeps the
# lane for its whole transaction and releases it at commit/rollback/close.
_SQLITE_WRITE_LANE = threading.Lock()
_SQLITE_WRITE_LANE_STATE = threading.Lock()
_SQLITE_WRITE_LANE_OWNER = None
_SQLITE_WRITE_LANE_WAITERS = 0
_SQLITE_WRITE_LANE_STATS = {
    'acquires': 0,
    'contended': 0,
    'timeouts': 0,
    'wait_seconds': 0.0,
    'max_wait_seconds': 0.0,
    'hold_seconds': 0.0,
    'max_hold_seconds': 0.0,
}
_SQLITE_LEADING_NOISE_RE = re.compile(
    r'^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*', re.DOTALL)
_SQLITE_WRITE_WORD_RE = re.compile(
    r'\b(?:INSERT|UPDATE|DELETE|REPLACE)\b', re.IGNORECASE)


def _sqlite_statement_head(sql) -> tuple[str, str]:
    text = str(sql or '')
    text = _SQLITE_LEADING_NOISE_RE.sub('', text, count=1)
    match = re.match(r'([A-Za-z]+)', text)
    return (match.group(1).upper() if match else ''), text


def _sqlite_statement_is_write(sql) -> bool:
    """Conservatively classify statements that can take SQLite's writer lock."""
    head, text = _sqlite_statement_head(sql)
    if head in {
            'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER',
            'DROP', 'VACUUM', 'REINDEX', 'ATTACH', 'DETACH', 'ANALYZE'}:
        return True
    if head == 'WITH':
        return _SQLITE_WRITE_WORD_RE.search(text) is not None
    if head in {'BEGIN', 'SAVEPOINT'}:
        # A deferred read transaction can later fail its read->write upgrade
        # with SQLITE_BUSY_SNAPSHOT when another connection commits between
        # the SELECT and first mutation.  Reserve the application writer lane
        # at every explicit transaction boundary; begin() below additionally
        # asks SQLite for the physical writer slot immediately.
        #
        # A top-level SAVEPOINT also starts a deferred transaction.  The
        # conversation-message mirror deliberately uses one so it can compose
        # with an outer transaction, then reads row state before its first
        # mutation.  Treating SAVEPOINT as read-only recreated the same stale
        # snapshot upgrade race even though ordinary BEGIN was protected.
        return True
    if head == 'PRAGMA':
        # Read-only introspection (table_info, index_list, foreign_key_check,
        # etc.) must not pin the lane. Assignment and the explicitly mutating
        # maintenance pragmas do.
        return ('=' in text or bool(re.match(
            r'(?is)^\s*PRAGMA\s+(?:wal_checkpoint|optimize|incremental_vacuum)\b',
            text)))
    return False


def get_commit_count():
    """Total DB commits this process has issued (all backends combined)."""
    return _COMMIT_TOTAL


def reset_commit_count():
    """Reset the commit gauge (acceptance probes snapshot around a workload)."""
    global _COMMIT_TOTAL
    _COMMIT_TOTAL = 0


def get_sqlite_writer_lane_stats():
    """Return a non-sensitive snapshot of single-writer pressure metrics."""
    with _SQLITE_WRITE_LANE_STATE:
        stats = dict(_SQLITE_WRITE_LANE_STATS)
        owner = _SQLITE_WRITE_LANE_OWNER
        stats['waiting'] = _SQLITE_WRITE_LANE_WAITERS
        stats['held'] = 1 if owner is not None else 0
        stats['owner_age_seconds'] = (
            max(0.0, time.monotonic() - owner['since_monotonic'])
            if owner is not None else 0.0)
    return stats


class _SqliteConnectionWrapper:
    """SQLite connection wrapper providing the same API as PgConnection."""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._closed = False
        self._dirty = False
        self._created_at = time.monotonic()
        self._last_used = time.monotonic()
        self._write_lane_held = False
        self._write_lane_owner_ident = None
        self._operation_active = False
        self.row_factory = None

    @property
    def raw(self):
        """Access the underlying sqlite3 connection for special operations."""
        return self._conn

    @property
    def dialect(self):
        """Stable data-layer capability for backend-specific planner hints."""
        return 'sqlite'

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
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        needs_lane = _sqlite_statement_is_write(sql) or bool(
            re.search(r'(?is)(?:^|;)\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|VACUUM|REINDEX|ATTACH|DETACH|ANALYZE)\b',
                      str(sql or '')))
        if needs_lane:
            self._assert_write_discipline(sql)
            self._acquire_write_lane(sql)
        self._operation_active = True
        try:
            self._conn.executescript(sql)
            self._dirty = needs_lane
        finally:
            self._operation_active = False
            self._release_write_lane_if_transaction_ended()

    def _acquire_write_lane(self, sql=''):
        global _SQLITE_WRITE_LANE_OWNER, _SQLITE_WRITE_LANE_WAITERS
        if self._write_lane_held:
            if self._write_lane_owner_ident != threading.get_ident():
                raise SQLiteWriteDisciplineError(
                    'SQLite write transaction was entered from a different '
                    'thread than its writer-lane owner')
            return
        from lib.database.sqlite_owner import assert_owner
        assert_owner(
            getattr(self, '_pool_path', DB_PATH), _DEFAULT_DB_FILE,
            authority_path=DB_PATH)
        started = time.monotonic()
        timeout_s = max(0.001, _BUSY_TIMEOUT_MS / 1000)
        was_contended = _SQLITE_WRITE_LANE.locked()
        with _SQLITE_WRITE_LANE_STATE:
            _SQLITE_WRITE_LANE_WAITERS += 1
        try:
            acquired = _SQLITE_WRITE_LANE.acquire(timeout=timeout_s)
        finally:
            with _SQLITE_WRITE_LANE_STATE:
                _SQLITE_WRITE_LANE_WAITERS -= 1
        waited = time.monotonic() - started
        if not acquired:
            with _SQLITE_WRITE_LANE_STATE:
                owner = _SQLITE_WRITE_LANE_OWNER
                _SQLITE_WRITE_LANE_STATS['timeouts'] += 1
                _SQLITE_WRITE_LANE_STATS['wait_seconds'] += waited
                _SQLITE_WRITE_LANE_STATS['max_wait_seconds'] = max(
                    _SQLITE_WRITE_LANE_STATS['max_wait_seconds'], waited)
                owner_thread = owner.get('thread') if owner else None
                owner_age = (max(0.0, time.monotonic()
                                 - owner['since_monotonic'])
                             if owner else 0.0)
            raise sqlite3.OperationalError(
                f'SQLite writer lane timed out after {waited:.3f}s; '
                f'owner_thread={owner_thread!r}; held={owner_age:.3f}s')
        self._write_lane_held = True
        self._write_lane_owner_ident = threading.get_ident()
        with _SQLITE_WRITE_LANE_STATE:
            _SQLITE_WRITE_LANE_STATS['acquires'] += 1
            _SQLITE_WRITE_LANE_STATS['wait_seconds'] += waited
            _SQLITE_WRITE_LANE_STATS['max_wait_seconds'] = max(
                _SQLITE_WRITE_LANE_STATS['max_wait_seconds'], waited)
            if was_contended or waited >= 0.001:
                _SQLITE_WRITE_LANE_STATS['contended'] += 1
            _SQLITE_WRITE_LANE_OWNER = {
                'thread': threading.current_thread().name,
                'since': time.time(),
                'since_monotonic': time.monotonic(),
            }
        if waited >= 0.1:
            logger.info('[DB] SQLite writer lane waited %.0fms', waited * 1000)

    def _assert_write_discipline(self, sql=''):
        """Reject stale-snapshot and cross-thread writes before SQLite sees them."""
        current_ident = threading.get_ident()
        if self._write_lane_held:
            if self._write_lane_owner_ident != current_ident:
                raise SQLiteWriteDisciplineError(
                    'SQLite write transaction was entered from a different '
                    'thread than its writer-lane owner')
            return
        if self._conn.in_transaction:
            head, _ = _sqlite_statement_head(sql)
            raise SQLiteWriteDisciplineError(
                'SQLite write attempted from an existing transaction without '
                'reserving the writer before its first read; use db.begin() '
                f'or write_transaction() (statement={head or "UNKNOWN"})')

    def _release_write_lane(self):
        if not self._write_lane_held:
            return
        self._write_lane_held = False
        self._write_lane_owner_ident = None
        global _SQLITE_WRITE_LANE_OWNER
        with _SQLITE_WRITE_LANE_STATE:
            owner = _SQLITE_WRITE_LANE_OWNER
            if owner is not None:
                held = max(0.0, time.monotonic() - owner['since_monotonic'])
                _SQLITE_WRITE_LANE_STATS['hold_seconds'] += held
                _SQLITE_WRITE_LANE_STATS['max_hold_seconds'] = max(
                    _SQLITE_WRITE_LANE_STATS['max_hold_seconds'], held)
            _SQLITE_WRITE_LANE_OWNER = None
        _SQLITE_WRITE_LANE.release()

    def _release_write_lane_if_transaction_ended(self):
        if self._write_lane_held and not self._conn.in_transaction:
            self._release_write_lane()

    def begin(self):
        """Begin an explicit transaction (shared facade with PgConnection)."""
        # WAL permits readers beside a writer but still has exactly one writer.
        # BEGIN (DEFERRED) followed by SELECT creates a snapshot that cannot be
        # upgraded after another writer commits: SQLite raises BUSY_SNAPSHOT
        # immediately and busy_timeout cannot help.  IMMEDIATE queues before
        # the first read and makes a read-first atomic transaction reliable.
        return self.execute('BEGIN IMMEDIATE')

    def commit(self):
        self._operation_active = True
        try:
            self._conn.commit()
            self._dirty = False
            global _COMMIT_TOTAL
            _COMMIT_TOTAL += 1
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                try:
                    self._conn.close()
                finally:
                    self._closed = True
            raise
        finally:
            self._operation_active = False
            self._release_write_lane()

    def rollback(self):
        self._operation_active = True
        try:
            self._conn.rollback()
            self._dirty = False
        except Exception:
            try:
                self._conn.close()
            finally:
                self._closed = True
            raise
        finally:
            self._operation_active = False
            self._release_write_lane()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except Exception as e:
                logger.debug('[DB] Error closing SQLite connection: %s', e)
            finally:
                self._release_write_lane()

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

def _bounded_sqlite_setting(name, default, minimum, maximum):
    """Parse SQLite tuning without letting a typo brick zero-config startup."""
    raw = getenv_compat(name, default=str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning('[DB] Invalid %s=%r; using %d', name, raw, default)
        value = default
    if value < minimum or value > maximum:
        clamped = min(max(value, minimum), maximum)
        logger.warning('[DB] %s=%s outside [%d, %d]; clamped to %d',
                       name, value, minimum, maximum, clamped)
        value = clamped
    return value


# SQLite busy timeout — the process lane handles ordinary contention; keep a
# generous file-lock fallback until the historical whole-conversation writes
# have completed their row-store cutover.
_BUSY_TIMEOUT_MS = _bounded_sqlite_setting(
    'TOFU_SQLITE_BUSY_TIMEOUT_MS', 30000, 100, 300000)
_SQLITE_WAL_AUTOCHECKPOINT_PAGES = _bounded_sqlite_setting(
    'TOFU_SQLITE_WAL_AUTOCHECKPOINT_PAGES', 4096, 256, 65536)

# SQLite connection pool (connections are cheap but file-handle churn adds up at 1000 users)
_sqlite_pool = []
_sqlite_pool_lock = threading.Lock()
_SQLITE_POOL_MAX = _bounded_sqlite_setting(
    'TOFU_SQLITE_POOL_MAX', 20, 1, 256)

# Every live wrapper, weakly held. The test harness's leaked-transaction
# reaper (tests/conftest.py) enumerates this to roll back write txns that
# outlived their test — in WAL one zombie txn write-locks the whole worker
# DB for every later test (the CI 'database is locked' cascade, 2026-08-06).
# Weak refs: zero production cost, no lifetime interference.
_LIVE_SQLITE_CONNS: 'weakref.WeakSet' = weakref.WeakSet()

# A transaction is allowed to execute a slow SQL statement, but it must never
# sit idle while application code performs HTTP/LLM/sleep work. SQLite has one
# writer for the whole process; one forgotten commit otherwise stalls every
# unrelated subsystem until somebody restarts the server.
_SQLITE_MAX_IDLE_WRITE_TXN_MS = _bounded_sqlite_setting(
    'TOFU_SQLITE_MAX_IDLE_WRITE_TXN_MS', 5000, 500, 300000)
_sqlite_txn_watchdog_lock = threading.Lock()
_sqlite_txn_watchdog_thread = None

#: Capture each connection's creation stack (tests set this via conftest;
#: off in production). Read once at import — test sessions set the env
#: before lib.database is imported.
_CONN_TRACE = getenv_compat('TOFU_DB_CONN_TRACE', default='').strip() not in ('', '0', 'false', 'no')


def reap_idle_write_transactions(idle_s: float = 1.0,
                                 owner_ident: int | None = None) -> int:
    """Roll back leaked sqlite write transactions; return the count.

    Test-harness belt (called by an autouse conftest fixture at every test
    boundary): pytest-timeout kills a test's MAIN thread, never its
    background threads, so a thread stopped mid-transaction holds its write
    txn forever — and in WAL that locks every later test out of the worker
    DB. A genuinely-live background writer touches its connection
    continuously (``_last_used`` is stamped on every execute); only a zombie
    goes quiet past the threshold.  ``owner_ident`` lets a test boundary reap
    a fresh transaction owned by that same runner thread immediately, without
    racing an unrelated live background writer. Not used by production paths.
    """
    reaped = 0
    now = time.monotonic()
    for w in list(_LIVE_SQLITE_CONNS):
        try:
            same_owner = (owner_ident is not None
                          and w._write_lane_owner_ident == owner_ident)
            if (not w._closed and w._conn.in_transaction
                    and (same_owner or now - w._last_used > idle_s)):
                logger.warning('[DB] reaping leaked write txn (idle %.1fs) — '
                               'a thread outlived its owner mid-transaction',
                               now - w._last_used)
                w.rollback()
                reaped += 1
        except Exception as e:
            logger.debug('[DB] txn reap probe failed: %s', e)
    return reaped


def _force_close_idle_write_transactions(idle_s: float) -> int:
    """Rollback+close production write transactions abandoned between calls.

    Closing (rather than returning the connection to its owner) is deliberate:
    the owner may resume after a network call believing its old transaction is
    intact. Its next DB operation must fail loudly and obtain a fresh
    thread-local connection instead of silently continuing a split unit.
    Active SQL/commit/rollback calls are never touched.
    """
    reaped = 0
    now = time.monotonic()
    for w in list(_LIVE_SQLITE_CONNS):
        try:
            idle = now - w._last_used
            if (w._closed or w._operation_active or not w._write_lane_held
                    or not w._conn.in_transaction or idle <= idle_s):
                continue
            owner = w._write_lane_owner_ident
            logger.critical(
                '[DB] Force-aborting idle SQLite write transaction after %.1fs '
                '(owner_ident=%s, max=%.1fs). Business code held the global '
                'writer across non-DB work; its connection is being closed.',
                idle, owner, idle_s)
            try:
                w._conn.rollback()
            finally:
                try:
                    w._conn.close()
                finally:
                    w._closed = True
                    w._dirty = False
                    w._release_write_lane()
            reaped += 1
        except Exception as e:
            # Even a broken raw handle must not leave the process lane pinned.
            try:
                w._closed = True
                w._release_write_lane()
            except Exception as release_error:
                logger.error('[DB] idle transaction watchdog release failed: %s',
                             release_error, exc_info=True)
            logger.error('[DB] idle transaction watchdog failed: %s', e,
                         exc_info=True)
    return reaped


def _sqlite_txn_watchdog_loop() -> None:
    idle_s = _SQLITE_MAX_IDLE_WRITE_TXN_MS / 1000.0
    interval = max(0.25, min(1.0, idle_s / 2.0))
    while True:
        time.sleep(interval)
        _force_close_idle_write_transactions(idle_s)


def _ensure_sqlite_txn_watchdog_started() -> bool:
    """Start the one process-wide leaked-writer circuit breaker."""
    global _sqlite_txn_watchdog_thread
    if ('pytest' in sys.modules
            or os.environ.get('TOFU_SERVER_PROCESS') != '1'
            or str(os.environ.get('TOFU_SQLITE_TXN_WATCHDOG', '1')).strip()
            .lower() in ('0', 'false', 'no', 'off')):
        return False
    with _sqlite_txn_watchdog_lock:
        if (_sqlite_txn_watchdog_thread is not None
                and _sqlite_txn_watchdog_thread.is_alive()):
            return False
        thread = threading.Thread(
            target=_sqlite_txn_watchdog_loop,
            name='sqlite-txn-watchdog', daemon=True)
        thread.start()
        _sqlite_txn_watchdog_thread = thread
        logger.info('[DB] SQLite idle-write watchdog armed (max_idle=%dms)',
                    _SQLITE_MAX_IDLE_WRITE_TXN_MS)
        return True


def _new_sqlite_connection():
    """Create a new SQLite connection with WAL mode and optimal settings."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with allow_sqlite_driver_connection('canonical pool connection'):
        conn = sqlite3.connect(
            DB_PATH,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level='DEFERRED',
        )
    w = _SqliteConnectionWrapper(conn)
    # Stamp the path this connection was opened for. The pool below is a
    # flat list and DB_PATH can be rebound at runtime (tests do it
    # constantly); without the stamp, _pool_get happily hands out a live
    # connection to a DIFFERENT file and the caller writes/reads the wrong
    # database (CI-only 404 on a committed row, 2026-08-05).
    w._pool_path = os.path.abspath(DB_PATH)
    try:
        # journal_mode and auto_vacuum are persistent database state and can
        # take SQLite's writer lock.  A brand-new authority opts into
        # INCREMENTAL auto-vacuum before its first table is created: later
        # bounded maintenance can then return deleted pages to the filesystem
        # without an exclusive, whole-file VACUUM.  Existing databases are
        # NEVER rewritten here — changing NONE -> INCREMENTAL after tables
        # exist requires VACUUM and would turn an ordinary startup into an
        # unbounded multi-GiB copy.
        #
        # Query first: once persistent setup has converged, later reader
        # connections need no mutating PRAGMA and must not queue behind an
        # unrelated live writer.  Re-check after acquiring the process lane so
        # concurrent cold connections share one safe transition.
        current_mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        current_auto_vacuum = conn.execute(
            'PRAGMA auto_vacuum').fetchone()[0]
        user_schema_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
        needs_persistent_setup = (
            str(current_mode).lower() != 'wal'
            or (int(current_auto_vacuum) == 0 and user_schema_count == 0)
        )
        if needs_persistent_setup:
            w._acquire_write_lane('SQLite persistent storage setup')
            try:
                # Another cold connection may have completed this while this
                # connection waited for the lane, so every decision is based
                # on state observed while ownership is exclusive.
                current_auto_vacuum = conn.execute(
                    'PRAGMA auto_vacuum').fetchone()[0]
                user_schema_count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
                if int(current_auto_vacuum) == 0 and user_schema_count == 0:
                    conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    selected_auto_vacuum = conn.execute(
                        'PRAGMA auto_vacuum').fetchone()[0]
                    if int(selected_auto_vacuum) != 2:
                        raise sqlite3.OperationalError(
                            'could not enable SQLite incremental auto-vacuum '
                            f'(got {selected_auto_vacuum!r})')

                current_mode = conn.execute(
                    'PRAGMA journal_mode').fetchone()[0]
                if str(current_mode).lower() != 'wal':
                    selected_mode = conn.execute(
                        'PRAGMA journal_mode=WAL').fetchone()[0]
                    if str(selected_mode).lower() != 'wal':
                        raise sqlite3.OperationalError(
                            'could not enable SQLite WAL mode '
                            f'(got {selected_mode!r})')
            finally:
                # These PRAGMAs apply outside a durable user transaction; do
                # not make request teardown commit setup again.  An exception
                # also reaches close() below, which releases the lane.
                w._dirty = False
                w._release_write_lane_if_transaction_ended()
        # The remaining pragmas are connection-local tuning. Keep them raw so
        # they do not create six fake writer-lane acquisitions per connection.
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA cache_size=-8000')
        # mmap is disabled: when the DB lives on a FUSE mount (beegfs-fuse,
        # NFS, etc.) and the backend hiccups, mapped pages can become invalid
        # and the next access raises SIGBUS. Plain pread() returns EIO.
        conn.execute('PRAGMA mmap_size=0')
        # Same-mount A/B measured commit p95 223ms at 1,000 pages vs 64ms at
        # 4,096; 8,192 regressed to 102ms. Keep the measured ~16 MiB knee.
        conn.execute(
            f'PRAGMA wal_autocheckpoint={_SQLITE_WAL_AUTOCHECKPOINT_PAGES}')
    except Exception:
        w.close()
        raise
    if _CONN_TRACE:
        # Forensics: which code path opened this connection (the CI-only
        # stale-read/lock hunts). Connects are rare (pool reuse), so a
        # bounded stack capture is affordable even in busy test sessions.
        w._created_stack = ''.join(traceback.format_stack(limit=16)[:-1])
    _LIVE_SQLITE_CONNS.add(w)
    _ensure_sqlite_txn_watchdog_started()
    return w


# ═══════════════════════════════════════════════════════════════════════
#  PostgreSQL Connection Factory
# ═══════════════════════════════════════════════════════════════════════

def _new_pg_connection(admin=False):
    """Create a new psycopg2 connection with full resilience parameters.

    Guarded by a bounded semaphore to prevent overwhelming PG with too
    many simultaneous connections (the root cause of 'too many clients').

    ``admin=True`` requests the direct (pooler-bypassing) lane with a real
    session — for multi-statement DDL and VACUUM/REINDEX, which cannot run
    through a transaction pooler. See ``_pg_connect_target``.
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
        raise PoolExhaustedError(
            f'Database connection pool exhausted ({current}/{_MAX_TOTAL_CONNS} '
            f'connections in use, {pooled} pooled, {tracked} thread-tracked). '
            f'Increase TOFU_DB_MAX_CONNS (current={_MAX_TOTAL_CONNS}) or '
            f'check for unclosed thread-local connections.',
            active=current, max_conns=_MAX_TOTAL_CONNS, pooled=pooled, tracked=tracked,
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

    _dsn, _session_plan = _pg_connect_target(admin)
    _connect_kwargs = dict(
        connect_timeout=_CONNECT_TIMEOUT_S,
        keepalives=1,
        keepalives_idle=_TCP_KEEPALIVES_IDLE_S,
        keepalives_interval=_TCP_KEEPALIVES_INTERVAL_S,
        keepalives_count=_TCP_KEEPALIVES_COUNT,
        application_name='tofu',
        gssencmode='disable',
    )
    if _session_plan['options']:
        _connect_kwargs['options'] = _session_plan['options']
    try:
        try:
            conn = psycopg2.connect(_dsn, **_connect_kwargs)
        except psycopg2.OperationalError as e:
            err_txt = str(e)
            # Expected shutdown race: the postmaster is stopping while a
            # background finalize/daemon connection is being opened. Do NOT
            # attempt a self-heal reboot (we are on the way down) — re-raise so
            # the caller's finalize aborts; it logs one concise line via
            # is_expected_shutdown_error. Checked BEFORE the dead-signature
            # self-heal because "server closed the connection" overlaps both.
            if _sql_error_is_expected_shutdown(err_txt):
                raise
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
            conn = psycopg2.connect(_dsn, **_connect_kwargs)
    except Exception:
        _conn_semaphore.release()
        raise
    psycopg2.extensions.register_type(json_type, conn)
    psycopg2.extensions.register_type(jsonb_type, conn)
    conn.autocommit = False

    # Connect-time SET SESSION is applied ONLY on the non-pooler path. Under
    # transaction pooling these are delivered as libpq startup options above
    # (_session_plan['options']) — a SET SESSION on a pooled connection no-ops
    # for the setter and leaks the GUC to the next pool borrower. See
    # _pg_session_setup_plan.
    if _session_plan['emit_set_session']:
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

        # Never run the rollback-based liveness probe through a transaction
        # the caller still owns. A long but valid explicit/CAS transaction can
        # call get_db()/get_thread_db() again before commit; probing it used to
        # silently roll its work back. A dead raw connection was already
        # rejected above, and statement execution will surface later failures
        # without unsafe replay because the wrapper remains dirty/pinned.
        if (getattr(pg_conn, '_dirty', False)
                or getattr(pg_conn, '_transaction_pinned', False)):
            return True

        now = time.monotonic()

        age = now - pg_conn._created_at
        if age > _MAX_CONN_AGE_S:
            logger.debug('[DB] Connection expired (age=%.0fs > %ds), recycling', age, _MAX_CONN_AGE_S)
            return False

        idle = now - pg_conn._last_used
        # ★ Suspend guard: time.monotonic() FREEZES while the OS is suspended
        #   (laptop lid close / VM pause / FUSE stall), so a connection whose
        #   backend PG killed overnight still looks "used a few seconds ago" by
        #   the monotonic clock and the SELECT 1 probe below would be skipped —
        #   handing a DEAD connection to the request (the recurring "server
        #   closed the connection unexpectedly" on the first Send after
        #   reconnecting the next day). Cross-check the WALL clock: if either
        #   clock shows a gap >= _IDLE_CHECK_S, probe. The wall clock advances
        #   across a suspend even when monotonic did not, so a resumed process
        #   always probes and discards the stale connection.
        wall_idle = time.time() - getattr(pg_conn, '_last_used_wall', 0)
        if idle < _IDLE_CHECK_S and wall_idle < _IDLE_CHECK_S:
            return True

        raw.rollback()
        cur = raw.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        # psycopg2 autocommit is disabled: SELECT 1 opened a brand-new
        # transaction after the rollback above.  Finish that probe transaction
        # or the health check itself parks the backend idle-in-transaction.
        raw.rollback()
        pg_conn._last_used = now
        pg_conn._last_used_wall = time.time()
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


def _new_pg_admin_connection():
    """PG connection for the direct (pooler-bypassing) admin lane — DDL /
    VACUUM / REINDEX. See ``_pg_connect_target``. PG-only."""
    return _new_pg_connection(admin=True)


def _new_admin_connection():
    """Backend-aware admin connection. On PG this bypasses the pooler with a
    real session; on SQLite it is an ordinary connection (no pooler concept)."""
    if _BACKEND == 'pg':
        return _new_pg_admin_connection()
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
_CONN_POOL_MAX = min(
    _MAX_TOTAL_CONNS,
    _bounded_pool_int('TOFU_DB_POOL_MAX', 16, minimum=1, maximum=1024),
)


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
                    conn._transaction_pinned = False
                    return conn
                try:
                    conn.close()
                except Exception as e:
                    logger.debug('[DB] Error closing dead pooled PG connection: %s', e)
    else:
        # SQLite pool
        _want_path = os.path.abspath(DB_PATH)
        with _sqlite_pool_lock:
            while _sqlite_pool:
                conn = _sqlite_pool.pop()
                # A pooled connection bound to a DIFFERENT file (DB_PATH was
                # rebound since it was opened) must never be handed out —
                # close it and keep looking.
                if getattr(conn, '_pool_path', None) != _want_path:
                    try:
                        conn.close()
                    except Exception as e:
                        logger.debug('[DB] Error closing stale-path pooled SQLite connection: %s', e)
                    continue
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
            # The raw backend may have been killed by
            # idle_in_transaction_session_timeout.  Closing the wrapper is
            # still mandatory: it releases the application semaphore slot and
            # decrements _conn_count.
            conn.close()
            return
        try:
            conn.rollback()
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
            conn.rollback()
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


def assert_write_transaction(db, *, label='write helper'):
    """Fail loudly unless ``db`` is inside an owned write transaction.

    Semantic helper functions that intentionally issue bare ``db.execute``
    writes use this at entry. It converts the otherwise implicit
    "caller owns the transaction" convention into a runtime invariant and a
    static marker consumed by ``tests/test_database_access_boundary.py``.
    """
    if db is None:
        raise TypeError(f'{label} requires a data-layer connection')
    if isinstance(db, _SqliteConnectionWrapper):
        if (not db.raw.in_transaction or not db._write_lane_held
                or db._write_lane_owner_ident != threading.get_ident()):
            raise SQLiteWriteDisciplineError(
                f'{label} requires write_transaction() with the SQLite '
                'writer lane reserved')
        return db
    if isinstance(db, sqlite3.Connection):
        if not db.in_transaction:
            raise SQLiteWriteDisciplineError(
                f'{label} requires write_transaction()')
        return db
    if not bool(getattr(db, '_transaction_pinned', False)):
        raise RuntimeError(
            f'{label} requires write_transaction() with a pinned boundary')
    return db


@contextmanager
def write_transaction(db, *, label='write'):
    """Own one atomic write unit on an existing data-layer connection.

    The outermost boundary uses ``db.begin()``.  On SQLite that is
    ``BEGIN IMMEDIATE``, which reserves both the process lane and SQLite's
    physical writer slot *before* any read snapshot is taken.  Nested units
    use a savepoint and therefore roll back only their own work.

    This is the synchronous write primitive application repositories should
    use instead of hand-written BEGIN/commit/rollback triples.  It commits on
    clean outer exit, rolls back on every ``BaseException``, and refuses to
    nest inside a SQLite transaction that bypassed writer reservation.
    """
    if db is None:
        raise TypeError('write_transaction() requires a data-layer connection')

    is_sqlite = isinstance(db, _SqliteConnectionWrapper)
    # A few repository unit tests deliberately supply an in-memory DB-API
    # connection. Supporting that protocol here keeps transaction ownership in
    # the data layer; production code is statically forbidden from opening raw
    # sqlite3 connections outside lib/database.
    is_raw_sqlite = isinstance(db, sqlite3.Connection)
    if is_sqlite or is_raw_sqlite:
        nested = bool((db.raw if is_sqlite else db).in_transaction)
        if nested:
            # Fail before SAVEPOINT can accidentally bless a raw/unclassified
            # read transaction whose WAL snapshot is unsafe to upgrade.
            if is_sqlite:
                db._assert_write_discipline('SAVEPOINT')
    else:
        # PgConnection finishes disposable reads immediately.  Durable state
        # is represented by either a previous write or an explicitly pinned
        # boundary; those are the only legitimate nesting cases.
        nested = bool(getattr(db, '_dirty', False)
                      or getattr(db, '_transaction_pinned', False))

    if not nested:
        if is_raw_sqlite:
            db.execute('BEGIN IMMEDIATE')
        else:
            db.begin()
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
        return

    savepoint = (
        f'tofu_write_tx_{threading.get_ident()}_{time.monotonic_ns()}')
    db.execute(f'SAVEPOINT {savepoint}')
    try:
        yield db
    except BaseException:
        try:
            db.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
        finally:
            db.execute(f'RELEASE SAVEPOINT {savepoint}')
        raise
    else:
        db.execute(f'RELEASE SAVEPOINT {savepoint}')


@contextmanager
def pooled_db(domain=DOMAIN_CHAT):
    """Borrow one connection for a bounded synchronous unit of work.

    Unlike :func:`get_thread_db`, this lease is not attached to the calling
    thread.  It is therefore the preferred API for periodic pollers, janitors,
    short-lived worker callbacks, and other code that may sleep for minutes
    after a query.  The connection is always rolled back/sanitised by
    :func:`_pool_put` and returned to the shared pool, including when the body
    raises.  Writes must still call ``commit()`` explicitly; an accidental
    uncommitted write is rolled back on exit.

    ``domain`` is accepted for API parity with ``get_thread_db``.  The current
    dual-backend implementation shares one physical pool across domains.
    """
    del domain
    conn = _pool_get()
    try:
        yield conn
    finally:
        _pool_put(conn)


@contextmanager
def pooled_write_transaction(domain=DOMAIN_CHAT, *, label='write'):
    """Borrow, transact, sanitize, and return one synchronous connection."""
    with pooled_db(domain) as db:
        with write_transaction(db, label=label):
            yield db


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


def get_read_db(domain=DOMAIN_CHAT):
    """Read-only DB lane (Epic D3 scaffold).

    Returns a connection suitable for LAG-TOLERANT read-only queries (sidebar
    list, old-conversation load, search). Today it is an ALIAS of
    :func:`get_db` — the read-replica routing lands only when
    ``TOFU_PG_READ_REPLICAS`` is provisioned AND the infra sign-off from the
    Epic D design (§5) is granted, so the single-box / current behaviour is
    byte-identical. Call sites can adopt ``get_read_db`` incrementally NOW; the
    routing turns on later without touching them.

    Lag discipline (design D3 §2.3): NEVER use this for a read that must see a
    just-written row (e.g. immediately after ``chat_send`` persists, or a task
    poll of a freshly-persisted result) — those MUST stay on :func:`get_db`
    (the primary). Only lag-tolerant reads may adopt this lane.
    """
    from lib.env_compat import getenv_compat as _ge
    _replicas = (_ge('TOFU_PG_READ_REPLICAS', default='') or '').strip()
    if not _replicas or _BACKEND != 'pg':
        # Unset, or SQLite single-box → identical to the primary lane.
        return get_db(domain)
    # NOTE: replica-pool routing is the §10-gated infra follow-up (D3). Until
    # the replica pool exists, fall back to the primary so behaviour is safe
    # and unchanged; the seam is in place for the routing to slot in.
    logger.debug('[DB] get_read_db: TOFU_PG_READ_REPLICAS set but replica pool '
                 'not yet wired — using primary (D3 infra follow-up)')
    return get_db(domain)


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
    """Return a request-scoped or thread-local database connection.

    Historical callers use this helper from both background workers *and*
    request handlers. Pinning the latter to a server executor thread leaks one
    connection per reused worker: the worker lives far longer than the
    request, so normal app-context teardown cannot return it. Inside a
    Flask/Quart app context, delegate to :func:`get_db` and let ``close_db``
    return it after the request. Outside one, preserve the original
    thread-local contract for task workers and standalone scripts.
    """
    if _has_app_context():
        return get_db(domain)
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

def _reconnect_pg_inplace(db):
    """Swap a dead PgConnection's underlying raw connection for a fresh one.

    Adopts a brand-new psycopg2 connection (and its semaphore slot) into the
    existing ``db`` wrapper so callers holding the wrapper (request-scoped /
    thread-local / pooled) transparently continue on a live connection. The
    OLD raw connection is closed and its semaphore slot + global ``_conn_count``
    released here — otherwise every reconnect permanently leaks one pool slot
    and one PG backend (the fresh connection kept its own slot/count).

    Only meaningful on the PG backend. Returns True on success, False on
    failure (caller then re-raises the original error).
    """
    if _BACKEND != 'pg' or not hasattr(db, '_conn'):
        return False
    try:
        old_raw = db._conn
        fresh = _new_pg_connection()
        try:
            old_raw.close()
        except Exception as _c_err:
            logger.debug('[DB] Closing dead raw conn during reconnect failed: %s', _c_err)
        old_sem = getattr(db, '_semaphore', None)
        if old_sem is not None:
            try:
                old_sem.release()
            except ValueError:
                logger.debug('[DB] Old semaphore already released during reconnect')
            with _conn_count_lock:
                globals()['_conn_count'] = max(0, _conn_count - 1)
        db._conn = fresh._conn
        db._semaphore = fresh._semaphore
        db._created_at = fresh._created_at
        db._last_used = time.monotonic()
        db._last_used_wall = time.time()
        db._closed = False
        db._dirty = False
        db._transaction_pinned = False
        return True
    except Exception as re_err:
        logger.warning('[DB] In-place PG reconnect failed: %s', re_err)
        return False


def db_execute_with_retry(db, sql, params=(), *, commit=True, max_retries=3,
                          return_cursor=False):
    """Execute one SQL write with bounded transient-error retry.

    ``return_cursor`` is opt-in because the historical contract returned
    ``None``. It is needed by conditional writes whose ``rowcount`` is the
    ownership/fencing verdict; the cursor remains usable for metadata after
    commit on both supported backends.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            cursor = db.execute(sql, params)
            if commit:
                db.commit()
            return cursor if return_cursor else None
        except Exception as e:
            # Determine if retryable
            is_retryable = False
            if _BACKEND == 'sqlite':
                # 'disk i/o error' (SQLITE_IOERR) is transient on a FUSE/NFS
                # mount (backend hiccup → pread/pwrite EIO); a bounded retry
                # rides out the blip instead of failing the whole task. A
                # genuinely broken mount still exhausts max_retries and raises.
                sqlite_code = int(getattr(e, 'sqlite_errorcode', 0) or 0) & 0xFF
                is_retryable = sqlite_code in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                    sqlite3.SQLITE_IOERR,
                }
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
                        if _reconnect_pg_inplace(db):
                            logger.info('[DB-Retry] Reconnected underlying PG connection (was: %s)', etype)

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
        # Admin lane: VACUUM (FULL) / REINDEX below cannot run through a
        # transaction pooler, so bypass it (direct DSN + real session).
        conn = _new_admin_connection()
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

    Controlled by env var ``TOFU_STOP_PG_ON_EXIT`` (default ``0``):
      - ``0`` / unset: leave local PG running across a server.py restart. This
        is the default because PG is a SHARED resource: on a box with multiple
        sibling sessions (and mid-flight/quiesced tasks) all bound to the same
        local PG, stopping it on every code-reload restart tears every sibling's
        DB connections out — a blast radius far larger than the one stateless
        web process being reloaded. Leaving it up also makes restarts FASTER:
        the next boot's ``_ensure_pg_running`` sees ``pg_isready`` OK and
        ATTACHES instead of cold-starting a fresh postmaster. The heartbeat is
        still cleared on exit (below) so cross-host takeover on shared FUSE
        pgdata remains safe.
      - ``1``: stop local PG when server.py exits (e.g. a true host shutdown, or
        before switching hosts on shared FUSE pgdata).

    Never stops a REMOTE PG — that belongs to another machine.
    """
    if _BACKEND != 'pg':
        return
    # Flag the stop BEFORE we begin, so any DB write racing on a background
    # daemon during the teardown window is logged as an expected shutdown race
    # (single INFO line) rather than a full ERROR traceback.
    mark_pg_stopping()
    _stop_on_exit = getenv_compat('TOFU_STOP_PG_ON_EXIT',
                                  default='0').lower() \
        in ('1', 'true', 'yes', 'on')
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
                        'TOFU_STOP_PG_ON_EXIT=1 was set (default is 0 = keep '
                        'PG running across server.py restarts)')
            _boot_stop_pg(_PGDATA)
        else:
            logger.debug('[DB] Not stopping PG on exit (remote or attached, not owned by us)')
    except Exception as e:
        logger.warning('[DB] Failed to stop local PG on exit: %s', e)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Init (delegates to _schema module)
# ═══════════════════════════════════════════════════════════════════════

_OPTIONAL_DOMAINS_DISCOVERED = False
_OPTIONAL_DOMAINS_DISCOVERY_LOCK = threading.Lock()


def _register_optional_domains():
    """Wire optional DB-domain schema initializers before init_db() runs.

    Optional DB domains (e.g. the ``trading`` tables, now owned by the
    standalone ``tofu-trading`` package) register via the ``tofu.schema``
    entry-point group. Core defines no optional domains itself.

    Discovery is process-once: ``importlib.metadata.entry_points()`` scans
    every ``sys.path`` root and took 150s in the audited FUSE-heavy developer
    environment. A plugin installation already requires a process restart for
    routes/tools to appear, so rescanning on every idempotent schema init adds
    latency without enabling a supported hot-install flow.

    Ambient third-party entry points are skipped during ordinary pytest DB
    fixture setup. That both isolates unit schemas and avoids paying the same
    external-environment scan before every test invocation. Plugin integration
    tests can opt in with ``TOFU_TEST_SCHEMA_PLUGIN_DISCOVERY=1``.
    """
    global _OPTIONAL_DOMAINS_DISCOVERED
    if (os.environ.get('PYTEST_CURRENT_TEST')
            and os.environ.get('TOFU_TEST_SCHEMA_PLUGIN_DISCOVERY') != '1'):
        return
    if _OPTIONAL_DOMAINS_DISCOVERED:
        return
    with _OPTIONAL_DOMAINS_DISCOVERY_LOCK:
        if _OPTIONAL_DOMAINS_DISCOVERED:
            return
        from lib.database.schema_registry import discover_schema_plugins
        discover_schema_plugins()
        _OPTIONAL_DOMAINS_DISCOVERED = True


def init_db():
    """Initialize all database schemas using the active backend."""
    _register_optional_domains()
    if _BACKEND == 'pg':
        from lib.database._schema_pg import init_db as _pg_schema_init
        # Admin lane: multi-statement DDL + SET SESSION/restore below cannot run
        # through a transaction pooler, so it connects direct (bypasses it).
        _pg_schema_init(_new_pg_admin_connection, _STATEMENT_TIMEOUT_MS)
    else:
        from lib.database._schema_sqlite import init_db as _sqlite_schema_init
        _sqlite_schema_init(_new_sqlite_connection)
    # Row authority is an explicit deployment cutover. Validate its cheap,
    # permanent structural invariants on EVERY boot before the server accepts
    # traffic; a stale archive must never become an availability fallback.
    from lib.database._access_policy import rows_authority_configured
    if rows_authority_configured():
        authority_db = (_new_pg_connection() if _BACKEND == 'pg'
                        else _new_sqlite_connection())
        try:
            from lib.database.messages_rows import assert_rows_authority_ready
            assert_rows_authority_ready(authority_db)
            logger.info('[DB] normalized conversation rows passed authority preflight')
        finally:
            authority_db.close()


def reset_sqlite_for_tests(db_path: str):
    """Repoint the SQLite backend at a fresh ``db_path`` and re-init schema.

    Test-only helper that makes per-test / per-class DB isolation actually
    work. The naive approach — ``os.environ['TOFU_DB_PATH'] = ...`` inside a
    ``setUpClass`` — has NO effect, because ``_BACKEND`` and ``DB_PATH`` are
    module-level globals frozen when ``lib.database._core`` is first imported
    (which happens at conftest collection time, long before any test body
    runs). Worse, if the ambient environment selects PostgreSQL, unit tests
    that expect SQLite silently share the live PG and accumulate cross-test
    state (duplicate-email / stale-balance failures that depend on run order).

    This function performs the repoint that the env-var alone cannot:

      1. Forces the active backend to SQLite (overriding any ambient PG
         selection) so these unit tests never touch a real database.
      2. Sets the module global ``DB_PATH`` — read fresh inside
         :func:`_new_sqlite_connection` on every connect — to the new file.
      3. Drops every cached connection (the shared SQLite pool AND all
         thread-local handles) so the next ``get_thread_db`` / ``_pool_get``
         opens against the new path instead of the old one.
      4. Re-runs :func:`init_db` to create the schema in the fresh file.

    Args:
        db_path: Absolute path to the (fresh) SQLite file to use. Its parent
            directory is created if missing.

    Raises:
        ValueError: if ``db_path`` is empty.
    """
    global _BACKEND, DB_PATH, db_available, pg_available
    if not db_path:
        raise ValueError('reset_sqlite_for_tests requires a non-empty db_path')

    snapshot = {
        '_BACKEND': _BACKEND, 'DB_PATH': DB_PATH,
        'db_available': db_available, 'pg_available': pg_available,
    }

    _BACKEND = 'sqlite'
    db_available = True
    pg_available = False
    DB_PATH = db_path
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)

    # Drain the shared SQLite pool.
    with _sqlite_pool_lock:
        while _sqlite_pool:
            conn = _sqlite_pool.pop()
            try:
                conn.close()
            except Exception as e:
                logger.debug('[DB] reset_sqlite_for_tests: pool close failed: %s', e)

    # Drop every thread-local connection so the next access reconnects to the
    # new path. We can only directly reach the calling thread's handles via
    # _thread_local; replace the object outright so stale handles on other
    # threads (rare in unit tests) are orphaned and GC'd rather than reused.
    global _thread_local
    for d in (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM):
        attr = f'db_{d}'
        old = getattr(_thread_local, attr, None)
        if old is not None:
            try:
                old.close()
            except Exception as e:
                logger.debug('[DB] reset_sqlite_for_tests: tl close failed: %s', e)
            setattr(_thread_local, attr, None)
    _thread_local = threading.local()

    init_db()
    logger.debug('[DB] reset_sqlite_for_tests: repointed to %s', db_path)
    return snapshot


def restore_db_state(snapshot: dict):
    """Undo a :func:`reset_sqlite_for_tests` repoint (test-only).

    Restores the backend/path globals captured in ``snapshot`` and drops
    every cached connection so the next access reconnects against the
    ORIGINAL binding. This is the symmetric teardown that prevents a
    test class which repointed the DB at a temp file from poisoning later
    tests that share the conftest session DB (the temp file is deleted
    when the class's ``TemporaryDirectory`` is cleaned up, so without this
    restore the next connect hits a vanished path → ``no such table``).

    Args:
        snapshot: The dict returned by :func:`reset_sqlite_for_tests`.
    """
    global _BACKEND, DB_PATH, db_available, pg_available, _thread_local
    if not snapshot:
        return

    # Drop any connections opened against the temp DB before swapping back.
    with _sqlite_pool_lock:
        while _sqlite_pool:
            conn = _sqlite_pool.pop()
            try:
                conn.close()
            except Exception as e:
                logger.debug('[DB] restore_db_state: pool close failed: %s', e)
    for d in (DOMAIN_CHAT, DOMAIN_TRADING, DOMAIN_SYSTEM):
        attr = f'db_{d}'
        old = getattr(_thread_local, attr, None)
        if old is not None:
            try:
                old.close()
            except Exception as e:
                logger.debug('[DB] restore_db_state: tl close failed: %s', e)
            setattr(_thread_local, attr, None)
    _thread_local = threading.local()

    _BACKEND = snapshot.get('_BACKEND', _BACKEND)
    DB_PATH = snapshot.get('DB_PATH', DB_PATH)
    db_available = snapshot.get('db_available', db_available)
    pg_available = snapshot.get('pg_available', pg_available)
    logger.debug('[DB] restore_db_state: restored backend=%s path=%s',
                 _BACKEND, DB_PATH)


# ═══════════════════════════════════════════════════════════════════════
#  Backend Detection & Auto-Start (runs on import)
# ═══════════════════════════════════════════════════════════════════════

# SQLite is the default; PostgreSQL is selected only by the exact canonical
# value.  Availability and historical markers never switch this decision.
_FORCE_SQLITE = _selected_backend() == 'sqlite'

db_available = False
pg_available = False
# Live cluster location: local disk when the FUSE-backup split engages, else
# the legacy <data>/pgdata (byte-identical on a vanilla local-disk box).
# See lib/database/db_paths.py + the JOURNAL durability-redesign entry.
from lib.database.db_paths import resolve_pgdata_dir  # noqa: E402
_PGDATA = resolve_pgdata_dir(_DB_DIR)


def _claim_sqlite_authority_if_needed():
    from lib.database.sqlite_owner import claim_owner, guard_required
    if guard_required(DB_PATH, _DEFAULT_DB_FILE):
        claim_owner(DB_PATH)


if _FORCE_SQLITE:
    _BACKEND = 'sqlite'
    db_available = True
    pg_available = False
    _claim_sqlite_authority_if_needed()
    logger.info('[DB] SQLite authority backend: %s '
                '(busy_timeout=%dms, pool_max=%d, wal_checkpoint_pages=%d)',
                DB_PATH, _BUSY_TIMEOUT_MS, _SQLITE_POOL_MAX,
                _SQLITE_WAL_AUTOCHECKPOINT_PAGES)
else:
    # Try PostgreSQL
    _pg_ok = False

    def _attempt_pg_bootstrap():
        """One full PG bootstrap attempt (probe + auto-start). Returns the
        result dict on success, else None. Retried by _retry_until below when
        PG is mandatory, so a transient re-exec race doesn't fail-close boot."""
        try:
            from lib.database._bootstrap import _ensure_pg_running as _boot_ensure
            return _boot_ensure(_PGDATA, BASE_DIR, PG_HOST, PG_PORT, PG_USER,
                                PG_PASSWORD, PG_DBNAME)
        except ImportError as e:
            logger.info('[DB] PG bootstrap unavailable (missing dependency: %s) '
                        '— selected backend will fail closed', e)
            return None
        except Exception as e:
            logger.warning('[DB] PG bootstrap attempt failed: %s', e)
            return None

    # On a self-triggered re-exec the app-owned postmaster is coming back within
    # seconds. When PostgreSQL is selected, wait for it with backoff
    # up to a real deadline before refusing — a single-shot probe that aborts on
    # the race is the bug. wait=0 when PG is optional → byte-identical single-box.
    _pg_wait_s = _pg_require_wait_s()
    if _pg_wait_s > 0:
        logger.info('[DB] PostgreSQL selected — waiting up to %.0fs for '
                    'PostgreSQL (tolerates a self-restart re-exec race) '
                    'before fail-closing.', _pg_wait_s)
    _pg_result = _retry_until(_attempt_pg_bootstrap, deadline_s=_pg_wait_s)
    if _pg_result:
        PG_HOST = _pg_result['PG_HOST']
        PG_PORT = _pg_result['PG_PORT']
        PG_DSN = _pg_result['PG_DSN']
        _pg_ok = True

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

            # Record the backend decision in the audit trail so an operator
            # can always answer "which DB is actually serving?" from audit.log
            # — the failover branch below logs the SQLite case symmetrically.
            try:
                from lib.log import audit_log
                audit_log('db_backend_selected', backend='pg',
                          host=PG_HOST, port=PG_PORT, dbname=PG_DBNAME)
            except Exception as _ae:
                logger.debug('[DB] audit_log for PG backend selection failed: %s', _ae)

        except ImportError:
            logger.warning('[DB] psycopg2 not installed — selected backend cannot start')
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
        # Local-primary cold-start hazard: the RESOLVED _PGDATA may be an empty
        # local dir that SHOULD have been seeded/restored from a recoverable
        # source on FUSE (a populated legacy pgdata or a logical dump). If such
        # a source exists but the resolved cluster came up empty, serving SQLite
        # would look like data loss just as much as the pgdata-exists case —
        # fire the same fail-loud guard.
        _recoverable_source = False
        try:
            from lib.database.db_paths import has_recoverable_source
            _recoverable_source = has_recoverable_source(_DB_DIR)
        except Exception as _re:
            logger.debug('[DB] recoverable-source probe failed: %s', _re)
        if _pgdata_exists or _recoverable_source:
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
            # Data history makes the fallback unsafe regardless of a flag.  A
            # warning followed by serving a month-old SQLite file is still a
            # split-brain write path; fail closed before any schema/write code
            # can touch that file.
            _assert_sqlite_fallback_has_no_pg_history(True, _PGDATA)

        # ── Epic D1: fail-CLOSED PG guarantee for scaled deployments ──
        # Delegates to the testable _assert_pg_available_or_raise helper: when
        # PostgreSQL selection is unavailable: raise instead of switching.
        _assert_pg_available_or_raise(False, _PGDATA)


# Register teardown only after backend detection imported the SQLite owner
# module. ``atexit`` is LIFO: drain/close DB connections first, then remove our
# ownership member. Otherwise a peer host could take over while this process
# still had writable connections alive during interpreter shutdown.
if _BACKEND == 'sqlite':
    from lib.database.sqlite_owner import release_owner as _release_sqlite_owner
    atexit.unregister(_release_sqlite_owner)
    atexit.register(_release_sqlite_owner)
atexit.register(shutdown_pool)


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
