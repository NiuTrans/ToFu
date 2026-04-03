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
    """
    import json as _json
    kwargs.setdefault('ensure_ascii', False)
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
        translated, is_pragma = translate_sql(sql)
        if is_pragma:
            self._skipped = True
            return self
        params = _sanitize_params(params)
        try:
            if params:
                self._cursor.execute(translated, params)
            else:
                self._cursor.execute(translated)
            self.description = self._cursor.description
            self.rowcount = self._cursor.rowcount
            self._conn._last_used = time.monotonic()
            _sql_upper = translated[:30].lstrip().upper()
            if _sql_upper.startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP')):
                self._conn._dirty = True
        except Exception as e:
            logger.debug('[DB] SQL error: %s\n  Original: %.200s\n  Translated: %.200s\n  Params: %.200s',
                         e, sql, translated, str(params)[:200] if params else 'None')
            try:
                self._conn._conn.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Rollback after SQL error also failed: %s', _rb_err)
            raise
        return self

    def executemany(self, sql, params_list):
        translated, is_pragma = translate_sql(sql)
        if is_pragma:
            return self
        self._cursor.executemany(translated, params_list)
        self.description = self._cursor.description
        self.rowcount = self._cursor.rowcount
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
        self._created_at = time.monotonic()
        self._last_used = time.monotonic()
        self.row_factory = None

    @property
    def raw(self):
        """Access the underlying psycopg2 connection for special operations."""
        return self._conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        wrapper = PgCursor(cur, self)
        return wrapper.execute(sql, params)

    def executemany(self, sql, params_list):
        cur = self._conn.cursor()
        wrapper = PgCursor(cur, self)
        return wrapper.executemany(sql, params_list)

    def executescript(self, sql):
        """Execute multiple SQL statements."""
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
                logger.debug('[DB] executescript statement error: %s\n  Statement: %.300s',
                             e, translated)
                raise
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except Exception as _close_err:
                logger.debug('[DB] Error closing PG connection: %s', _close_err)

    def cursor(self):
        cur = self._conn.cursor()
        return PgCursor(cur, self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _split_sql_statements(sql):
    """Split SQL text into individual statements, respecting string literals."""
    statements = []
    current = []
    in_string = False
    for ch in sql:
        if ch == "'" and not in_string:
            in_string = True
            current.append(ch)
        elif ch == "'" and in_string:
            in_string = False
            current.append(ch)
        elif ch == ';' and not in_string:
            statements.append(''.join(current))
            current = []
        else:
            current.append(ch)
    remaining = ''.join(current).strip()
    if remaining:
        statements.append(remaining)
    return statements
