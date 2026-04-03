"""SQL compatibility layer — translates legacy SQL syntax to PostgreSQL.

Handles ? → %s placeholders, INSERT OR REPLACE → ON CONFLICT, PRAGMA no-ops, etc.
Extracted from _core.py for modularity. Re-exported via _core for backward compat.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Pre-compiled regex patterns for SQL translation
# ═══════════════════════════════════════════════════════════════════════

_RE_INSERT_OR_REPLACE = re.compile(
    r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)',
    re.IGNORECASE
)
_RE_INSERT_OR_IGNORE = re.compile(
    r'INSERT\s+OR\s+IGNORE\s+INTO',
    re.IGNORECASE
)
_RE_PRAGMA = re.compile(r'^\s*PRAGMA\s+', re.IGNORECASE)
_RE_JSON_ARRAY_LENGTH = re.compile(r'\bjson_array_length\b', re.IGNORECASE)
_RE_STRFTIME_EPOCH = re.compile(
    r"strftime\s*\(\s*'%s'\s*,\s*'now'\s*\)",
    re.IGNORECASE
)
_RE_STRFTIME_EPOCH_MS = re.compile(
    r"strftime\s*\(\s*'%s'\s*,\s*'now'\s*\)\s*\*\s*1000",
    re.IGNORECASE
)
_RE_DATETIME_NOW = re.compile(
    r"datetime\s*\(\s*'now'\s*\)",
    re.IGNORECASE
)
_RE_CHANGES = re.compile(
    r"SELECT\s+changes\s*\(\s*\)",
    re.IGNORECASE
)
_RE_JSON_EXTRACT = re.compile(
    r"json_extract\s*\(\s*(\w+)\s*,\s*'\$\.(\w+)'\s*\)",
    re.IGNORECASE
)
_RE_PRAGMA_TABLE_INFO = re.compile(
    r"PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)",
    re.IGNORECASE
)


def _get_pk_columns(table_name):
    """Return known primary key columns for INSERT OR REPLACE translation.

    PostgreSQL needs explicit ON CONFLICT (pk_columns) DO UPDATE SET ...
    to emulate INSERT OR REPLACE behavior.
    """
    _PK_MAP = {
        'conversations':              ['id', 'user_id'],
        'task_results':               ['task_id'],
        'users':                      ['id'],
        'pricing_cache':              ['key'],
        'recent_projects':            ['path'],
        'trading_price_cache':        ['symbol'],
        'trading_config':             ['key'],
        'trading_fee_rules':          ['symbol'],
        'trading_daily_briefing':     ['date'],
        'trading_bg_tasks':           ['task_id'],
        'trading_intel_crawl_log':    ['crawl_date', 'category', 'source_key'],
        # Swarm artifact store
        'artifacts':                  ['key'],
        # Scheduler
        'scheduled_tasks':            ['id'],
        'proactive_poll_log':         ['id'],
        # Error tracking
        'error_resolutions':          ['fingerprint'],
    }
    return _PK_MAP.get(table_name)


# ── SQL translation cache ──
_translate_sql_cache = {}  # str → (str|None, bool)
_TRANSLATE_CACHE_MAX = 1024


def translate_sql(sql):
    """Translate legacy SQL syntax to PostgreSQL.

    Returns (translated_sql, is_pragma).
    PRAGMA statements return (None, True) to signal they should be skipped.

    Results are cached (same SQL template always produces the same output)
    to avoid regex overhead on hot paths (poll every 500ms, meta every 5s).
    """
    cached = _translate_sql_cache.get(sql)
    if cached is not None:
        return cached
    result = _translate_sql_uncached(sql)
    if len(_translate_sql_cache) < _TRANSLATE_CACHE_MAX:
        _translate_sql_cache[sql] = result
    return result


def _translate_sql_uncached(sql):
    """Actual SQL translation logic (uncached)."""
    stripped = sql.strip()

    # PRAGMA table_info(X) → SELECT column info from information_schema
    m_pti = _RE_PRAGMA_TABLE_INFO.search(stripped)
    if m_pti:
        table_name = m_pti.group(1)
        return (
            f"SELECT ordinal_position - 1 as cid, column_name as name, "
            f"data_type as type, 0 as notnull, NULL as dflt_value, 0 as pk "
            f"FROM information_schema.columns "
            f"WHERE table_name = '{table_name}' "
            f"ORDER BY ordinal_position"
        ), False

    # Skip other PRAGMAs entirely
    if _RE_PRAGMA.match(stripped):
        return None, True

    # SELECT changes() → not supported, return a constant
    if _RE_CHANGES.search(stripped):
        return "SELECT 0", False

    # SELECT last_insert_rowid() → SELECT lastval()
    if 'last_insert_rowid' in stripped.lower():
        return "SELECT lastval()", False

    # INSERT OR REPLACE → INSERT ... ON CONFLICT (...) DO UPDATE SET ...
    m = _RE_INSERT_OR_REPLACE.search(stripped)
    if m:
        table_name = m.group(1)
        columns_str = m.group(2)
        columns = [c.strip().strip('"').strip("'") for c in columns_str.split(',')]
        pk_cols = _get_pk_columns(table_name)

        if pk_cols:
            non_pk = [c for c in columns if c not in pk_cols]
            pk_str = ', '.join(pk_cols)
            if non_pk:
                update_set = ', '.join(f'{c} = EXCLUDED.{c}' for c in non_pk)
                replacement = f'INSERT INTO {table_name} ({columns_str}) '
                translated = stripped.replace(m.group(0), replacement, 1)
                translated += f' ON CONFLICT ({pk_str}) DO UPDATE SET {update_set}'
            else:
                replacement = f'INSERT INTO {table_name} ({columns_str}) '
                translated = stripped.replace(m.group(0), replacement, 1)
                translated += f' ON CONFLICT ({pk_str}) DO NOTHING'
        else:
            # Unknown table — fall back to DO NOTHING
            replacement = f'INSERT INTO {table_name} ({columns_str}) '
            translated = stripped.replace(m.group(0), replacement, 1)
            translated += ' ON CONFLICT DO NOTHING'
            logger.debug('[DB] INSERT OR REPLACE for unknown table %s — using DO NOTHING', table_name)

        stripped = translated

    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if _RE_INSERT_OR_IGNORE.search(stripped):
        stripped = _RE_INSERT_OR_IGNORE.sub('INSERT INTO', stripped)
        if 'ON CONFLICT' not in stripped.upper():
            stripped += ' ON CONFLICT DO NOTHING'

    # strftime('%s','now')*1000 → (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
    stripped = _RE_STRFTIME_EPOCH_MS.sub(
        "(EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT", stripped
    )

    # strftime('%s','now') → EXTRACT(EPOCH FROM NOW())::BIGINT
    stripped = _RE_STRFTIME_EPOCH.sub(
        "EXTRACT(EPOCH FROM NOW())::BIGINT", stripped
    )

    # datetime('now') → NOW()
    stripped = _RE_DATETIME_NOW.sub('NOW()', stripped)

    # json_array_length → jsonb_array_length
    stripped = _RE_JSON_ARRAY_LENGTH.sub('jsonb_array_length', stripped)

    # json_extract(col, '$.key') → col::jsonb->>'key'
    stripped = _RE_JSON_EXTRACT.sub(r"\1::jsonb->>'\2'", stripped)

    # INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
    stripped = re.sub(
        r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
        'SERIAL PRIMARY KEY',
        stripped,
        flags=re.IGNORECASE
    )

    # COLLATE NOCASE → (PostgreSQL uses ILIKE or citext; just remove it)
    stripped = re.sub(r'\bCOLLATE\s+NOCASE\b', '', stripped, flags=re.IGNORECASE)

    # ? → %s (parameter placeholders)
    stripped = _translate_placeholders(stripped)

    return stripped, False


def _translate_placeholders(sql):
    """Replace ? with %s, avoiding replacements inside string literals."""
    result = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_string:
            in_string = True
            result.append(ch)
        elif ch == "'" and in_string:
            if i + 1 < len(sql) and sql[i + 1] == "'":
                result.append("''")
                i += 2
                continue
            in_string = False
            result.append(ch)
        elif ch == '?' and not in_string:
            result.append('%s')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)
