"""Schema meta helpers — PostgreSQL backend.

Existence probes (column/table/row-count), the ``schema_meta`` key/value
read/write, and the version + optional-domain cache accessors. The
``_SCHEMA_VERSION`` constant is single-homed here and re-exported from the
package facade (``lib.database._schema_pg``); it is consumed by name.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Version Cache — Skip redundant DDL on subsequent startups
# ═══════════════════════════════════════════════════════════════════════

_SCHEMA_VERSION = 45  # Increment when tables/columns/indexes change


def _column_exists(conn, table, column):
    """Check if a column exists in a PostgreSQL table."""
    cur = conn._conn.cursor()
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return cur.fetchone() is not None


def _table_exists(conn, table):
    """Check if a table exists in the PostgreSQL public schema."""
    cur = conn._conn.cursor()
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    """, (table,))
    return cur.fetchone() is not None


def _count_rows(conn, table):
    """Return the row count of a table (orphan-heal row guard)."""
    cur = conn._conn.cursor()
    cur.execute(f'SELECT count(*) FROM {table}')
    return int(cur.fetchone()[0])


def _read_meta(conn, key):
    """Read a value from the core-owned ``schema_meta`` table.

    Returns the string value, or None if the table or key is absent.

    The version cache lived in ``trading_config`` historically; it moved to the
    core-owned ``schema_meta`` table (schema v22) so the fast-startup cache
    survives when the trading domain is disabled or extracted.
    """
    try:
        cur = conn._conn.cursor()
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'schema_meta'
        """)
        if not cur.fetchone():
            conn._conn.rollback()
            return None
        cur.execute("SELECT value FROM schema_meta WHERE key = %s", (key,))
        row = cur.fetchone()
        conn._conn.rollback()
        return row[0] if row else None
    except Exception as e:
        logger.debug('[DB] Could not read schema_meta[%s] (expected on first run): %s', key, e)
        try:
            conn._conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after schema_meta read failed: %s', _rb_err)
        return None


def _write_meta(conn, key, value):
    """Write a key/value into the core-owned ``schema_meta`` table."""
    cur = conn._conn.cursor()
    cur.execute("""
        INSERT INTO schema_meta (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))
    conn._conn.commit()


def _get_schema_version(conn):
    """Read current schema version from DB.

    Returns:
        int version if found, None if table doesn't exist or key not set.
    """
    val = _read_meta(conn, '_schema_version')
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError) as e:
        logger.debug('[DB] Non-integer schema version %r: %s', val, e)
        return None


def _set_schema_version(conn, version):
    """Write schema version to DB after successful DDL."""
    try:
        _write_meta(conn, '_schema_version', version)
        logger.info('[DB] Schema version updated to %d', version)
    except Exception as e:
        logger.warning('[DB] Failed to write schema version: %s', e)
        try:
            conn._conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after schema version write failed: %s', _rb_err)


def _get_schema_domains(conn):
    """Read the persisted optional-domain set (comma-joined), or '' if unset."""
    val = _read_meta(conn, '_schema_domains')
    return val if val is not None else None


def _set_schema_domains(conn, domains):
    """Persist the active optional-domain set as a comma-joined string."""
    try:
        _write_meta(conn, '_schema_domains', ','.join(domains))
    except Exception as e:
        logger.warning('[DB] Failed to write schema domains: %s', e)
        try:
            conn._conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after schema domains write failed: %s', _rb_err)
