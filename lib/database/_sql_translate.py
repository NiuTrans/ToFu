"""SQL compatibility layer — translates legacy SQL syntax to PostgreSQL.

Handles ? → %s placeholders, INSERT OR REPLACE → ON CONFLICT, PRAGMA no-ops, etc.
Extracted from _core.py for modularity. Re-exported via _core for backward compat.
"""

import re

from lib.log import get_logger
from lib.ttl_cache import TTLCache

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
# Matches json_extract(col, '$.<path>') where <path> may be a single key,
# a nested dotted path (a.b.c), or include array indices (a[0].b). The full
# path body after '$.' / '$' is captured and translated to a PG jsonb path.
_RE_JSON_EXTRACT = re.compile(
    r"json_extract\s*\(\s*(\w+)\s*,\s*'\$\.?([^']+)'\s*\)",
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
        # conversations: MIGRATED to lib/database/_core_schema.upsert()
        # (queries-on-Core) — all 7 INSERT OR REPLACE call-sites converted.
        # task_results: MIGRATED to lib/database/_core_schema.upsert()
        # (queries-on-Core) — no INSERT OR REPLACE call-sites remain.
        # task_events: MIGRATED to lib/database/_core_schema.upsert() (DO NOTHING,
        # queries-on-Core) — no INSERT OR REPLACE/IGNORE call-sites remain.
        'users':                      ['id'],
        'schema_meta':                ['key'],
        # pricing_cache: MIGRATED to lib/database/_core_schema.upsert() — no
        # INSERT OR REPLACE call-sites remain, so no translation entry needed.
        # (First table on the queries-on-Core track; see the proof-of-concept.)
        'recent_projects':            ['path'],
        'trading_price_cache':        ['symbol'],
        'trading_config':             ['key'],
        'trading_fee_rules':          ['symbol'],
        'trading_daily_briefing':     ['date'],
        'trading_bg_tasks':           ['task_id'],
        'trading_intel_crawl_log':    ['crawl_date', 'category', 'source_key'],
        # Swarm artifact store (legacy in-memory, kept for back-compat)
        'artifacts':                  ['key'],
        # Chat artifacts (renderable reports — md / html / svg)
        'chat_artifacts':             ['id'],
        # Scheduler
        'scheduled_tasks':            ['id'],
        'proactive_poll_log':         ['id'],
        # Error tracking
        'error_resolutions':          ['fingerprint'],
        # paper_reports / paper_translations / paper_library: MIGRATED to
        # lib/database/_core_schema.upsert() (queries-on-Core) — no INSERT OR
        # REPLACE call-sites remain, so no translation entries needed.
        # daily_cost_cache: MIGRATED to lib/database/_core_schema.upsert()
        # (composite-PK queries-on-Core conversion) — no INSERT OR REPLACE
        # call-sites remain, so no translation entry needed.
        # Trading autopilot strategy-pair compatibility scores
        'trading_strategy_compatibility': ['pair_key'],
    }
    return _PK_MAP.get(table_name)


# ── SQL translation cache ──
# Backed by lib.ttl_cache.TTLCache. ttl=0 disables expiry (translations
# are deterministic per input SQL). max_size=1024 caps memory.
_translate_sql_cache = TTLCache(ttl=0, max_size=1024, name='sql_translate')


def translate_sql(sql):
    """Translate legacy SQL syntax to PostgreSQL.

    Returns (translated_sql, is_pragma).
    PRAGMA statements return (None, True) to signal they should be skipped.

    Results are cached (same SQL template always produces the same output)
    to avoid regex overhead on hot paths (poll every 500ms, meta every 5s).
    """
    return _translate_sql_cache.get_or_compute(sql, lambda: _translate_sql_uncached(sql))


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
            # Unknown table — we cannot synthesize a correct ON CONFLICT target.
            # Falling back to DO NOTHING would SILENTLY DROP the write (the row
            # is neither inserted on conflict nor updated). That is a data-loss
            # bug, so fail loudly instead: register the table's PK in _PK_MAP.
            logger.error('[DB] INSERT OR REPLACE into unmapped table %r — no PK '
                         'known, cannot translate to a safe upsert. Add it to '
                         '_PK_MAP in lib/database/_sql_translate.py.', table_name)
            raise ValueError(
                f'INSERT OR REPLACE into table {table_name!r} not supported on '
                f'PostgreSQL: no primary key registered in _PK_MAP. Add an entry '
                f'so a correct ON CONFLICT target can be generated.')

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

    # json_extract(col, '$.<path>') → PG jsonb accessor.
    #   single key      $.name      → col::jsonb->>'name'
    #   nested / array   $.a.b, $[0] → col::jsonb#>>'{a,b}' / '{0}'
    stripped = _RE_JSON_EXTRACT.sub(_json_extract_repl, stripped)

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


def _json_extract_repl(m):
    """Translate a matched ``json_extract(col, '$.<path>')`` to a PG accessor.

    Single top-level key  → ``col::jsonb->>'key'`` (text extraction).
    Nested / array path    → ``col::jsonb#>>'{a,b,0}'`` (text at path).

    The SQLite path syntax ``$.a.b`` / ``$[0]`` / ``$.a[0].b`` is parsed into
    path segments; numeric ``[i]`` indices become bare integers in the PG
    path array (jsonb path arrays index arrays positionally).
    """
    col = m.group(1)
    path_body = m.group(2)
    # Split "a.b[0].c" → ['a', 'b', '0', 'c']
    segments = []
    for dot_part in path_body.split('.'):
        if not dot_part:
            continue
        # Peel any [i] array indices off this dotted segment.
        idx_split = re.split(r'\[(\d+)\]', dot_part)
        for tok in idx_split:
            tok = tok.strip()
            if tok:
                segments.append(tok)
    if len(segments) <= 1:
        key = segments[0] if segments else path_body
        return f"{col}::jsonb->>'{key}'"
    pg_path = ','.join(segments)
    return f"{col}::jsonb#>>'{{{pg_path}}}'"


def _translate_placeholders(sql):
    """Replace ``?`` with ``%s`` for psycopg2, avoiding string literals.

    psycopg2 runs client-side ``%``-interpolation on the query string WHENEVER
    parameters are bound. A literal ``%`` in the SQL (e.g. a ``LIKE '%foo%'``
    pattern) is then mis-read as a format spec, raising ``IndexError: tuple
    index out of range`` or, when the bytes happen to slip through, a
    PostgreSQL ``near "%": syntax error``. To stay correct we MUST double every
    literal ``%`` → ``%%`` so psycopg2 un-escapes it back to a single ``%``.

    This doubling only applies when the statement actually contains a ``?``
    placeholder: a paramless statement is sent verbatim (psycopg2 does no
    interpolation without params), so its ``%`` must be left untouched —
    doubling there would corrupt the literal.
    """
    has_placeholder = _has_qmark_placeholder(sql)
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
        elif ch == '%' and has_placeholder:
            # Escape literal % (inside OR outside string literals) so psycopg2's
            # interpolation restores it. The %s we emit for ? is added above and
            # never reaches this branch, so it is not double-escaped.
            result.append('%%')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _has_qmark_placeholder(sql):
    """True if *sql* contains a ``?`` placeholder outside any string literal."""
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_string and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            in_string = not in_string
        elif ch == '?' and not in_string:
            return True
        i += 1
    return False
