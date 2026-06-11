"""lib/database/aio.py — Async facade over the synchronous dual-backend DB layer.

WHY THIS EXISTS
---------------
The native-async migration (Stage 2) needs an ``await``-able database API so
that ``async def`` route handlers and the orchestrator can talk to the DB
without blocking the event loop. The underlying drivers are still synchronous
(``psycopg2`` for PG, ``sqlite3`` for SQLite — no ``asyncpg``/``aiosqlite``
installed), so this facade runs each operation in a **dedicated, bounded
thread executor** and ``await``s the future.

LEAK SAFETY (critical — see the thread-local-db-conn-leak history)
------------------------------------------------------------------
We deliberately do **NOT** use ``asyncio.to_thread`` (the shared default
executor) nor ``get_thread_db`` (thread-local connections). Both would pin one
connection per long-lived pool thread for its entire life and re-create the
connection-pool-exhaustion leak that ``close_thread_db`` was added to fix.

Instead every call here follows a strict checkout→use→return cycle:

    conn = _pool_get()          # borrow from the shared pool
    try:
        ... run the operation on the dedicated executor ...
    finally:
        _pool_put(conn)         # ALWAYS return it to the shared pool

So a connection is held only for the duration of one operation/transaction,
never tied to a thread. The dedicated executor is bounded to the same size as
the connection pool so we can never request more connections than the pool /
semaphore allows.

SWAP-OUT PATH
-------------
Callers depend only on the ``async_*`` coroutine API below. The internals
(thread-offload of psycopg2/sqlite3) can later be replaced with a genuinely
async driver (psycopg3-async, which keeps the same ``%s`` paramstyle and so
reuses ``_sql_translate``) without touching a single caller.

PUBLIC API
----------
    rows  = await async_fetchall(sql, params, domain=...)
    row   = await async_fetchone(sql, params, domain=...)
    n     = await async_execute(sql, params, domain=..., commit=True)
    await async_executescript(script, domain=...)
    async with async_transaction(domain=...) as conn:
        conn.execute(...); conn.execute(...)   # committed on clean exit
"""

import asyncio
import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from lib.database._core import (
    DOMAIN_CHAT,
    _CONN_POOL_MAX,
    _MAX_TOTAL_CONNS,
    _pool_get,
    _pool_put,
)
from lib.log import get_logger

logger = get_logger(__name__)


# ── Dedicated bounded executor ──────────────────────────────────────────
# Each in-flight async DB op holds one checked-out connection for its whole
# duration, so the executor size caps the EXTRA concurrent connection demand
# this facade adds on top of the request path + background tasks — all of which
# share the same global ``_conn_semaphore`` (= _MAX_TOTAL_CONNS). Sizing it at
# the full pool max (~100) on a PG whose server-side max_connections may be far
# lower (and is shared) risks amplifying connection pressure, so it is
# env-tunable and defaults conservatively to min(_CONN_POOL_MAX, 25% of the
# total connection budget) — never larger than the budget itself.
def _default_executor_workers() -> int:
    env = os.environ.get('TOFU_DB_AIO_WORKERS')
    if env:
        try:
            n = int(env)
            if n >= 1:
                return min(n, _MAX_TOTAL_CONNS)
        except (ValueError, TypeError) as e:
            logger.debug('[DB.aio] Bad TOFU_DB_AIO_WORKERS=%r: %s', env, e)
    budget_quarter = max(4, _MAX_TOTAL_CONNS // 4)
    return max(4, min(_CONN_POOL_MAX, budget_quarter))


_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = _default_executor_workers()
                _executor = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix='db-aio')
                logger.info('[DB.aio] Async DB executor started (max_workers=%d)', workers)
    return _executor


def shutdown_async_db():
    """Shut down the async DB executor (called at process exit)."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None
            logger.info('[DB.aio] Async DB executor shut down')


atexit.register(shutdown_async_db)


async def _run(fn):
    """Run a blocking ``fn()`` on the dedicated DB executor and await it."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_get_executor(), fn)


# ── Core operations ─────────────────────────────────────────────────────

async def async_fetchall(sql, params=None, *, domain=DOMAIN_CHAT):
    """Run a query and return all rows as a list of DictRow (PG) / Row (SQLite)."""
    def _op():
        conn = _pool_get()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        finally:
            _pool_put(conn)
    return await _run(_op)


async def async_fetchone(sql, params=None, *, domain=DOMAIN_CHAT):
    """Run a query and return the first row, or None."""
    def _op():
        conn = _pool_get()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchone()
        finally:
            _pool_put(conn)
    return await _run(_op)


async def async_execute(sql, params=None, *, domain=DOMAIN_CHAT, commit=True):
    """Run a write statement. Commits by default. Returns affected rowcount."""
    def _op():
        conn = _pool_get()
        try:
            cur = conn.execute(sql, params)
            if commit:
                conn.commit()
            return cur.rowcount
        except Exception:
            try:
                conn.rollback()
            except Exception as rb:
                logger.debug('[DB.aio] rollback after execute failure failed: %s', rb)
            raise
        finally:
            _pool_put(conn)
    return await _run(_op)


async def async_executescript(script, *, domain=DOMAIN_CHAT):
    """Execute a multi-statement SQL script (DDL/migrations)."""
    def _op():
        conn = _pool_get()
        try:
            conn.executescript(script)
        finally:
            _pool_put(conn)
    return await _run(_op)


async def run_pooled(fn, *, domain=DOMAIN_CHAT):
    """Run a blocking ``fn(db)`` off-loop with a borrowed pooled connection.

    The escape hatch for handlers whose DB logic can't be expressed as
    individual ``async_*`` calls — e.g. multi-statement bodies that thread a
    sync ``db`` through helpers like ``db_execute_with_retry(db, ...)`` /
    ``update_conversation_fts(db, ...)``. ``fn`` receives a live pooled
    connection, runs on the dedicated DB executor, and the connection is
    ALWAYS returned to the pool afterwards (leak-safe, same contract as the
    other facade functions). ``fn``'s return value is passed straight back.

    APP/REQUEST CONTEXT: ``fn`` runs in an executor thread where Quart's
    app/request context is NOT present (and Quart's AppContext is async-only, so
    it can't be pushed from a sync thread). Therefore ``fn`` MUST NOT build a
    Quart Response (no ``jsonify`` / ``api_ok`` / ``api_*``) — it must return
    PLAIN DATA (dict / list / ``Resp`` tuple / sentinel) and the async caller
    builds the Response on the loop thread. Read request data BEFORE calling
    run_pooled and pass it in.

    Usage::

        data = await run_pooled(lambda db: _save_conv_blocking(db, conv_id, data))
        # ... then jsonify(data) on the loop thread.
    """
    def _op():
        conn = _pool_get()
        try:
            return fn(conn)
        finally:
            _pool_put(conn)
    return await _run(_op)


@asynccontextmanager
async def async_transaction(*, domain=DOMAIN_CHAT):
    """Async transaction context — commits on clean exit, rolls back on error.

    The borrowed connection is passed to the body. All statements on it run on
    the SAME thread, then it is committed and returned to the pool. Use for
    multi-statement atomic writes::

        async with async_transaction() as conn:
            await conn.execute('INSERT ...', a)
            await conn.execute('UPDATE ...', b)

    THREAD AFFINITY (critical): a psycopg2 connection is NOT safe to use from
    multiple threads. The shared ``_get_executor`` pool would scatter the
    transaction's statements across different worker threads (same connection,
    different threads → libpq protocol corruption). So a transaction gets its
    OWN single-worker executor; every statement, the commit/rollback, and the
    pool checkout/return all run on that one thread. The connection is still
    bounded by the global ``_conn_semaphore`` (acquired inside ``_pool_get`` →
    ``_new_connection``), so this adds at most one extra in-flight connection
    per concurrent transaction, never an unbounded path.
    """
    loop = asyncio.get_event_loop()
    tx_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='db-aio-tx')

    async def _on_tx_thread(fn):
        return await loop.run_in_executor(tx_executor, fn)

    conn = await _on_tx_thread(_pool_get)

    class _Tx:
        """Wraps the borrowed conn so each statement runs on the SAME thread."""
        async def execute(self, sql, params=None):
            return await _on_tx_thread(lambda: conn.execute(sql, params))

        async def fetchall(self, sql, params=None):
            return await _on_tx_thread(lambda: conn.execute(sql, params).fetchall())

        async def fetchone(self, sql, params=None):
            return await _on_tx_thread(lambda: conn.execute(sql, params).fetchone())

    try:
        yield _Tx()
        await _on_tx_thread(conn.commit)
    except Exception:
        await _on_tx_thread(conn.rollback)
        raise
    finally:
        await _on_tx_thread(lambda: _pool_put(conn))
        tx_executor.shutdown(wait=False)
