"""Database schema initialization — SQLite backend: meta helpers.

Schema-version cache, column/table existence probes, and the core-owned
``schema_meta`` key/value read/write used by the fast-startup version cache.
Native SQLite DDL — no translation layer needed.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Version Cache — Skip redundant DDL on subsequent startups
# ═══════════════════════════════════════════════════════════════════════

_SCHEMA_VERSION = 43  # Increment when tables/columns/indexes change


def _column_exists(conn, table, column):
    """Check if a column exists in a SQLite table."""
    cur = conn._conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    return column in columns


def _table_exists(conn, table):
    """Check if a table exists in SQLite."""
    cur = conn._conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
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
        if not _table_exists(conn, 'schema_meta'):
            return None
        cur = conn._conn.cursor()
        cur.execute("SELECT value FROM schema_meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug('[DB] Could not read schema_meta[%s] (expected on first run): %s', key, e)
        return None


def _write_meta(conn, key, value):
    """Write a key/value into the core-owned ``schema_meta`` table."""
    conn._conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn._conn.commit()


def _get_schema_version(conn):
    """Read current schema version from DB."""
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


def _get_schema_domains(conn):
    """Read the persisted optional-domain set (comma-joined), or None if unset."""
    return _read_meta(conn, '_schema_domains')


def _set_schema_domains(conn, domains):
    """Persist the active optional-domain set as a comma-joined string."""
    try:
        _write_meta(conn, '_schema_domains', ','.join(domains))
    except Exception as e:
        logger.warning('[DB] Failed to write schema domains: %s', e)
