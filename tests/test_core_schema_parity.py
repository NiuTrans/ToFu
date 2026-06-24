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
    # ── Wave 2 (2026-06) ──
    MESSAGE_QUEUE as _MESSAGE_QUEUE,
    SCHEDULED_TASKS as _SCHEDULED_TASKS,
    PROACTIVE_POLL_LOG as _PROACTIVE_POLL_LOG,
    TIMER_WATCHERS as _TIMER_WATCHERS,
    TIMER_POLL_LOG as _TIMER_POLL_LOG,
    SWARM_SESSIONS as _SWARM_SESSIONS,
    SWARM_AGENTS as _SWARM_AGENTS,
    ORCHESTRATION_RUNS as _ORCHESTRATION_RUNS,
    ORCHESTRATION_RUN_EVENTS as _ORCHESTRATION_RUN_EVENTS,
    OPTIMIZER_PROPOSALS as _OPTIMIZER_PROPOSALS,
    OPTIMIZER_ACTION_LOG as _OPTIMIZER_ACTION_LOG,
    RATE_LIMIT_EVENTS as _RATE_LIMIT_EVENTS,
    ERROR_RESOLUTIONS as _ERROR_RESOLUTIONS,
    TENANT_USERS as _TENANT_USERS,
    BILLING_LEDGER as _BILLING_LEDGER,
    BILLING_WALLETS as _BILLING_WALLETS,
    BILLING_REDEEM_CODES as _BILLING_REDEEM_CODES,
    BILLING_PAYMENTS as _BILLING_PAYMENTS,
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
        meta TEXT NOT NULL DEFAULT '',
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
        meta TEXT NOT NULL DEFAULT '',
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



# ═══════════════════════════════════════════════════════════════════════
#  Wave 2 (2026-06) — the remaining hand-DDL tables.
#  Live DDL copied verbatim from _schema_pg.py / _schema_sqlite.py.
# ═══════════════════════════════════════════════════════════════════════

# message_queue — pending message queue, single PK.
LIVE_PG_MESSAGE_QUEUE = """
    CREATE TABLE message_queue (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        config TEXT NOT NULL DEFAULT '{}',
        position INTEGER NOT NULL DEFAULT 1,
        kind TEXT NOT NULL DEFAULT 'real',
        priority INTEGER NOT NULL DEFAULT 100,
        created_at BIGINT NOT NULL
    )
"""
LIVE_SQLITE_MESSAGE_QUEUE = """
    CREATE TABLE message_queue (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        config TEXT NOT NULL DEFAULT '{}',
        position INTEGER NOT NULL DEFAULT 1,
        kind TEXT NOT NULL DEFAULT 'real',
        priority INTEGER NOT NULL DEFAULT 100,
        created_at INTEGER NOT NULL
    )
"""

# scheduled_tasks — cron/agent registry, single PK; BOOLEAN flags + nullable cols.
LIVE_PG_SCHEDULED_TASKS = """
    CREATE TABLE scheduled_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        schedule TEXT NOT NULL,
        task_type TEXT NOT NULL DEFAULT 'command',
        command TEXT NOT NULL,
        description TEXT DEFAULT '',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        notify_on_failure BOOLEAN NOT NULL DEFAULT TRUE,
        notify_on_success BOOLEAN NOT NULL DEFAULT FALSE,
        max_runtime INTEGER NOT NULL DEFAULT 300,
        last_run TEXT,
        last_result TEXT,
        last_status TEXT DEFAULT 'never',
        run_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        target_conv_id TEXT DEFAULT '',
        source_conv_id TEXT DEFAULT '',
        tools_config TEXT DEFAULT '{}',
        poll_count INTEGER NOT NULL DEFAULT 0,
        last_poll_at TEXT DEFAULT '',
        last_poll_decision TEXT DEFAULT '',
        last_poll_reason TEXT DEFAULT '',
        last_execution_at TEXT DEFAULT '',
        last_execution_task_id TEXT DEFAULT '',
        last_execution_status TEXT DEFAULT '',
        execution_count INTEGER NOT NULL DEFAULT 0,
        max_executions INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT DEFAULT ''
    )
"""
LIVE_SQLITE_SCHEDULED_TASKS = """
    CREATE TABLE scheduled_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        schedule TEXT NOT NULL,
        task_type TEXT NOT NULL DEFAULT 'command',
        command TEXT NOT NULL,
        description TEXT DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        notify_on_failure INTEGER NOT NULL DEFAULT 1,
        notify_on_success INTEGER NOT NULL DEFAULT 0,
        max_runtime INTEGER NOT NULL DEFAULT 300,
        last_run TEXT,
        last_result TEXT,
        last_status TEXT DEFAULT 'never',
        run_count INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        target_conv_id TEXT DEFAULT '',
        source_conv_id TEXT DEFAULT '',
        tools_config TEXT DEFAULT '{}',
        poll_count INTEGER NOT NULL DEFAULT 0,
        last_poll_at TEXT DEFAULT '',
        last_poll_decision TEXT DEFAULT '',
        last_poll_reason TEXT DEFAULT '',
        last_execution_at TEXT DEFAULT '',
        last_execution_task_id TEXT DEFAULT '',
        last_execution_status TEXT DEFAULT '',
        execution_count INTEGER NOT NULL DEFAULT 0,
        max_executions INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT DEFAULT ''
    )
"""

# proactive_poll_log — append-only, autoincrement PK.
LIVE_PG_PROACTIVE_POLL_LOG = """
    CREATE TABLE proactive_poll_log (
        id SERIAL PRIMARY KEY,
        task_id TEXT NOT NULL,
        poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'skip',
        reason TEXT NOT NULL DEFAULT '',
        status_snapshot TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        tokens_used INTEGER NOT NULL DEFAULT 0,
        execution_task_id TEXT DEFAULT ''
    )
"""
LIVE_SQLITE_PROACTIVE_POLL_LOG = """
    CREATE TABLE proactive_poll_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'skip',
        reason TEXT NOT NULL DEFAULT '',
        status_snapshot TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        tokens_used INTEGER NOT NULL DEFAULT 0,
        execution_task_id TEXT DEFAULT ''
    )
"""

# timer_watchers — durable watchers, single PK.
LIVE_PG_TIMER_WATCHERS = """
    CREATE TABLE timer_watchers (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        source_task_id TEXT NOT NULL DEFAULT '',
        check_instruction TEXT NOT NULL,
        check_command TEXT NOT NULL DEFAULT '',
        continuation_message TEXT NOT NULL,
        poll_interval INTEGER NOT NULL DEFAULT 60,
        max_polls INTEGER NOT NULL DEFAULT 120,
        poll_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        tools_config TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        triggered_at TEXT DEFAULT '',
        cancelled_at TEXT DEFAULT '',
        execution_task_id TEXT DEFAULT '',
        last_poll_at TEXT DEFAULT '',
        last_poll_decision TEXT DEFAULT '',
        last_poll_reason TEXT DEFAULT ''
    )
"""
LIVE_SQLITE_TIMER_WATCHERS = LIVE_PG_TIMER_WATCHERS  # identical (all TEXT/INTEGER)

# timer_poll_log — append-only, autoincrement PK.
LIVE_PG_TIMER_POLL_LOG = """
    CREATE TABLE timer_poll_log (
        id SERIAL PRIMARY KEY,
        timer_id TEXT NOT NULL,
        poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'wait',
        reason TEXT NOT NULL DEFAULT '',
        check_output TEXT NOT NULL DEFAULT '',
        tokens_used INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        poll_id TEXT NOT NULL DEFAULT '',
        raw_output TEXT NOT NULL DEFAULT ''
    )
"""
LIVE_SQLITE_TIMER_POLL_LOG = """
    CREATE TABLE timer_poll_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timer_id TEXT NOT NULL,
        poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'wait',
        reason TEXT NOT NULL DEFAULT '',
        check_output TEXT NOT NULL DEFAULT '',
        tokens_used INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        poll_id TEXT NOT NULL DEFAULT '',
        raw_output TEXT NOT NULL DEFAULT ''
    )
"""

# swarm_sessions — single PK, bigint timestamps.
LIVE_PG_SWARM_SESSIONS = """
    CREATE TABLE swarm_sessions (
        swarm_key TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running',
        specs_json TEXT NOT NULL DEFAULT '[]',
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0
    )
"""
LIVE_SQLITE_SWARM_SESSIONS = """
    CREATE TABLE swarm_sessions (
        swarm_key TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running',
        specs_json TEXT NOT NULL DEFAULT '[]',
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0
    )
"""

# swarm_agents — composite PK; delivered is a plain INTEGER flag on both backends.
LIVE_PG_SWARM_AGENTS = """
    CREATE TABLE swarm_agents (
        swarm_key TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        objective TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        messages_json TEXT NOT NULL DEFAULT '[]',
        result_json TEXT NOT NULL DEFAULT '{}',
        rounds_used INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (swarm_key, agent_id)
    )
"""
LIVE_SQLITE_SWARM_AGENTS = """
    CREATE TABLE swarm_agents (
        swarm_key TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        objective TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        messages_json TEXT NOT NULL DEFAULT '[]',
        result_json TEXT NOT NULL DEFAULT '{}',
        rounds_used INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (swarm_key, agent_id)
    )
"""

# orchestration_runs — single PK, bigint timestamps.
LIVE_PG_ORCHESTRATION_RUNS = """
    CREATE TABLE orchestration_runs (
        id TEXT PRIMARY KEY,
        orch_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        definition TEXT NOT NULL DEFAULT '{}',
        input TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        final TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0,
        finished_at BIGINT NOT NULL DEFAULT 0
    )
"""
LIVE_SQLITE_ORCHESTRATION_RUNS = """
    CREATE TABLE orchestration_runs (
        id TEXT PRIMARY KEY,
        orch_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        definition TEXT NOT NULL DEFAULT '{}',
        input TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        final TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0,
        finished_at INTEGER NOT NULL DEFAULT 0
    )
"""

# orchestration_run_events — composite PK (run_id, seq).
LIVE_PG_ORCHESTRATION_RUN_EVENTS = """
    CREATE TABLE orchestration_run_events (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        node_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        ts BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, seq)
    )
"""
LIVE_SQLITE_ORCHESTRATION_RUN_EVENTS = """
    CREATE TABLE orchestration_run_events (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        node_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, seq)
    )
"""

# optimizer_proposals — single PK; confidence is DOUBLE PRECISION/REAL.
LIVE_PG_OPTIMIZER_PROPOSALS = """
    CREATE TABLE optimizer_proposals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_args TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'low',
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        evidence TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_review',
        status_reason TEXT NOT NULL DEFAULT ''
    )
"""
LIVE_SQLITE_OPTIMIZER_PROPOSALS = """
    CREATE TABLE optimizer_proposals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_args TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'low',
        confidence REAL NOT NULL DEFAULT 0,
        evidence TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_review',
        status_reason TEXT NOT NULL DEFAULT ''
    )
"""

# optimizer_action_log — single PK, all TEXT.
LIVE_OPTIMIZER_ACTION_LOG = """
    CREATE TABLE optimizer_action_log (
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        pre_metric TEXT NOT NULL DEFAULT '',
        outcome_metric TEXT NOT NULL DEFAULT '',
        outcome_recorded_at TEXT NOT NULL DEFAULT '',
        reverted_at TEXT NOT NULL DEFAULT '',
        revert_reason TEXT NOT NULL DEFAULT ''
    )
"""

# rate_limit_events — BIGSERIAL/INTEGER AUTOINCREMENT PK.
LIVE_PG_RATE_LIMIT_EVENTS = """
    CREATE TABLE rate_limit_events (
        id BIGSERIAL PRIMARY KEY,
        endpoint TEXT NOT NULL,
        ip TEXT NOT NULL,
        ts_ms BIGINT NOT NULL
    )
"""
LIVE_SQLITE_RATE_LIMIT_EVENTS = """
    CREATE TABLE rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT NOT NULL,
        ip TEXT NOT NULL,
        ts_ms INTEGER NOT NULL
    )
"""

# error_resolutions — PG-ONLY (no SQLite CREATE in live bootstrap).
LIVE_PG_ERROR_RESOLUTIONS = """
    CREATE TABLE error_resolutions (
        fingerprint TEXT PRIMARY KEY,
        logger_name TEXT NOT NULL DEFAULT '',
        sample_message TEXT NOT NULL DEFAULT '',
        resolved_by TEXT NOT NULL DEFAULT '',
        ticket TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        resolved_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )
"""

# tenant_users — multi-tenant user table; email inline UNIQUE; email_verified
# is INTEGER on BOTH backends.
LIVE_PG_TENANT_USERS = """
    CREATE TABLE tenant_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at BIGINT NOT NULL,
        last_login_at BIGINT NOT NULL DEFAULT 0,
        email_verified INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
"""
LIVE_SQLITE_TENANT_USERS = """
    CREATE TABLE tenant_users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL,
        last_login_at INTEGER NOT NULL DEFAULT 0,
        email_verified INTEGER NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
"""

# billing_ledger — append-only, single PK; bigint micro amounts.
LIVE_PG_BILLING_LEDGER = """
    CREATE TABLE billing_ledger (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        ts BIGINT NOT NULL,
        amount_micro BIGINT NOT NULL,
        kind TEXT NOT NULL,
        ref_type TEXT NOT NULL DEFAULT '',
        ref_id TEXT NOT NULL DEFAULT '',
        balance_after_micro BIGINT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )
"""
LIVE_SQLITE_BILLING_LEDGER = """
    CREATE TABLE billing_ledger (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        ts INTEGER NOT NULL,
        amount_micro INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ref_type TEXT NOT NULL DEFAULT '',
        ref_id TEXT NOT NULL DEFAULT '',
        balance_after_micro INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )
"""

# billing_wallets — single PK (user_id).
LIVE_PG_BILLING_WALLETS = """
    CREATE TABLE billing_wallets (
        user_id TEXT PRIMARY KEY,
        balance_micro BIGINT NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'CREDIT',
        low_balance_alert_micro BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL
    )
"""
LIVE_SQLITE_BILLING_WALLETS = """
    CREATE TABLE billing_wallets (
        user_id TEXT PRIMARY KEY,
        balance_micro INTEGER NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'CREDIT',
        low_balance_alert_micro INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    )
"""

# billing_redeem_codes — single PK (code).
LIVE_PG_BILLING_REDEEM_CODES = """
    CREATE TABLE billing_redeem_codes (
        code TEXT PRIMARY KEY,
        amount_micro BIGINT NOT NULL,
        batch TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL DEFAULT 0,
        redeemed_by TEXT NOT NULL DEFAULT '',
        redeemed_at BIGINT NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT ''
    )
"""
LIVE_SQLITE_BILLING_REDEEM_CODES = """
    CREATE TABLE billing_redeem_codes (
        code TEXT PRIMARY KEY,
        amount_micro INTEGER NOT NULL,
        batch TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL DEFAULT 0,
        redeemed_by TEXT NOT NULL DEFAULT '',
        redeemed_at INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT ''
    )
"""

# billing_payments — single PK.
LIVE_PG_BILLING_PAYMENTS = """
    CREATE TABLE billing_payments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        amount_minor BIGINT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        credit_micro BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at BIGINT NOT NULL,
        settled_at BIGINT NOT NULL DEFAULT 0,
        raw TEXT NOT NULL DEFAULT '{}'
    )
"""
LIVE_SQLITE_BILLING_PAYMENTS = """
    CREATE TABLE billing_payments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        credit_micro INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        settled_at INTEGER NOT NULL DEFAULT 0,
        raw TEXT NOT NULL DEFAULT '{}'
    )
"""


def _assert_parity(table, live_pg, live_sqlite):
    """Compile both backends and compare against the live DDL constants."""
    core = both_ddl(table)
    assert _norm(core["pg"]) == _norm(live_pg), (
        "\n--- Core PG ---\n" + core["pg"] + "\n--- Live PG ---\n" + live_pg)
    assert _norm(core["sqlite"]) == _norm(live_sqlite), (
        "\n--- Core SQLite ---\n" + core["sqlite"] + "\n--- Live SQLite ---\n" + live_sqlite)


def test_message_queue_parity():
    _assert_parity(_MESSAGE_QUEUE, LIVE_PG_MESSAGE_QUEUE, LIVE_SQLITE_MESSAGE_QUEUE)


def test_scheduled_tasks_parity():
    _assert_parity(_SCHEDULED_TASKS, LIVE_PG_SCHEDULED_TASKS, LIVE_SQLITE_SCHEDULED_TASKS)


def test_proactive_poll_log_parity():
    _assert_parity(_PROACTIVE_POLL_LOG, LIVE_PG_PROACTIVE_POLL_LOG, LIVE_SQLITE_PROACTIVE_POLL_LOG)


def test_timer_watchers_parity():
    _assert_parity(_TIMER_WATCHERS, LIVE_PG_TIMER_WATCHERS, LIVE_SQLITE_TIMER_WATCHERS)


def test_timer_poll_log_parity():
    _assert_parity(_TIMER_POLL_LOG, LIVE_PG_TIMER_POLL_LOG, LIVE_SQLITE_TIMER_POLL_LOG)


def test_swarm_sessions_parity():
    _assert_parity(_SWARM_SESSIONS, LIVE_PG_SWARM_SESSIONS, LIVE_SQLITE_SWARM_SESSIONS)


def test_swarm_agents_parity():
    _assert_parity(_SWARM_AGENTS, LIVE_PG_SWARM_AGENTS, LIVE_SQLITE_SWARM_AGENTS)


def test_orchestration_runs_parity():
    _assert_parity(_ORCHESTRATION_RUNS, LIVE_PG_ORCHESTRATION_RUNS, LIVE_SQLITE_ORCHESTRATION_RUNS)


def test_orchestration_run_events_parity():
    _assert_parity(_ORCHESTRATION_RUN_EVENTS, LIVE_PG_ORCHESTRATION_RUN_EVENTS,
                   LIVE_SQLITE_ORCHESTRATION_RUN_EVENTS)


def test_optimizer_proposals_parity():
    _assert_parity(_OPTIMIZER_PROPOSALS, LIVE_PG_OPTIMIZER_PROPOSALS, LIVE_SQLITE_OPTIMIZER_PROPOSALS)


def test_optimizer_action_log_parity():
    _assert_parity(_OPTIMIZER_ACTION_LOG, LIVE_OPTIMIZER_ACTION_LOG, LIVE_OPTIMIZER_ACTION_LOG)


def test_rate_limit_events_parity():
    _assert_parity(_RATE_LIMIT_EVENTS, LIVE_PG_RATE_LIMIT_EVENTS, LIVE_SQLITE_RATE_LIMIT_EVENTS)


def test_error_resolutions_pg_parity():
    # PG-only table; only the PG DDL exists in the live bootstrap.
    core = both_ddl(_ERROR_RESOLUTIONS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_ERROR_RESOLUTIONS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_ERROR_RESOLUTIONS)


def test_tenant_users_parity():
    _assert_parity(_TENANT_USERS, LIVE_PG_TENANT_USERS, LIVE_SQLITE_TENANT_USERS)


def test_billing_ledger_parity():
    _assert_parity(_BILLING_LEDGER, LIVE_PG_BILLING_LEDGER, LIVE_SQLITE_BILLING_LEDGER)


def test_billing_wallets_parity():
    _assert_parity(_BILLING_WALLETS, LIVE_PG_BILLING_WALLETS, LIVE_SQLITE_BILLING_WALLETS)


def test_billing_redeem_codes_parity():
    _assert_parity(_BILLING_REDEEM_CODES, LIVE_PG_BILLING_REDEEM_CODES, LIVE_SQLITE_BILLING_REDEEM_CODES)


def test_billing_payments_parity():
    _assert_parity(_BILLING_PAYMENTS, LIVE_PG_BILLING_PAYMENTS, LIVE_SQLITE_BILLING_PAYMENTS)
