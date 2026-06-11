"""tests/test_core_schema_parity.py — READ-ONLY parity gate for Core migration.

This ships NOTHING live. It never opens a DB connection and never touches
`init_db()`. It only compiles the SQLAlchemy Core definition of a table to
DDL strings (compile-only, via `_core_schema.both_ddl`) and compares them,
token-normalized, against the live hand-maintained DDL in `_schema_pg.py` /
`_schema_sqlite.py`.

Purpose: a table is only *eligible* to be migrated onto Core once its Core
definition byte-matches (modulo whitespace/IF-NOT-EXISTS) the schema already
on disk. Until this test is green for a table, wiring it into the live
bootstrap would silently diverge fresh installs from upgraded ones.

Run:  pytest tests/test_core_schema_parity.py -v
"""

import re

import pytest

sa = pytest.importorskip("sqlalchemy")

from lib.database._core_schema import (
    DAILY_COST_CACHE as _DAILY_COST_CACHE,
    PAPER_LIBRARY as _PAPER_LIBRARY,
    PAPER_REPORTS as _PAPER_REPORTS,
    PAPER_TRANSLATIONS as _PAPER_TRANSLATIONS,
    PRICING_CACHE as _PRICING_CACHE,
    RECENT_PROJECTS as _RECENT_PROJECTS,
    CHAT_ARTIFACTS as _CHAT_ARTIFACTS,
    CONVERSATIONS as _CONVERSATIONS,
    SCHEMA_META as _SCHEMA_META,
    TRANSCRIPT_ARCHIVE as _TRANSCRIPT_ARCHIVE,
    USERS as _USERS_CANON,
    TASK_EVENTS as _TASK_EVENTS,
    TASK_RESULTS as _TASK_RESULTS,
    TRADING_CONFIG as _TRADING_CONFIG,
    both_ddl,
    define_table,
    jsonb_column,
)


# ── Live DDL as it exists on disk today (source of truth) ───────────────
# conversations — OPTION B Core-owned base shape. search_text is now in the
# base CREATE on BOTH backends (SQLite always had it; on PG the Core create
# emits it and the _column_exists-guarded ALTER becomes a no-op). The PG-only
# search_tsv column + GIN indexes + trigger are NOT part of the base table —
# they stay as explicit guarded post-create DDL in _schema_pg.py, so they are
# intentionally absent from this base-table parity constant.
LIVE_PG_CONVERSATIONS = """
    CREATE TABLE conversations (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        msg_count INTEGER NOT NULL DEFAULT 0,
        search_text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (id, user_id)
    )
"""

LIVE_SQLITE_CONVERSATIONS = """
    CREATE TABLE conversations (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages TEXT NOT NULL DEFAULT '[]',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        msg_count INTEGER NOT NULL DEFAULT 0,
        search_text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (id, user_id)
    )
"""


# trading_config — identical on both backends
# (lib/database/_schema_pg.py:513, _schema_sqlite.py:433)
LIVE_TRADING_CONFIG = """
    CREATE TABLE trading_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
"""

# schema_meta — core-owned bootstrap kv (version + active domains); same shape
# as trading_config, identical on both backends.
LIVE_SCHEMA_META = """
    CREATE TABLE schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
"""


# pricing_cache — system kv cache (lib/database/_schema_pg.py:862, _sqlite.py:773)
LIVE_PG_PRICING_CACHE = """
    CREATE TABLE pricing_cache (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at BIGINT NOT NULL
    )
"""
LIVE_SQLITE_PRICING_CACHE = """
    CREATE TABLE pricing_cache (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
"""

# recent_projects — MRU list (lib/database/_schema_pg.py:870, _sqlite.py:781)
LIVE_PG_RECENT_PROJECTS = """
    CREATE TABLE recent_projects (
        path TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 1,
        last_used BIGINT NOT NULL
    )
"""
LIVE_SQLITE_RECENT_PROJECTS = """
    CREATE TABLE recent_projects (
        path TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 1,
        last_used INTEGER NOT NULL
    )
"""


# daily_cost_cache — per-day cost aggregate, composite PK (user_id, date)
# (lib/database/_schema_pg.py:442, _schema_sqlite.py:345)
LIVE_PG_DAILY_COST_CACHE = """
    CREATE TABLE daily_cost_cache (
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        conversations_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        computed_at BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, date)
    )
"""
LIVE_SQLITE_DAILY_COST_CACHE = """
    CREATE TABLE daily_cost_cache (
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        cost REAL NOT NULL DEFAULT 0,
        conversations_json TEXT NOT NULL DEFAULT '{}',
        computed_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, date)
    )
"""


# paper_translations — Babel whole-paper cache, composite PK
# (lib/database/_schema_pg.py:511, _schema_sqlite.py:415)
LIVE_PG_PAPER_TRANSLATIONS = """
    CREATE TABLE paper_translations (
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (paper_hash, lang)
    )
"""
LIVE_SQLITE_PAPER_TRANSLATIONS = """
    CREATE TABLE paper_translations (
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        PRIMARY KEY (paper_hash, lang)
    )
"""


# paper_reports — report cache, composite PK (lib/database/_schema_pg.py:406, _sqlite.py:307)
LIVE_PG_PAPER_REPORTS = """
    CREATE TABLE paper_reports (
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'en',
        report TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (paper_hash, lang)
    )
"""
LIVE_SQLITE_PAPER_REPORTS = """
    CREATE TABLE paper_reports (
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'en',
        report TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        PRIMARY KEY (paper_hash, lang)
    )
"""

# paper_library — bookshelf, composite PK + FK users (lib/database/_schema_pg.py:418, _sqlite.py:321)
LIVE_PG_PAPER_LIBRARY = """
    CREATE TABLE paper_library (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT '',
        pdf_url TEXT NOT NULL DEFAULT '',
        pdf_filename TEXT NOT NULL DEFAULT '',
        arxiv_id TEXT NOT NULL DEFAULT '',
        paper_hash TEXT NOT NULL DEFAULT '',
        parsed_text TEXT NOT NULL DEFAULT '',
        qa_history TEXT NOT NULL DEFAULT '[]',
        images TEXT NOT NULL DEFAULT '[]',
        babel_cache TEXT NOT NULL DEFAULT '{}',
        page_count INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (id, user_id)
    )
"""
LIVE_SQLITE_PAPER_LIBRARY = """
    CREATE TABLE paper_library (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT '',
        pdf_url TEXT NOT NULL DEFAULT '',
        pdf_filename TEXT NOT NULL DEFAULT '',
        arxiv_id TEXT NOT NULL DEFAULT '',
        paper_hash TEXT NOT NULL DEFAULT '',
        parsed_text TEXT NOT NULL DEFAULT '',
        qa_history TEXT NOT NULL DEFAULT '[]',
        images TEXT NOT NULL DEFAULT '[]',
        babel_cache TEXT NOT NULL DEFAULT '{}',
        page_count INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (id, user_id)
    )
"""


# task_results — persisted task output (lib/database/_schema_pg.py:238, _sqlite.py:168)
LIVE_PG_TASK_RESULTS = """
    CREATE TABLE task_results (
        task_id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        thinking TEXT NOT NULL DEFAULT '',
        error TEXT,
        status TEXT NOT NULL DEFAULT 'done',
        tool_rounds TEXT,
        search_results TEXT,
        metadata TEXT,
        created_at BIGINT NOT NULL,
        completed_at BIGINT
    )
"""
LIVE_SQLITE_TASK_RESULTS = """
    CREATE TABLE task_results (
        task_id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        thinking TEXT NOT NULL DEFAULT '',
        error TEXT,
        status TEXT NOT NULL DEFAULT 'done',
        tool_rounds TEXT,
        search_results TEXT,
        metadata TEXT,
        created_at INTEGER NOT NULL,
        completed_at INTEGER
    )
"""

# task_events — persisted SSE event log (lib/database/_schema_pg.py:259, _sqlite.py:189)
LIVE_PG_TASK_EVENTS = """
    CREATE TABLE task_events (
        task_id TEXT NOT NULL,
        event_id BIGINT NOT NULL,
        ts_ms BIGINT NOT NULL,
        type TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (task_id, event_id)
    )
"""
LIVE_SQLITE_TASK_EVENTS = """
    CREATE TABLE task_events (
        task_id TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        ts_ms INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (task_id, event_id)
    )
"""


# chat_artifacts — renderable reports (lib/database/_schema_pg.py:256, _sqlite.py:188)
LIVE_PG_CHAT_ARTIFACTS = """
    CREATE TABLE chat_artifacts (
        id              TEXT    PRIMARY KEY,
        conv_id         TEXT    NOT NULL,
        task_id         TEXT    NOT NULL DEFAULT '',
        msg_id          TEXT    NOT NULL DEFAULT '',
        source          TEXT    NOT NULL,
        source_ref      JSONB   NOT NULL DEFAULT '{}'::jsonb,
        format          TEXT    NOT NULL,
        title           TEXT    NOT NULL DEFAULT '',
        content         TEXT    NOT NULL,
        content_sha256  TEXT    NOT NULL,
        size_bytes      INTEGER NOT NULL DEFAULT 0,
        version         INTEGER NOT NULL DEFAULT 1,
        parent_id       TEXT    NOT NULL DEFAULT '',
        pinned          BOOLEAN NOT NULL DEFAULT FALSE,
        meta            JSONB   NOT NULL DEFAULT '{}'::jsonb,
        created_at      BIGINT  NOT NULL,
        deleted_at      BIGINT  NOT NULL DEFAULT 0
    )
"""
LIVE_SQLITE_CHAT_ARTIFACTS = """
    CREATE TABLE chat_artifacts (
        id              TEXT    PRIMARY KEY,
        conv_id         TEXT    NOT NULL,
        task_id         TEXT    NOT NULL DEFAULT '',
        msg_id          TEXT    NOT NULL DEFAULT '',
        source          TEXT    NOT NULL,
        source_ref      TEXT    NOT NULL DEFAULT '{}',
        format          TEXT    NOT NULL,
        title           TEXT    NOT NULL DEFAULT '',
        content         TEXT    NOT NULL,
        content_sha256  TEXT    NOT NULL,
        size_bytes      INTEGER NOT NULL DEFAULT 0,
        version         INTEGER NOT NULL DEFAULT 1,
        parent_id       TEXT    NOT NULL DEFAULT '',
        pinned          INTEGER NOT NULL DEFAULT 0,
        meta            TEXT    NOT NULL DEFAULT '{}',
        created_at      INTEGER NOT NULL,
        deleted_at      INTEGER NOT NULL DEFAULT 0
    )
"""


# transcript_archive — pre-compaction snapshots (lib/database/_schema_pg.py:266,
# _schema_sqlite.py:198). Auto-increment PK + per-dialect created_at default.
LIVE_PG_TRANSCRIPT_ARCHIVE = """
    CREATE TABLE transcript_archive (
        id SERIAL PRIMARY KEY,
        conv_id TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
        trigger TEXT NOT NULL DEFAULT 'force',
        task_id TEXT NOT NULL DEFAULT '',
        round_num INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        tokens_before INTEGER NOT NULL DEFAULT 0,
        tokens_after INTEGER NOT NULL DEFAULT 0,
        msgs_before INTEGER NOT NULL DEFAULT 0,
        msgs_after INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT ''
    )
"""
LIVE_SQLITE_TRANSCRIPT_ARCHIVE = """
    CREATE TABLE transcript_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        trigger TEXT NOT NULL DEFAULT 'force',
        task_id TEXT NOT NULL DEFAULT '',
        round_num INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        tokens_before INTEGER NOT NULL DEFAULT 0,
        tokens_after INTEGER NOT NULL DEFAULT 0,
        msgs_before INTEGER NOT NULL DEFAULT 0,
        msgs_after INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT ''
    )
"""


# users — account table (lib/database/_schema_pg.py:212, _schema_sqlite.py:142)
LIVE_PG_USERS = """
    CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
"""
LIVE_SQLITE_USERS = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
"""


def _norm(ddl: str) -> str:
    """Canonicalize DDL to a *semantic* form so backends that differ only in
    formatting compare equal, while genuine schema divergences (column types,
    missing columns, different PK columns) still differ.

    Transforms applied (all semantics-preserving on both PG and SQLite):
      - drop ``IF NOT EXISTS`` and collapse whitespace;
      - strip double-quotes around identifiers (SQLAlchemy quotes reserved
        words like ``"key"``);
      - reorder ``DEFAULT x NOT NULL`` → ``NOT NULL DEFAULT x``;
      - rewrite an inline single-column ``PRIMARY KEY`` into a trailing
        ``PRIMARY KEY (col)`` table constraint (the form Core emits);
      - strip PostgreSQL ``::type`` cast suffixes on default literals
        (``'{}'::jsonb`` ≡ ``'{}'`` — PG implicitly casts the literal to the
        column type; Core omits the explicit cast).

      - strip a per-dialect ``DEFAULT`` *expression*'s in-parenthesis spaces
        and commas so a multi-token function default (``EXTRACT(EPOCH FROM
        NOW())``, ``strftime('%s','now')``) collapses to a single opaque
        token the structural rules can handle;
      - treat ``SERIAL``-trailing-PK (PG) and ``INTEGER PRIMARY KEY
        AUTOINCREMENT``-inline (SQLite) as the same single-col PK, and drop
        the PK column's redundant ``NOT NULL`` even when ``AUTOINCREMENT``
        trails it.

    These are deliberately narrow. The ``conversations`` xfail (strict) acts
    as a tripwire: if a transform here ever over-masks, that genuinely
    divergent table would start passing and fail the suite as an xpass.
    """
    s = ddl.strip()
    s = re.sub(r"\bIF NOT EXISTS\b", "", s, flags=re.IGNORECASE)
    s = s.replace('"', "")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("( ", "(").replace(" )", ")")
    s = s.rstrip(";").strip().lower()
    # Strip PG default-literal casts: '{}'::jsonb -> '{}', 0::bigint -> 0.
    s = re.sub(r"::\s*\w+", "", s)
    # Mask spaces/commas INSIDE parentheses (depth>0) so a function-call
    # default like extract(epoch from now()) or strftime('%s','now') becomes a
    # single comma-free, space-free token. The same expression renders
    # identically on Core and live, so the masked tokens compare equal; the
    # mask is never reversed. This keeps the comma/space-sensitive rules below
    # from mis-splitting a column on punctuation that lives inside an default
    # expression. (Comparison is per-backend, so PG↔SQLite never compared.)
    # depth 1 = the table's column-list parens; depth >1 = a NESTED paren,
    # i.e. inside a function-call default. Only mask at depth >1 so the table
    # body's own column/comma structure is preserved.
    masked, depth = [], 0
    for ch in s:
        if ch == "(":
            depth += 1
            masked.append(ch)
            continue
        elif ch == ")":
            depth = max(0, depth - 1)
            masked.append(ch)
            continue
        if depth > 1 and ch == " ":
            ch = "\x01"
        elif depth > 1 and ch == ",":
            ch = "\x02"
        masked.append(ch)
    s = "".join(masked)
    # DEFAULT <val> NOT NULL  ->  NOT NULL DEFAULT <val>
    # <val> is a quoted string, or any now-space-free token (incl. a masked
    # function-call default).
    s = re.sub(r"default\s+('(?:[^']|'')*'|\S+)\s+not null", r"not null default \1", s)
    # inline single-col PK:  "<col> <type...> primary key ..."  ->
    #   strip inline + append "primary key (<col>)" as a trailing constraint.
    m = re.search(r"\(\s*(\w+)\b[^,]*\bprimary key\b", s)
    if m and "primary key (" not in s:
        col = m.group(1)
        s = re.sub(r"\bprimary key\b", "", s, count=1)
        s = re.sub(r"\s+", " ", s).replace(" ,", ",").replace(", ", ", ")
        # Drop exactly ONE trailing paren (the table's), NOT a function
        # default's closing paren(s).
        body = s[:-1] if s.endswith(")") else s
        body = body.rstrip().rstrip(",")
        s = re.sub(r"\s+", " ", body + f", primary key ({col}))")
    # PRIMARY KEY implies NOT NULL on both PG and SQLite. For a single-column
    # PK, drop the redundant NOT NULL Core makes explicit — even when an
    # AUTOINCREMENT keyword trails it (SQLite) — so the trailing-constraint
    # form matches the inline form.
    pk = re.search(r"primary key \((\w+)\)", s)
    if pk:
        col = pk.group(1)
        s = re.sub(rf"(\(\s*{col}\s+\w+) not null( autoincrement)?", r"\1\2", s)
    # TIMESTAMPTZ (hand-DDL alias) == TIMESTAMP WITH TIME ZONE (SQLAlchemy
    # canonical spelling). Canonicalize to the short form.
    s = s.replace("timestamp with time zone", "timestamptz")
    # inline column UNIQUE -> trailing UNIQUE (col). Live writes
    # "username text unique not null"; Core emits the column without UNIQUE
    # plus a trailing "unique (username)" table constraint. Hoist the inline
    # form to match. (An already-trailing "unique (" is left untouched by the
    # negative lookahead.)
    mu = re.search(r"\b(\w+)\s+\w+\s+unique\b(?!\s*\()", s)
    if mu and "unique (" not in s:
        col = mu.group(1)
        s = re.sub(r"\bunique\b(?!\s*\()", "", s, count=1)
        s = re.sub(r"\s+", " ", s).replace(" ,", ",")
        body = s[:-1] if s.endswith(")") else s
        body = body.rstrip().rstrip(",")
        s = re.sub(r"\s+", " ", body + f", unique ({col}))")
    # Foreign keys: live writes them INLINE on the column
    # (`user_id integer not null references users(id) on delete cascade`);
    # Core emits a TRAILING `foreign key(user_id) references users (id) on
    # delete cascade` constraint. Canonicalize BOTH to a single positional
    # `\x03fk(col->target,action)\x03` token (then drop it from the column
    # body) so inline vs trailing compares equal while a genuinely different
    # target/column/action still differs. Handles the optional space before
    # the target paren (`users (id)` vs `users(id)`).
    fk_tokens = []

    def _fk_token(col, target, tcol, action):
        return "\x03fk(%s->%s.%s%s)\x03" % (
            col.strip(), target.strip(), tcol.strip(),
            (";" + action.strip()) if action.strip() else "")

    # trailing: foreign key(col) references target (tcol) [on delete X]
    def _sub_trailing(m):
        fk_tokens.append(_fk_token(m.group(1), m.group(2), m.group(3), m.group(4) or ""))
        return ""
    s = re.sub(
        r",?\s*foreign key\((\w+)\) references (\w+)\s*\((\w+)\)( on delete \w+)?",
        _sub_trailing, s)

    # inline: <col> <type...> references target (tcol) [on delete X]  ->
    # keep the column + type, strip just the REFERENCES clause, emit token.
    def _inline_repl(m):
        fk_tokens.append(_fk_token(m.group('col'), m.group('tgt'), m.group('tcol'), m.group('act') or ""))
        return ""  # remove just the references clause
    s = re.sub(
        r"(?P<col>\b\w+\b)(?P<mid>[^,]*?)\s+references (?P<tgt>\w+)\s*\((?P<tcol>\w+)\)(?P<act> on delete \w+)?",
        lambda m: m.group('col') + m.group('mid') + _inline_repl(m), s)
    if fk_tokens:
        body = s[:-1] if s.endswith(")") else s
        body = body.rstrip().rstrip(",")
        s = re.sub(r"\s+", " ", body + ", " + ", ".join(sorted(fk_tokens)) + ")")

    # Trailing table-constraint ORDER is semantically irrelevant
    # (`primary key (...)` vs `unique (...)`): Core and the hoisted hand-DDL
    # may list them in either order. Sort any run of trailing
    # `primary key (...)` / `unique (...)` constraints so the comparison is
    # order-independent. Only these two constraint kinds are touched.
    tail = re.search(r"((?:,\s*(?:primary key|unique)\s*\([^)]*\))+)\s*\)\s*$", s)
    if tail:
        clauses = re.findall(r"(?:primary key|unique)\s*\([^)]*\)", tail.group(1))
        if len(clauses) > 1:
            rebuilt = ", " + ", ".join(sorted(clauses))
            s = s[:tail.start(1)] + rebuilt + ")"
            s = re.sub(r"\s+", " ", s)
    return s


# The canonical `users` table (imported from _core_schema) is the FK target for
# `conversations` — Core needs it present in the same MetaData to compile
# `REFERENCES users(id)`. It is already defined on the shared MetaData, so we
# reuse it rather than redefining (which would raise "already defined").
_USERS = _USERS_CANON


def test_trading_config_pg_parity():
    core = both_ddl(_TRADING_CONFIG)["pg"]
    assert _norm(core) == _norm(LIVE_TRADING_CONFIG), (
        "\n--- Core PG ---\n" + core +
        "\n--- Live PG ---\n" + LIVE_TRADING_CONFIG
    )


def test_trading_config_sqlite_parity():
    core = both_ddl(_TRADING_CONFIG)["sqlite"]
    assert _norm(core) == _norm(LIVE_TRADING_CONFIG), (
        "\n--- Core SQLite ---\n" + core +
        "\n--- Live SQLite ---\n" + LIVE_TRADING_CONFIG
    )


def test_schema_meta_pg_parity():
    core = both_ddl(_SCHEMA_META)["pg"]
    assert _norm(core) == _norm(LIVE_SCHEMA_META), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_SCHEMA_META
    )


def test_schema_meta_sqlite_parity():
    core = both_ddl(_SCHEMA_META)["sqlite"]
    assert _norm(core) == _norm(LIVE_SCHEMA_META), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SCHEMA_META
    )


def test_pricing_cache_pg_parity():
    core = both_ddl(_PRICING_CACHE)["pg"]
    assert _norm(core) == _norm(LIVE_PG_PRICING_CACHE), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_PRICING_CACHE
    )


def test_pricing_cache_sqlite_parity():
    core = both_ddl(_PRICING_CACHE)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_PRICING_CACHE), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_PRICING_CACHE
    )


def test_daily_cost_cache_pg_parity():
    core = both_ddl(_DAILY_COST_CACHE)["pg"]
    assert _norm(core) == _norm(LIVE_PG_DAILY_COST_CACHE), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_DAILY_COST_CACHE
    )


def test_daily_cost_cache_sqlite_parity():
    core = both_ddl(_DAILY_COST_CACHE)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_DAILY_COST_CACHE), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_DAILY_COST_CACHE
    )


def test_recent_projects_pg_parity():
    core = both_ddl(_RECENT_PROJECTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_RECENT_PROJECTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_RECENT_PROJECTS
    )


def test_recent_projects_sqlite_parity():
    core = both_ddl(_RECENT_PROJECTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_RECENT_PROJECTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_RECENT_PROJECTS
    )


def test_paper_reports_pg_parity():
    core = both_ddl(_PAPER_REPORTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_PAPER_REPORTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_PAPER_REPORTS
    )


def test_paper_reports_sqlite_parity():
    core = both_ddl(_PAPER_REPORTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_PAPER_REPORTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_PAPER_REPORTS
    )


def test_paper_library_pg_parity():
    core = both_ddl(_PAPER_LIBRARY)["pg"]
    assert _norm(core) == _norm(LIVE_PG_PAPER_LIBRARY), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_PAPER_LIBRARY
    )


def test_paper_library_sqlite_parity():
    core = both_ddl(_PAPER_LIBRARY)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_PAPER_LIBRARY), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_PAPER_LIBRARY
    )


def test_paper_translations_pg_parity():
    core = both_ddl(_PAPER_TRANSLATIONS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_PAPER_TRANSLATIONS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_PAPER_TRANSLATIONS
    )


def test_paper_translations_sqlite_parity():
    core = both_ddl(_PAPER_TRANSLATIONS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_PAPER_TRANSLATIONS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_PAPER_TRANSLATIONS
    )


def test_task_results_pg_parity():
    core = both_ddl(_TASK_RESULTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_TASK_RESULTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_TASK_RESULTS
    )


def test_task_results_sqlite_parity():
    core = both_ddl(_TASK_RESULTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_TASK_RESULTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_TASK_RESULTS
    )


def test_task_events_pg_parity():
    core = both_ddl(_TASK_EVENTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_TASK_EVENTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_TASK_EVENTS
    )


def test_task_events_sqlite_parity():
    core = both_ddl(_TASK_EVENTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_TASK_EVENTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_TASK_EVENTS
    )


def test_chat_artifacts_pg_parity():
    core = both_ddl(_CHAT_ARTIFACTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_CHAT_ARTIFACTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_CHAT_ARTIFACTS
    )


def test_chat_artifacts_sqlite_parity():
    core = both_ddl(_CHAT_ARTIFACTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_CHAT_ARTIFACTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_CHAT_ARTIFACTS
    )


def test_norm_distinguishes_genuine_differences():
    """Guard that `_norm` only masks COSMETIC differences, never structural
    ones. This replaces the role the conversations strict-xfail played before
    conversations became a real passing test: if a future `_norm` rule
    over-masks, one of these must start failing."""
    base = "CREATE TABLE x (id TEXT NOT NULL, a INTEGER, PRIMARY KEY (id))"
    diff_name = "CREATE TABLE x (id TEXT NOT NULL, b INTEGER, PRIMARY KEY (id))"
    diff_type = "CREATE TABLE x (id TEXT NOT NULL, a BIGINT, PRIMARY KEY (id))"
    with_fk = "CREATE TABLE x (id TEXT NOT NULL, a INTEGER REFERENCES y(id), PRIMARY KEY (id))"
    diff_fk = "CREATE TABLE x (id TEXT NOT NULL, a INTEGER REFERENCES z(id), PRIMARY KEY (id))"
    assert _norm(base) == _norm(base)
    assert _norm(base) != _norm(diff_name), "column name difference masked"
    assert _norm(base) != _norm(diff_type), "column type difference masked"
    assert _norm(base) != _norm(with_fk), "FK presence difference masked"
    assert _norm(with_fk) != _norm(diff_fk), "FK target difference masked"


def test_users_pg_parity():
    core = both_ddl(_USERS_CANON)["pg"]
    assert _norm(core) == _norm(LIVE_PG_USERS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_USERS
    )


def test_users_sqlite_parity():
    core = both_ddl(_USERS_CANON)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_USERS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_USERS
    )


def test_transcript_archive_pg_parity():
    core = both_ddl(_TRANSCRIPT_ARCHIVE)["pg"]
    assert _norm(core) == _norm(LIVE_PG_TRANSCRIPT_ARCHIVE), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_TRANSCRIPT_ARCHIVE
    )


def test_transcript_archive_sqlite_parity():
    core = both_ddl(_TRANSCRIPT_ARCHIVE)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_TRANSCRIPT_ARCHIVE), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_TRANSCRIPT_ARCHIVE
    )


# conversations — OPTION B: Core owns the base table (now incl. search_text on
# both backends); the PG-only search_tsv/pg_trgm/GIN/trigger infra stays as
# explicit guarded post-create DDL. Formerly xfail (search_text asymmetry) —
# now a real passing parity check.
def test_conversations_pg_parity():
    core = both_ddl(_CONVERSATIONS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_CONVERSATIONS), (
        "\n--- Core PG ---\n" + core +
        "\n--- Live PG ---\n" + LIVE_PG_CONVERSATIONS
    )


def test_conversations_sqlite_parity():
    core = both_ddl(_CONVERSATIONS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_CONVERSATIONS), (
        "\n--- Core SQLite ---\n" + core +
        "\n--- Live SQLite ---\n" + LIVE_SQLITE_CONVERSATIONS
    )
