"""PostgreSQL wrapper classes — DictRow, PgCursor, PgConnection.

Provides dict-like Row access and SQL compatibility translation.
Extracted from _core.py for modularity. Re-exported via _core for backward compat.
"""

import os
import re
import time

from lib.database._sql_translate import translate_sql
from lib.log import get_logger

logger = get_logger(__name__)


_LOCKING_SELECT_RE = re.compile(
    r'\bFOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b', re.I)
_CTE_WRITE_RE = re.compile(r'\b(?:INSERT|UPDATE|DELETE|MERGE)\b', re.I)


def _statement_effect(sql):
    """Classify one translated statement for transaction hygiene.

    psycopg2 starts a transaction for *every* statement when autocommit is
    disabled, including a plain SELECT.  Most Tofu background readers keep a
    thread-local connection, so a read that is not explicitly finished parks
    the backend ``idle in transaction`` until PostgreSQL kills it.  We need a
    conservative classifier to distinguish disposable reads from writes and
    lock-bearing/explicit transactions.

    Returns ``read``, ``write``, ``pin`` (transaction/lock must remain open),
    or ``end`` (COMMIT/ROLLBACK transaction control).  Unknown statements are
    treated as writes: retaining a transaction is cheaper than accidentally
    committing or rolling back an operation with side effects.
    """
    text = (sql or '').lstrip()
    # Strip leading comments so instrumentation comments do not defeat the
    # classifier.  This is intentionally small; it is not a SQL parser.
    while True:
        if text.startswith('--'):
            nl = text.find('\n')
            text = '' if nl < 0 else text[nl + 1:].lstrip()
            continue
        if text.startswith('/*'):
            end = text.find('*/', 2)
            text = '' if end < 0 else text[end + 2:].lstrip()
            continue
        break
    upper = text.upper()
    keyword = upper.split(None, 1)[0] if upper else ''

    if keyword in ('BEGIN', 'START', 'SAVEPOINT', 'LOCK'):
        return 'pin'
    if keyword in ('COMMIT', 'END'):
        return 'end'
    if keyword == 'ROLLBACK':
        # ROLLBACK TO SAVEPOINT keeps the outer transaction alive.
        return 'pin' if upper.startswith('ROLLBACK TO') else 'end'
    if keyword == 'RELEASE':
        return 'pin'

    if keyword == 'SELECT':
        return 'pin' if _LOCKING_SELECT_RE.search(upper) else 'read'
    if keyword in ('SHOW', 'VALUES', 'TABLE'):
        return 'read'
    if keyword == 'EXPLAIN':
        # EXPLAIN ANALYZE actually executes its DML target.
        if 'ANALYZE' in upper and _CTE_WRITE_RE.search(upper):
            return 'write'
        return 'read'
    if keyword == 'WITH':
        return 'write' if _CTE_WRITE_RE.search(upper) else 'read'
    return 'write'


# ═══════════════════════════════════════════════════════════════════════
#  DictRow — dict-like row wrapper for psycopg2 rows
# ═══════════════════════════════════════════════════════════════════════

class DictRow:
    """A row wrapper that supports both dict-like (row['col']) and index access (row[0]).

    Provides dict-like and index-based row access.
    Stores both a dict (for key access) and a tuple (for fast int-index access).
    """
    __slots__ = ('_data', '_keys', '_values')

    def __init__(self, cursor, values):
        self._keys = [desc[0] for desc in cursor.description]
        self._values = tuple(values)  # O(1) int-index access
        self._data = dict(zip(self._keys, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]  # O(1) instead of O(n) list() call
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


# ═══════════════════════════════════════════════════════════════════════
#  String Sanitization for PostgreSQL
# ═══════════════════════════════════════════════════════════════════════

# PostgreSQL rejects null bytes (\x00 / \u0000) in TEXT/JSONB columns.
# Also rejects lone surrogate Unicode escapes (\uD800-\uDFFF) in JSONB.
_RE_SURROGATE = re.compile(r'[\ud800-\udfff]')


def _sanitize_pg_param(val):
    """Strip null bytes and surrogate code points from a string value.

    Handles literal ``\\x00`` bytes and lone surrogates in all TEXT/JSONB
    parameters.  For JSONB-bound JSON text, callers should use
    :func:`json_dumps_pg` which additionally strips ``\\u0000`` escape
    sequences that PostgreSQL's JSONB parser rejects.
    """
    if not isinstance(val, str):
        return val
    val = val.replace('\x00', '')
    val = _RE_SURROGATE.sub('', val)
    return val


def strip_null_bytes_deep(obj):
    """Recursively strip \\x00 null bytes from all strings in a data structure.

    Use this on raw Python objects (message lists, dicts) **before**
    ``json.dumps()`` so that the resulting JSON text stays valid.

    >>> strip_null_bytes_deep({'a': 'hello\\x00world'})
    {'a': 'helloworld'}
    """
    if isinstance(obj, str):
        return obj.replace('\x00', '')
    if isinstance(obj, dict):
        return {k: strip_null_bytes_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_null_bytes_deep(v) for v in obj]
    return obj


def _strip_json_null_escapes(json_text):
    r"""Remove ``\u0000`` escape sequences from JSON text for PostgreSQL JSONB.

    PostgreSQL rejects ``\u0000`` in JSONB columns even though it is valid
    JSON per RFC 8259.  ``json.dumps()`` encodes Python null bytes (``\x00``)
    as ``\u0000`` in the output text.

    Strategy: temporarily replace ``\\`` pairs with a placeholder,
    strip lone ``\u0000``, then restore.
    """
    if '\\u0000' not in json_text:
        return json_text
    _PH = '\x01\x01'
    tmp = json_text.replace('\\\\', _PH)
    tmp = tmp.replace('\\u0000', '')
    return tmp.replace(_PH, '\\\\')


def json_dumps_pg(obj, **kwargs):
    """Serialize *obj* to a JSON string safe for PostgreSQL JSONB columns.

    Equivalent to::

        json.dumps(strip_null_bytes_deep(obj), ensure_ascii=False, **kwargs)

    plus a post-pass that strips any residual ``\\u0000`` escape sequences
    that PostgreSQL's JSONB parser rejects.

    Fast path: ``json.dumps`` escapes every U+0000 to the six-char sequence
    ``\\u0000``, so its absence from the serialised text PROVES the payload
    carries no null bytes and the recursive ``strip_null_bytes_deep`` walk
    would be a no-op. Skipping that walk matters: it is pure-Python,
    GIL-holding CPU (~0.9 s for a 93.7 MB conversation blob, measured), and
    a 2026-07-27 hard-refresh burst parked 149 writer threads inside it
    simultaneously, starving the event loop for 8.8 s. Only payloads that
    actually contain null bytes pay the deep strip, which keeps the output
    byte-identical to the historical always-strip behaviour.
    """
    import json as _json
    kwargs.setdefault('ensure_ascii', False)
    text = _json.dumps(obj, **kwargs)
    if '\\u0000' not in text:
        return text
    text = _json.dumps(strip_null_bytes_deep(obj), **kwargs)
    text = _strip_json_null_escapes(text)
    if os.environ.get('DEBUG_JSON_VALIDATE'):
        try:
            _json.loads(text)
        except (ValueError, TypeError) as e:
            logger.error('[json_dumps_pg] Output is not valid JSON (%s), re-serializing with ensure_ascii=True', e)
            kwargs['ensure_ascii'] = True
            text = _json.dumps(strip_null_bytes_deep(obj), **kwargs)
            text = _strip_json_null_escapes(text)
    return text


def _sanitize_params(params):
    """Sanitize all string values in a params tuple/list for PostgreSQL."""
    if params is None:
        return None
    if isinstance(params, (list, tuple)):
        sanitized = type(params)(_sanitize_pg_param(p) for p in params)
        return sanitized
    if isinstance(params, dict):
        return {k: _sanitize_pg_param(v) for k, v in params.items()}
    return params


# ═══════════════════════════════════════════════════════════════════════
#  PgCursor — wraps psycopg2 cursor with SQL translation
# ═══════════════════════════════════════════════════════════════════════

class PgCursor:
    """Wraps a psycopg2 cursor to translate SQL and return DictRow objects."""

    def __init__(self, real_cursor, conn):
        self._cursor = real_cursor
        self._conn = conn
        self.description = None
        self.rowcount = 0
        self._skipped = False  # True if PRAGMA was skipped

    def execute(self, sql, params=None):
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        translated, is_pragma = translate_sql(sql)
        if is_pragma:
            self._skipped = True
            return self
        params = _sanitize_params(params)
        _t0 = time.monotonic()
        try:
            if params:
                self._cursor.execute(translated, params)
            else:
                self._cursor.execute(translated)
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            self._conn._last_used_wall = time.time()
            effect = _statement_effect(translated)
            if effect == 'write':
                self._conn._dirty = True
            elif effect == 'pin':
                self._conn._transaction_pinned = True
            elif effect == 'end':
                self._conn._dirty = False
                self._conn._transaction_pinned = False
            elif effect == 'read':
                # psycopg2's ordinary (unnamed) cursor has already buffered the
                # result when execute() returns, so rollback does not invalidate
                # fetchone/fetchall.  End a clean implicit read transaction
                # immediately; explicit transactions and SELECT ... FOR UPDATE
                # are pinned and therefore deliberately excluded.
                self._conn._finish_clean_read()
        except Exception as e:
            # A SQL execution failure is a genuine, often data-affecting error.
            # Log at ERROR so it reaches error.log; the full SQL/params stay at
            # DEBUG (enable via TOFU_DB_LOG_LEVEL=DEBUG) to avoid leaking values.
            # A caller running an expected-to-fail probe (see
            # suppress_sql_error_log in _core) demotes this to DEBUG.
            from lib.database._core import (
                _sql_errors_suppressed,
                _sql_error_is_expected_shutdown,
            )
            if _sql_error_is_expected_shutdown(str(e)):
                # Expected race: the postmaster is stopping while a background
                # finalize/daemon write is still in flight. One concise line,
                # NO traceback — the caller still handles the raised exception.
                logger.info('[DB] query aborted during shutdown — expected (%s)',
                            type(e).__name__)
            elif _sql_errors_suppressed():
                logger.debug('[DB] SQL execution failed (%s) [suppressed]: %.120s', type(e).__name__, e)
            else:
                logger.error('[DB] SQL execution failed (%s): %.120s', type(e).__name__, e)
                logger.debug('[DB] SQL error detail: %s\n  Original: %.200s\n  Translated: %.200s\n  Params: %.200s',
                             e, sql, translated, str(params)[:200] if params else 'None')
            try:
                self._conn.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Rollback after SQL error also failed: %s', _rb_err)
            raise
        from lib.database._core import _SLOW_QUERY_MS
        if _SLOW_QUERY_MS:
            _elapsed_ms = (time.monotonic() - _t0) * 1000
            if _elapsed_ms >= _SLOW_QUERY_MS:
                logger.warning('[DB] Slow query %.0fms (threshold %dms): %.150s',
                               _elapsed_ms, _SLOW_QUERY_MS, translated)
        return self

    def executemany(self, sql, params_list):
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        translated, is_pragma = translate_sql(sql)
        if is_pragma:
            return self
        try:
            self._cursor.executemany(translated, params_list)
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            self._conn._last_used_wall = time.time()
            # Mark the connection dirty so teardown COMMITs instead of
            # rolling back — otherwise executemany-only writes are silently
            # discarded by close_db()'s clean-reads rollback branch.
            # executemany is a mutation API in this codebase.  Classifying it
            # conservatively also protects uncommon CTE/DDL batches.
            self._conn._dirty = True
        except Exception as e:
            logger.error('[DB] SQL executemany failed (%s): %.120s', type(e).__name__, e)
            logger.debug('[DB] executemany error detail: %s\n  Translated: %.200s', e, translated)
            try:
                self._conn.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Rollback after executemany error also failed: %s', _rb_err)
            raise
        return self

    def fetchone(self):
        if self._skipped:
            return None
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._cursor.description:
            return DictRow(self._cursor, row)
        return row

    def fetchall(self):
        if self._skipped:
            return []
        rows = self._cursor.fetchall()
        if not rows or not self._cursor.description:
            return rows
        return [DictRow(self._cursor, r) for r in rows]

    def __iter__(self):
        """Support iteration over cursor results."""
        if self._skipped:
            return
        while True:
            row = self._cursor.fetchone()
            if row is None:
                break
            if self._cursor.description:
                yield DictRow(self._cursor, row)
            else:
                yield row

    def close(self):
        self._cursor.close()


# ═══════════════════════════════════════════════════════════════════════
#  PgConnection — PostgreSQL connection wrapper
# ═══════════════════════════════════════════════════════════════════════

class PgConnection:
    """PostgreSQL connection wrapper with SQL compatibility.

    Translates legacy SQL syntax to PostgreSQL and provides a familiar API:
      conn.execute(sql, params)
      conn.commit()
      conn.rollback()
      conn.close()
      conn.row_factory  (ignored — always returns DictRow)
      conn.executescript(sql)
    """

    def __init__(self, pg_conn):
        self._conn = pg_conn
        self._closed = False
        self._dirty = False
        self._transaction_pinned = False
        self._created_at = time.monotonic()
        self._last_used = time.monotonic()
        # Wall-clock twin of _last_used. time.monotonic() FREEZES across an OS
        # suspend (laptop lid close / VM pause / FUSE stall) while time.time()
        # keeps advancing, so the health check needs the wall clock to notice a
        # connection went stale during a suspend (see _test_pg_connection).
        self._last_used_wall = time.time()
        self.row_factory = None

    def _finish_clean_read(self):
        """End a disposable implicit read transaction.

        A write, explicit transaction, or locking SELECT owns its transaction
        boundary and must be ended by the caller.  A plain read owns no durable
        state, so returning the backend to IDLE here prevents long-lived reader
        threads from accumulating ``idle in transaction`` sessions.
        """
        if self._dirty or getattr(self, '_transaction_pinned', False):
            return
        self._conn.rollback()

    def begin(self):
        """Begin and pin an explicit transaction until commit/rollback."""
        self._transaction_pinned = True
        try:
            return self.execute('BEGIN')
        except Exception:
            self._transaction_pinned = False
            raise

    @property
    def raw(self):
        """Access the underlying psycopg2 connection for special operations."""
        return self._conn

    @property
    def dialect(self):
        """Stable data-layer capability for backend-specific planner hints."""
        return 'postgres'

    def execute(self, sql, params=None):
        # PgCursor rolls the failed raw transaction back before re-raising,
        # which intentionally clears wrapper state.  Capture the pre-statement
        # ownership here so reconnect policy cannot mistake a formerly-dirty or
        # explicit transaction for a clean first statement after that cleanup.
        had_transaction_state = (
            self._dirty or getattr(self, '_transaction_pinned', False))
        cur = self._conn.cursor()
        wrapper = PgCursor(cur, self)
        try:
            return wrapper.execute(sql, params)
        except Exception as e:
            # Transparent one-shot reconnect on a dead-connection error, but
            # ONLY when this connection has issued no write yet in the current
            # transaction (self._dirty is False). A connection-level failure
            # means nothing was committed, so re-running a clean read / the
            # first statement is idempotent. This rescues the raw db.execute()
            # read sites (e.g. persist_conv_messages' settings SELECT) that do
            # NOT route through db_execute_with_retry, so a pooled connection
            # killed by a suspend / PG restart / network flap becomes a
            # transparent reconnect instead of a hard 500 on the user's next
            # Send/Regen. A statement_timeout cancellation (OperationalError
            # that is NOT a dead-connection signature) is deliberately NOT
            # retried — only genuine connection death is.
            etype = type(e).__name__
            if (etype in ('OperationalError', 'InterfaceError')
                    and not had_transaction_state):
                from lib.database._core import _pg_error_is_dead, _reconnect_pg_inplace
                is_dead = (etype == 'InterfaceError') or _pg_error_is_dead(str(e))
                if is_dead and _reconnect_pg_inplace(self):
                    logger.warning('[DB] Transparent reconnect after dead connection '
                                   '(%s) — retrying statement once', etype)
                    cur = self._conn.cursor()
                    wrapper = PgCursor(cur, self)
                    return wrapper.execute(sql, params)
            raise

    def executemany(self, sql, params_list):
        cur = self._conn.cursor()
        wrapper = PgCursor(cur, self)
        return wrapper.executemany(sql, params_list)

    def executescript(self, sql):
        """Execute multiple SQL statements."""
        from lib.database._access_policy import enforce_sql_access
        enforce_sql_access(sql)
        cur = self._conn.cursor()
        statements = _split_sql_statements(sql)
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            translated, is_pragma = translate_sql(stmt)
            if is_pragma:
                continue
            try:
                cur.execute(translated)
            except Exception as e:
                logger.error('[DB] executescript statement failed (%s): %.120s', type(e).__name__, e)
                logger.debug('[DB] executescript statement detail: %.300s', translated)
                raise
        self.commit()

    def commit(self):
        self._conn.commit()
        self._dirty = False
        self._transaction_pinned = False
        try:
            from lib.database import _core as _core_mod
            _core_mod._COMMIT_TOTAL += 1
        except Exception as e:
            logger.debug('[DB] commit counter increment failed: %s', e)

    def rollback(self):
        self._conn.rollback()
        self._dirty = False
        self._transaction_pinned = False

    def close(self):
        if not self._closed:
            self._closed = True
            self._dirty = False
            self._transaction_pinned = False
            try:
                self._conn.close()
            except Exception as _close_err:
                logger.debug('[DB] Error closing PG connection: %s', _close_err)
            # Release the connection semaphore slot (if governed)
            sem = getattr(self, '_semaphore', None)
            if sem is not None:
                try:
                    sem.release()
                except ValueError:
                    logger.debug('[DB] Semaphore already released (double-close)')  # defensive
                self._semaphore = None
                # Decrement global count
                from lib.database._core import _conn_count_lock
                import lib.database._core as _core_mod
                with _conn_count_lock:
                    _core_mod._conn_count = max(0, _core_mod._conn_count - 1)

    def cursor(self):
        cur = self._conn.cursor()
        return PgCursor(cur, self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _split_sql_statements(sql):
    """Split SQL text into individual statements, respecting string literals.

    A literal single quote inside a string is escaped by doubling it (``''``)
    in both SQLite and PostgreSQL. Treat ``''`` as an escaped quote (consume
    both, stay in-string) rather than two delimiters, otherwise a ``;`` inside
    a literal like ``'it''s; ok'`` would wrongly split the statement and
    corrupt the DDL/DML.
    """
    statements = []
    current = []
    in_string = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            if in_string and i + 1 < n and sql[i + 1] == "'":
                # Escaped quote inside a literal — consume both, stay in-string.
                current.append("''")
                i += 2
                continue
            in_string = not in_string
            current.append(ch)
        elif ch == ';' and not in_string:
            statements.append(''.join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    remaining = ''.join(current).strip()
    if remaining:
        statements.append(remaining)
    return statements
