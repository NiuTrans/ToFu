"""Database schema initialization — SQLite backend.

CREATE TABLE, migrations, FTS5.
Native SQLite DDL — no translation layer needed.
"""

import time

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Version Cache — Skip redundant DDL on subsequent startups
# ═══════════════════════════════════════════════════════════════════════

_SCHEMA_VERSION = 28  # Increment when tables/columns/indexes change


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


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

def _backfill_search_fts(conn):
    """One-time migration: populate FTS5 table from existing conversations."""
    import json
    from lib.conversations import build_search_text

    cur = conn._conn.cursor()
    cur.execute("SELECT id, messages FROM conversations WHERE search_text = '' AND msg_count > 0")
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        row_id = row[0]
        messages_raw = row[1]
        try:
            messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[DB] Failed to parse messages for conv %s: %s', row_id, e)
            continue
        st = build_search_text(messages)
        if st:
            cur.execute("UPDATE conversations SET search_text = ? WHERE id = ?", (st, row_id))
            # Also insert into FTS5
            cur.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (st, row_id)
            )
            updated += 1
    conn._conn.commit()
    logger.info('[DB] Backfilled search_text for %d/%d conversations', updated, len(rows))


def _init_chat_schema(conn):
    """Create chat domain tables and run migrations."""
    cur = conn._conn.cursor()

    # users: migrated onto Core (lib/database/_core_schema.py). Auto-increment
    # PK (INTEGER AUTOINCREMENT) + TEXT created_at DEFAULT (datetime('now')).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import USERS, create_if_absent
    create_if_absent(conn, USERS, table_exists=_table_exists)

    # conversations: base table migrated onto Core (lib/database/_core_schema.py,
    # OPTION B). Core owns all shared base columns including search_text.
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). SQLite full-text search uses the separate FTS5
    # conversations_fts table (created elsewhere), not a tsvector column.
    # See tests/test_core_schema_parity.py.
    from lib.database._core_schema import CONVERSATIONS, create_if_absent
    create_if_absent(conn, CONVERSATIONS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_meta ON conversations(user_id, updated_at DESC, id, title, msg_count, created_at)')

    # task_results + task_events: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). Indexes stay below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        TASK_RESULTS, TASK_EVENTS, create_if_absent,
    )
    create_if_absent(conn, TASK_RESULTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_conv ON task_results(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_created ON task_results(created_at)')

    # ── task_events: persisted SSE event log (durable Last-Event-ID resumption) ──
    # Replaces in-memory task['events'] for cross-restart and post-cleanup
    # replay. event_id is monotonic per task, mirrored in the SSE 'id:' field.
    create_if_absent(conn, TASK_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_events_ts ON task_events(ts_ms)')

    # ── chat_artifacts: renderable reports promoted out of chat (md/html/svg) ──
    # First-class storage for "report-shaped" outputs so they survive
    # compaction, can be re-opened in the right-side panel by stable URL,
    # and can be versioned / pinned independently of the conversation row.
    # See docs/ARCHITECTURE.md §Artifacts subsystem.
    # chat_artifacts: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). Indexes stay below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import CHAT_ARTIFACTS, create_if_absent
    create_if_absent(conn, CHAT_ARTIFACTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_conv ON chat_artifacts(conv_id, created_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_msg ON chat_artifacts(conv_id, msg_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_sha ON chat_artifacts(conv_id, content_sha256)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_task ON chat_artifacts(task_id)')

    # transcript_archive: migrated onto Core (lib/database/_core_schema.py).
    # Auto-increment PK (INTEGER AUTOINCREMENT) + per-dialect epoch_now()
    # default. _table_exists guard REQUIRED on SQLite (bare execute, Core DDL
    # has no IF NOT EXISTS). The ALTER migration loop below stays (upgrade
    # path). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import TRANSCRIPT_ARCHIVE, create_if_absent
    create_if_absent(conn, TRANSCRIPT_ARCHIVE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ta_conv ON transcript_archive(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ta_conv_created ON transcript_archive(conv_id, created_at DESC)')
    # Migrations — extend existing transcript_archive with metadata columns
    for col, sql in {
        'trigger':       "ALTER TABLE transcript_archive ADD COLUMN trigger TEXT NOT NULL DEFAULT 'force'",
        'task_id':       "ALTER TABLE transcript_archive ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
        'round_num':     "ALTER TABLE transcript_archive ADD COLUMN round_num INTEGER NOT NULL DEFAULT 0",
        'model':         "ALTER TABLE transcript_archive ADD COLUMN model TEXT NOT NULL DEFAULT ''",
        'tokens_before': "ALTER TABLE transcript_archive ADD COLUMN tokens_before INTEGER NOT NULL DEFAULT 0",
        'tokens_after':  "ALTER TABLE transcript_archive ADD COLUMN tokens_after INTEGER NOT NULL DEFAULT 0",
        'msgs_before':   "ALTER TABLE transcript_archive ADD COLUMN msgs_before INTEGER NOT NULL DEFAULT 0",
        'msgs_after':    "ALTER TABLE transcript_archive ADD COLUMN msgs_after INTEGER NOT NULL DEFAULT 0",
        'reason':        "ALTER TABLE transcript_archive ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'transcript_archive', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to transcript_archive', col)

    # Migrations — check columns
    for col, sql in {
        'search_results': "ALTER TABLE task_results ADD COLUMN search_results TEXT",
        'metadata':       "ALTER TABLE task_results ADD COLUMN metadata TEXT",
    }.items():
        if not _column_exists(conn, 'task_results', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to task_results', col)

    # ── Migration: rename search_rounds → tool_rounds ──
    if _column_exists(conn, 'task_results', 'search_rounds') and not _column_exists(conn, 'task_results', 'tool_rounds'):
        cur.execute('ALTER TABLE task_results RENAME COLUMN search_rounds TO tool_rounds')
        logger.info('[DB] Migration: renamed column search_rounds → tool_rounds in task_results')
    elif not _column_exists(conn, 'task_results', 'tool_rounds'):
        cur.execute('ALTER TABLE task_results ADD COLUMN tool_rounds TEXT')
        logger.info('[DB] Migration: added column tool_rounds to task_results')

    # ── Migration: rename searchRounds → toolRounds inside messages JSON ──
    try:
        cur.execute("""
            UPDATE conversations
            SET messages = REPLACE(messages, '"searchRounds":', '"toolRounds":')
            WHERE messages LIKE '%"searchRounds":%'
        """)
        _migrated_count = cur.rowcount
        if _migrated_count > 0:
            logger.info('[DB] Migration: renamed searchRounds → toolRounds in %d conversation(s)', _migrated_count)
        conn._conn.commit()
    except Exception as e:
        logger.warning('[DB] Migration: searchRounds→toolRounds failed (non-fatal): %s', e)

    for col, sql in {
        'settings':     "ALTER TABLE conversations ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'",
        'msg_count':    "ALTER TABLE conversations ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0",
        'search_text':  "ALTER TABLE conversations ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'conversations', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to conversations', col)

    # ── FTS5 virtual table for full-text search ──
    # SELF-CONTENT (NOT content=''). A contentless FTS5 table cannot DELETE
    # a row, so when a conversation is edited the OLD tokens can never be
    # retracted from the index — searching the pre-edit text keeps matching
    # (a stale hit). A self-content table stores the indexed text, so
    # update_conversation_fts() can DELETE-then-INSERT by rowid and the old
    # terms actually disappear. The minor extra storage is the search_text
    # we already keep in the conversations column anyway.
    cur.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts
        USING fts5(search_text, tokenize='unicode61')
    ''')

    # ── Migrate legacy CONTENTLESS tables (content='') → self-content ──
    # Older installs created conversations_fts with content='' and cannot
    # retract stale terms on edit. Detect that via the stored DDL and rebuild
    # from the authoritative conversations.search_text column.
    try:
        cur.execute("SELECT sql FROM sqlite_master WHERE name='conversations_fts'")
        _fts_ddl_row = cur.fetchone()
        _fts_ddl = (_fts_ddl_row[0] if _fts_ddl_row else '') or ''
        if "content=''" in _fts_ddl.replace('"', "'").replace(' ', ''):
            logger.info('[DB] Rebuilding contentless conversations_fts → self-content')
            cur.execute('DROP TABLE conversations_fts')
            cur.execute('''
                CREATE VIRTUAL TABLE conversations_fts
                USING fts5(search_text, tokenize='unicode61')
            ''')
            cur.execute("""
                INSERT INTO conversations_fts (rowid, search_text)
                SELECT rowid, search_text FROM conversations WHERE search_text != ''
            """)
    except Exception as e:
        logger.debug('[DB] conversations_fts contentless-migration skipped: %s', e)

    # ── Sync FTS from existing search_text if FTS is empty ──
    cur.execute("SELECT count(*) FROM conversations_fts")
    fts_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM conversations WHERE search_text != ''")
    conv_count = cur.fetchone()[0]
    if fts_count < conv_count and conv_count > 0:
        logger.info('[DB] Syncing FTS5 index (%d in FTS vs %d with search_text)...', fts_count, conv_count)
        cur.execute("""
            INSERT OR REPLACE INTO conversations_fts (rowid, search_text)
            SELECT rowid, search_text FROM conversations WHERE search_text != ''
        """)
        logger.info('[DB] FTS5 index synced')

    # ── Backfill search_text for existing conversations that have empty search_text ──
    cur.execute("SELECT count(*) FROM conversations WHERE search_text = '' AND msg_count > 0")
    backfill_count = cur.fetchone()[0]
    if backfill_count > 0:
        logger.info('[DB] Backfilling search_text for %d conversations...', backfill_count)
        _backfill_search_fts(conn)

    # ── Message queue: migrated onto Core. ──
    from lib.database._core_schema import (
        MESSAGE_QUEUE, create_if_absent,
    )
    create_if_absent(conn, MESSAGE_QUEUE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
    # ── Migration (v26): unified priority turn-source queue columns ──
    for col, sql in {
        'kind':     "ALTER TABLE message_queue ADD COLUMN kind TEXT NOT NULL DEFAULT 'real'",
        'priority': "ALTER TABLE message_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 100",
    }.items():
        if not _column_exists(conn, 'message_queue', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to message_queue', col)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv_prio ON message_queue(conv_id, priority, position)')

    # paper_reports: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_REPORTS, create_if_absent
    create_if_absent(conn, PAPER_REPORTS, table_exists=_table_exists)
    # Migration: add `meta` (JSON model+usage+cost finish-tag) to existing DBs.
    if not _column_exists(conn, 'paper_reports', 'meta'):
        cur.execute("ALTER TABLE paper_reports ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column meta to paper_reports')

    # ── Paper library: server-side bookshelf (shared across browsers) ──
    # Stores one row per paper the user has loaded; the PDF bytes live under
    # uploads/papers/<pdf_filename>, reports in paper_reports, images on disk.
    # paper_library: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_LIBRARY, create_if_absent
    create_if_absent(conn, PAPER_LIBRARY, table_exists=_table_exists)
    # ── Daily cost cache: pre-aggregated per-day LLM costs (avoids full
    # table scans on every calendar render).  date is 'YYYY-MM-DD' local time.
    # conversations_json stores the per-conv breakdown for drill-down.
    # Past days are cached forever (messages are immutable); today is always
    # recomputed live.  Invalidated on conv delete / message delete.
    # daily_cost_cache: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). Index stays below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import DAILY_COST_CACHE, create_if_absent
    create_if_absent(conn, DAILY_COST_CACHE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_daily_cost_user_date ON daily_cost_cache(user_id, date)')

    cur.execute('CREATE INDEX IF NOT EXISTS idx_paper_lib_user ON paper_library(user_id, updated_at DESC)')

    # ── Paper translations: persistent cache for Babel-mode whole-paper
    # translations (server-owned task; mirrors paper_reports). lang is the
    # target language code ('zh', 'en', 'ja', …).
    # paper_translations: migrated onto Core (lib/database/_core_schema.py).
    # _table_exists guard REQUIRED on SQLite (bare execute, Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_TRANSLATIONS, create_if_absent
    create_if_absent(conn, PAPER_TRANSLATIONS, table_exists=_table_exists)

    # Seed default user
    cur.execute("""
        INSERT OR IGNORE INTO users (id, username, display_name, password_hash)
        VALUES (1, 'default', 'User', '')
    """)

    conn._conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    # pricing_cache + recent_projects: migrated onto Core definitions
    # (lib/database/_core_schema.py). _table_exists guard is REQUIRED on the
    # SQLite path (bare execute, no savepoint tolerance; Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        PRICING_CACHE, RECENT_PROJECTS, SCHEMA_META, create_if_absent,
    )
    # schema_meta is core-owned and holds the fast-startup version cache; it
    # MUST exist independently of the (optional) trading domain.
    create_if_absent(conn, SCHEMA_META, table_exists=_table_exists)
    create_if_absent(conn, PRICING_CACHE, table_exists=_table_exists)
    create_if_absent(conn, RECENT_PROJECTS, table_exists=_table_exists)

    # scheduled_tasks + proactive_poll_log: migrated onto Core. The post-create
    # ALTERs below stay (upgrade-only; Core's create only fires on fresh installs).
    from lib.database._core_schema import (
        SCHEDULED_TASKS, PROACTIVE_POLL_LOG, create_if_absent,
    )
    create_if_absent(conn, SCHEDULED_TASKS, table_exists=_table_exists)
    create_if_absent(conn, PROACTIVE_POLL_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_poll_log_task ON proactive_poll_log(task_id, poll_time DESC)')

    # ── Migration: add proactive agent columns ──
    _proactive_cols = [
        ('target_conv_id', "TEXT DEFAULT ''"),
        ('source_conv_id', "TEXT DEFAULT ''"),
        ('tools_config', "TEXT DEFAULT '{}'"),
        ('poll_count', "INTEGER NOT NULL DEFAULT 0"),
        ('last_poll_at', "TEXT DEFAULT ''"),
        ('last_poll_decision', "TEXT DEFAULT ''"),
        ('last_poll_reason', "TEXT DEFAULT ''"),
        ('last_execution_at', "TEXT DEFAULT ''"),
        ('last_execution_task_id', "TEXT DEFAULT ''"),
        ('last_execution_status', "TEXT DEFAULT ''"),
        ('execution_count', "INTEGER NOT NULL DEFAULT 0"),
        ('max_executions', "INTEGER NOT NULL DEFAULT 0"),
        ('expires_at', "TEXT DEFAULT ''"),
        # Defensive: these are created by the canonical CREATE TABLE above,
        # but older installs may have had a minimal version.
        ('description', "TEXT DEFAULT ''"),
        ('notify_on_failure', "INTEGER NOT NULL DEFAULT 1"),
        ('notify_on_success', "INTEGER NOT NULL DEFAULT 0"),
        ('max_runtime', "INTEGER NOT NULL DEFAULT 300"),
        ('last_result', "TEXT"),
        ('run_count', "INTEGER NOT NULL DEFAULT 0"),
        ('fail_count', "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in _proactive_cols:
        if not _column_exists(conn, 'scheduled_tasks', col_name):
            cur.execute(f'ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}')
            logger.info('[DB] Migration: added column %s to scheduled_tasks', col_name)

    # Timer Watcher tables
    # timer_watchers + timer_poll_log: migrated onto Core.
    from lib.database._core_schema import (
        TIMER_WATCHERS, TIMER_POLL_LOG, create_if_absent,
    )
    create_if_absent(conn, TIMER_WATCHERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_status ON timer_watchers(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_conv ON timer_watchers(conv_id)')
    create_if_absent(conn, TIMER_POLL_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_poll_log ON timer_poll_log(timer_id, poll_time DESC)')
    # Migration (v25): record which model the poll LLM resolved to.
    if not _column_exists(conn, 'timer_poll_log', 'model'):
        cur.execute("ALTER TABLE timer_poll_log ADD COLUMN model TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column model to timer_poll_log')
    # Migration (v27): stable per-poll id + the raw LLM output (so a
    # parse-failure poll can be located by id and its raw decision text
    # survives refresh/restart for diagnosis).
    for _tpl_col, _tpl_sql in {
        'poll_id':    "ALTER TABLE timer_poll_log ADD COLUMN poll_id TEXT NOT NULL DEFAULT ''",
        'raw_output': "ALTER TABLE timer_poll_log ADD COLUMN raw_output TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'timer_poll_log', _tpl_col):
            cur.execute(_tpl_sql)
            logger.info('[DB] Migration: added column %s to timer_poll_log', _tpl_col)

    # ── Swarm durable state (see lib/swarm/persistence.py) ──
    # Persists conversation-scoped swarm sessions and per-agent message
    # checkpoints so an in-flight sub-agent can be rehydrated and resumed
    # at round granularity after a server restart. swarm_key == convId.
    # swarm_sessions + swarm_agents: migrated onto Core.
    from lib.database._core_schema import (
        SWARM_SESSIONS, SWARM_AGENTS, create_if_absent,
    )
    create_if_absent(conn, SWARM_SESSIONS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_swarm_sessions_status ON swarm_sessions(status)')
    create_if_absent(conn, SWARM_AGENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_swarm_agents_key ON swarm_agents(swarm_key)')

    # ── Orchestration run instances (see lib/orchestration_runs.py) ──
    # A run instance is a durable, reopenable execution of a flow TEMPLATE.
    # Unlike the in-memory TaskRuntime (TTL-purged), these survive restarts:
    #   orchestration_runs        — one row per run; pins a definition SNAPSHOT
    #                               so editing the template never mutates a run.
    #   orchestration_run_events  — append-only mirror of the engine event
    #                               stream for durable cursor replay. seq is
    #                               monotonic per run (matches TaskRuntime seq).
    # 'paused' is an instance-only status (blocked on a human gate); the engine
    # thread's TaskRuntime status stays 'running' while it waits.
    # orchestration_runs + orchestration_run_events: migrated onto Core.
    from lib.database._core_schema import (
        ORCHESTRATION_RUNS, ORCHESTRATION_RUN_EVENTS, create_if_absent,
    )
    create_if_absent(conn, ORCHESTRATION_RUNS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_status ON orchestration_runs(status, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_orch ON orchestration_runs(orch_id, created_at DESC)')
    create_if_absent(conn, ORCHESTRATION_RUN_EVENTS, table_exists=_table_exists)

    # ── Daily Optimizer tables (see lib/optimizer/) ──
    # optimizer_proposals + optimizer_action_log: migrated onto Core.
    from lib.database._core_schema import (
        OPTIMIZER_PROPOSALS, OPTIMIZER_ACTION_LOG, create_if_absent,
    )
    create_if_absent(conn, OPTIMIZER_PROPOSALS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_created ON optimizer_proposals(created_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_status ON optimizer_proposals(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_action ON optimizer_proposals(action_type)')
    create_if_absent(conn, OPTIMIZER_ACTION_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_proposal ON optimizer_action_log(proposal_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_applied ON optimizer_action_log(applied_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_expires ON optimizer_action_log(expires_at)')

    # ── Rate-limit event log (PR3c / C7 step 2) ──
    # See the matching block in _schema_pg.py for design notes.
    # rate_limit_events: migrated onto Core.
    from lib.database._core_schema import RATE_LIMIT_EVENTS, create_if_absent
    create_if_absent(conn, RATE_LIMIT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_events(endpoint, ip, ts_ms)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limit_events(ts_ms)')

    # ─────────────────────────────────────────────────────────────────
    #  Billing / multi-tenant tables
    # ─────────────────────────────────────────────────────────────────
    # All amounts are stored in MICRO-CREDITS (1 credit = 1,000,000 micro)
    # to keep the math integer-only. 1 credit roughly equals US $0.001
    # — i.e. one US dollar = 1,000 credits = 1,000,000,000 micro. This
    # buys ~10⁹ resolution per dollar which is plenty for token billing.
    # Conversion to display currency happens only at presentation layer.
    # ``tenant_users`` is the multi-tenant relay user table. The existing
    # ``users`` table from the chat schema is a single-row stub for the
    # local UI's session — different shape, different purpose. Naming
    # collision avoided here on purpose.
    # tenant_users: migrated onto Core.
    from lib.database._core_schema import TENANT_USERS, create_if_absent
    create_if_absent(conn, TENANT_USERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(email)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_role ON tenant_users(role)')

    # The ledger is the SOURCE OF TRUTH for every credit movement.
    # Append-only — never UPDATE or DELETE rows. The wallet balance is a
    # denormalized cache derived from SUM(amount) over this table.
    # billing_ledger: migrated onto Core.
    from lib.database._core_schema import BILLING_LEDGER, create_if_absent
    create_if_absent(conn, BILLING_LEDGER, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_user_ts ON billing_ledger(user_id, ts DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_ref ON billing_ledger(ref_type, ref_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_kind ON billing_ledger(kind)')

    # billing_wallets + billing_redeem_codes: migrated onto Core.
    from lib.database._core_schema import (
        BILLING_WALLETS, BILLING_REDEEM_CODES, create_if_absent,
    )
    create_if_absent(conn, BILLING_WALLETS, table_exists=_table_exists)
    create_if_absent(conn, BILLING_REDEEM_CODES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_redeem_batch ON billing_redeem_codes(batch)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_redeem_redeemed ON billing_redeem_codes(redeemed_by)')

    # billing_payments: migrated onto Core.
    from lib.database._core_schema import BILLING_PAYMENTS, create_if_absent
    create_if_absent(conn, BILLING_PAYMENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_user ON billing_payments(user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_provider ON billing_payments(provider, provider_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_status ON billing_payments(status)')

    # Migration for existing api_keys store: add user_id column so a
    # multi-tenant deployment can attribute every key to a wallet.
    # Personal/private installs leave it empty — middleware treats
    # an empty user_id as "the local single-user account".
    if not _column_exists(conn, 'rate_limit_events', 'placeholder'):
        # The api_keys table doesn't live in SQLite — it's still a JSON
        # store at data/config/api_keys.json. The user_id field is added
        # to that JSON shape directly in lib/api_keys.py. Nothing to do
        # here; this comment prevents a future maintainer from re-adding
        # ALTER TABLE api_keys.
        pass

    conn._conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  init_db — top-level entry point
# ═══════════════════════════════════════════════════════════════════════

def init_db(_new_connection):
    """Initialize all database schemas.

    Uses a schema version cache to skip redundant DDL on subsequent startups.

    Args:
        _new_connection: callable that returns a SqliteConnection.
    """
    logger.info('[DB] Schema initialization started (SQLite)')

    conn = None
    try:
        conn = _new_connection()

        # ── Fast path: version AND optional-domain set both unchanged.
        #    The domain set is part of the cache key so enabling a new domain
        #    (e.g. trading) re-triggers its DDL instead of being skipped.
        from lib.database.schema_registry import active_domains
        t0 = time.monotonic()
        current_version = _get_schema_version(conn)
        current_domains = _get_schema_domains(conn)
        want_domains = ','.join(active_domains())
        if current_version == _SCHEMA_VERSION and current_domains == want_domains:
            elapsed = time.monotonic() - t0
            logger.info('[DB] Schema version %d + domains [%s] current — skipping '
                        'DDL (fast startup, checked in %.2fs)',
                        _SCHEMA_VERSION, want_domains, elapsed)
            return

        logger.info('[DB] Schema version %s → %d, domains [%s] → [%s] — running '
                    'full DDL migration', current_version, _SCHEMA_VERSION,
                    current_domains, want_domains)

        _init_chat_schema(conn)
        logger.info('[DB] Chat schema initialized')
        # system schema first — it creates the core-owned schema_meta table
        # the version/domain cache writes into below.
        _init_system_schema(conn)
        logger.info('[DB] System schema initialized')
        # Optional domains (e.g. trading) register their initializers via
        # lib/database/schema_registry.py — in-tree shim or tofu.schema plugin.
        from lib.database.schema_registry import run_registered
        run_registered(conn)

        _set_schema_version(conn, _SCHEMA_VERSION)
        _set_schema_domains(conn, active_domains())
        try:
            from lib.log import audit_log
            audit_log('db_schema_init', backend='sqlite', version=_SCHEMA_VERSION,
                      domains=want_domains, prev_version=current_version)
        except Exception as _ae:
            logger.debug('[DB] audit_log db_schema_init failed: %s', _ae)

        elapsed = time.monotonic() - t0
        logger.info('[DB] Schema initialization complete in %.1fs (version %d)',
                    elapsed, _SCHEMA_VERSION)
    except Exception as e:
        logger.error('[DB] Schema init failed: %s', e, exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as ce:
                logger.debug('[DB] Schema init conn close failed: %s', ce)
