"""Database schema initialization — PostgreSQL backend.

CREATE TABLE, migrations, version cache, tsvector search.
Uses native PostgreSQL DDL (SERIAL, JSONB, tsvector, pg_trgm).
"""

import time

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Schema Version Cache — Skip redundant DDL on subsequent startups
# ═══════════════════════════════════════════════════════════════════════

_SCHEMA_VERSION = 29  # Increment when tables/columns/indexes change


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


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

def _backfill_search_text(conn):
    """One-time migration: populate search_text and search_tsv for existing conversations.

    Reads messages JSON in batches, extracts plaintext via build_search_text(),
    and writes back.  Runs only once (when search_text column is newly added).
    """
    import json
    from lib.conversations import build_search_text

    cur = conn._conn.cursor()
    cur.execute("SELECT id, messages FROM conversations WHERE search_text = '' AND msg_count > 0")
    rows = cur.fetchall()
    updated = 0
    for row_id, messages_raw in rows:
        try:
            messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[DB] Failed to parse messages for conv %s: %s', row_id, e)
            continue
        st = build_search_text(messages)
        if st:
            cur.execute(
                "UPDATE conversations SET search_text = %s, "
                "search_tsv = to_tsvector('simple', left(%s, 50000)) WHERE id = %s",
                (st, st, row_id))
            updated += 1
    conn._conn.commit()
    logger.info('[DB] Backfilled search_text for %d/%d conversations', updated, len(rows))


def _backfill_search_tsv(conn):
    """One-time migration: populate search_tsv from existing search_text.

    Runs when the search_tsv column is added but search_text already exists.
    """
    cur = conn._conn.cursor()
    cur.execute(
        "UPDATE conversations SET search_tsv = to_tsvector('simple', left(search_text, 50000)) "
        "WHERE search_text != '' AND search_tsv IS NULL")
    count = cur.rowcount
    conn._conn.commit()
    logger.info('[DB] Backfilled search_tsv for %d conversations', count)


def _safe_create_table(cur, ddl):
    """Execute CREATE TABLE IF NOT EXISTS, tolerating pg_type conflicts.

    PostgreSQL auto-creates a composite type for each table.  If a prior
    init crashed after CREATE TABLE but before the schema version was
    persisted, re-running the same CREATE can hit:
        UniqueViolation on pg_type_typname_nsp_index
    because the type already exists even though IF NOT EXISTS should
    handle the table itself.  We wrap in a savepoint so one failure
    doesn't abort the entire transaction.
    """
    try:
        cur.execute('SAVEPOINT _safe_ddl')
        cur.execute(ddl)
        cur.execute('RELEASE SAVEPOINT _safe_ddl')
    except Exception as e:
        err_str = str(e)
        # Known harmless: table/type already exists from a previous partial init
        if 'already exists' in err_str or 'UniqueViolation' in type(e).__name__:
            cur.execute('ROLLBACK TO SAVEPOINT _safe_ddl')
            cur.execute('RELEASE SAVEPOINT _safe_ddl')
            logger.debug('[DB] Table already exists (tolerating): %.200s', err_str)
        else:
            cur.execute('ROLLBACK TO SAVEPOINT _safe_ddl')
            cur.execute('RELEASE SAVEPOINT _safe_ddl')
            raise


def _init_chat_schema(conn):
    """Create chat domain tables and run migrations."""
    cur = conn._conn.cursor()

    # users: migrated onto Core (lib/database/_core_schema.py). Auto-increment
    # PK (SERIAL) + TIMESTAMPTZ created_at DEFAULT NOW(). Parity-verified
    # byte-equivalent; guarded create is a no-op on existing DBs. See
    # tests/test_core_schema_parity.py.
    from lib.database._core_schema import USERS, create_if_absent
    create_if_absent(conn, USERS, table_exists=_table_exists)

    # conversations: base table migrated onto Core (lib/database/_core_schema.py,
    # OPTION B). Core owns the shared base columns INCLUDING search_text; the
    # PG-only full-text infra (search_tsv tsvector, pg_trgm, GIN indexes, sync
    # trigger + backfills) stays as explicit guarded DDL below. The
    # search_text entry in the ALTER loop below is now a guarded no-op on fresh
    # installs (Core emits the column) and still upgrades old DBs that lack it.
    # See tests/test_core_schema_parity.py.
    from lib.database._core_schema import CONVERSATIONS, create_if_absent
    create_if_absent(conn, CONVERSATIONS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_meta ON conversations(user_id, updated_at DESC, id, title, msg_count, created_at)')
    # Partial expression index for stale-task startup recovery: the recovery
    # scan probes `settings->>'activeTaskId' IS NOT NULL` (see
    # lib/tasks_pkg/manager.py::recover_stale_tasks_on_startup). Only the handful
    # of conversations carrying a stuck activeTaskId after a crash are indexed,
    # so this stays tiny and turns a full-table seq scan into an index lookup.
    # The expression MUST match what translate_sql emits for the recovery query
    # (`json_extract` → `settings::jsonb->>'activeTaskId'`), otherwise the
    # planner won't recognize the index. settings is already jsonb so the cast
    # is a no-op at runtime but is required for the expression to align.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_active_task ON conversations "
                "((settings::jsonb->>'activeTaskId')) "
                "WHERE (settings::jsonb->>'activeTaskId') IS NOT NULL")

    # task_results + task_events: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded creates are no-ops on existing
    # DBs. Indexes stay as explicit DDL below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        TASK_RESULTS, TASK_EVENTS, create_if_absent,
    )
    create_if_absent(conn, TASK_RESULTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_conv ON task_results(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_created ON task_results(created_at)')
    # Partial index for the startup stale-task sweep (recover_stale_tasks_on_startup
    # selects WHERE status='running'). task_results grows unbounded (25k+ done
    # rows) while running/interrupted stay tiny, so a partial index keeps the
    # sweep at ~0.04ms instead of a full seq scan that grows with table size.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON task_results(status) "
                "WHERE status IN ('running', 'interrupted')")

    # ── task_events: persisted SSE event log (durable Last-Event-ID resumption) ──
    # Replaces in-memory task['events'] for cross-restart and post-cleanup
    # replay. event_id is monotonic per task, mirrored in the SSE 'id:' field.
    create_if_absent(conn, TASK_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_events_ts ON task_events(ts_ms)')

    # ── conversation_messages: Phase 5 messages-as-rows (migrator-first) ──
    # Empty on existing installs until the TOFU_MESSAGES_ROWS-gated backfill /
    # dual-write populates it (lib/database/messages_rows.py). No data depends
    # on it until reads are flipped (a separate, verification-gated step).
    from lib.database._core_schema import CONVERSATION_MESSAGES
    create_if_absent(conn, CONVERSATION_MESSAGES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv ON conversation_messages(conv_id, seq)')
    # Partial UNIQUE: _msgId is the per-conv addressing key WHEN PRESENT, but
    # legacy/un-backfilled messages carry msg_id='' and several may coexist in
    # one conversation, so empty ids are excluded from the uniqueness guarantee.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_msgs_msgid ON conversation_messages(conv_id, msg_id) WHERE msg_id <> ''")

    # ── chat_artifacts: renderable reports promoted out of chat (md/html/svg) ──
    # First-class storage for "report-shaped" outputs so they survive
    # compaction, can be re-opened in the right-side panel by stable URL,
    # and can be versioned / pinned independently of the conversation row.
    # chat_artifacts: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded create is a no-op on existing
    # DBs. Indexes stay as explicit DDL below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import CHAT_ARTIFACTS, create_if_absent
    create_if_absent(conn, CHAT_ARTIFACTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_conv ON chat_artifacts(conv_id, created_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_msg ON chat_artifacts(conv_id, msg_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_sha ON chat_artifacts(conv_id, content_sha256)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_artifact_task ON chat_artifacts(task_id)')

    # transcript_archive: migrated onto Core (lib/database/_core_schema.py).
    # Auto-increment PK (SERIAL) + per-dialect epoch_now() default. Parity-
    # verified byte-equivalent; guarded create is a no-op on existing DBs.
    # The ALTER-COLUMN migration loop below stays (upgrade path for DBs that
    # predate the metadata columns). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import TRANSCRIPT_ARCHIVE, create_if_absent
    create_if_absent(conn, TRANSCRIPT_ARCHIVE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ta_conv ON transcript_archive(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ta_conv_created ON transcript_archive(conv_id, created_at DESC)')
    # Migrations — extend existing transcript_archive with metadata columns
    for col, sql in {
        'trigger':       "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT 'force'",
        'task_id':       "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS task_id TEXT NOT NULL DEFAULT ''",
        'round_num':     "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS round_num INTEGER NOT NULL DEFAULT 0",
        'model':         "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT ''",
        'tokens_before': "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS tokens_before INTEGER NOT NULL DEFAULT 0",
        'tokens_after':  "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS tokens_after INTEGER NOT NULL DEFAULT 0",
        'msgs_before':   "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS msgs_before INTEGER NOT NULL DEFAULT 0",
        'msgs_after':    "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS msgs_after INTEGER NOT NULL DEFAULT 0",
        'reason':        "ALTER TABLE transcript_archive ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''",
    }.items():
        try:
            cur.execute(sql)
        except Exception as e:
            logger.debug('[DB] PG migration %s skipped: %s', col, e)

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

    # ── Migration: rename searchRounds → toolRounds inside conversations messages JSON ──
    # The messages JSONB column stores assistant messages with a 'searchRounds' key.
    # Rename all occurrences to 'toolRounds' in a single SQL update.
    # This is idempotent — only updates messages that still have 'searchRounds'.
    try:
        cur.execute("""
            UPDATE conversations
            SET messages = REPLACE(messages::text, '"searchRounds":', '"toolRounds":')::jsonb
            WHERE messages::text LIKE '%"searchRounds":%'
        """)
        _migrated_count = cur.rowcount
        if _migrated_count > 0:
            logger.info('[DB] Migration: renamed searchRounds → toolRounds in %d conversation(s)', _migrated_count)
        conn.commit()
    except Exception as _sr_err:
        logger.warning('[DB] Migration: searchRounds→toolRounds in conversations failed (non-fatal): %s', _sr_err)
        try:
            conn._conn.rollback()
        except Exception as _re:
            logger.debug('[DB] rollback after searchRounds migration failure: %s', _re)

    for col, sql in {
        'settings':  "ALTER TABLE conversations ADD COLUMN settings JSONB NOT NULL DEFAULT '{}'::jsonb",
        'msg_count': "ALTER TABLE conversations ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0",
        'search_text': "ALTER TABLE conversations ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'conversations', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to conversations', col)

    # ── search_tsv: stored tsvector column for fast full-text search ──
    if not _column_exists(conn, 'conversations', 'search_tsv'):
        cur.execute('ALTER TABLE conversations ADD COLUMN search_tsv tsvector')
        logger.info('[DB] Migration: added column search_tsv to conversations')

    # ── pg_trgm GIN index for ILIKE fallback on search_text ──
    cur.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_search_trgm ON conversations USING gin (search_text gin_trgm_ops)')
    # ── Expression trgm index matching the Phase-2 search fallback predicate ──
    # routes/conversations_search.py filters with `lower(left(search_text,10000))
    # LIKE ?`. The plain idx_conv_search_trgm above is on the RAW column, so the
    # lower()/left() wrappers defeat it → full Seq Scan that detoasts every row
    # (~1.2s on 3k rows). This expression index matches the predicate EXACTLY
    # (same lower(left(...,10000)) shape) → Bitmap Index Scan: common term
    # 1218ms→101ms, rare term 744ms→<1ms. The 10000 cap MUST stay in sync with
    # the SQL in conversations_search.py or the planner won't use this index.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_search_head_trgm ON conversations '
                'USING gin (lower(left(search_text, 10000)) gin_trgm_ops)')
    # ── GIN index on search_tsv for fast tsvector @@ queries ──
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_search_tsv ON conversations USING gin (search_tsv)')

    # ── Trigger: keep search_tsv in sync with search_text ──
    # Without this, every INSERT/UPDATE on conversations would need to
    # explicitly set search_tsv = to_tsvector(...), which is easy to forget
    # (and was forgotten in routes/conversations.py save_conv — see
    # https://… internal bug). A BEFORE trigger makes it automatic.
    cur.execute('''
        CREATE OR REPLACE FUNCTION conversations_search_tsv_update() RETURNS trigger AS $$
        BEGIN
            IF (TG_OP = 'INSERT') OR (NEW.search_text IS DISTINCT FROM OLD.search_text) THEN
                NEW.search_tsv := to_tsvector('simple', left(coalesce(NEW.search_text, ''), 50000));
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    ''')
    cur.execute('DROP TRIGGER IF EXISTS conversations_search_tsv_trg ON conversations')
    cur.execute('''
        CREATE TRIGGER conversations_search_tsv_trg
        BEFORE INSERT OR UPDATE OF search_text ON conversations
        FOR EACH ROW EXECUTE FUNCTION conversations_search_tsv_update();
    ''')

    # ── Backfill search_text for existing conversations that have empty search_text ──
    cur.execute("SELECT count(*) FROM conversations WHERE search_text = '' AND msg_count > 0")
    backfill_count = cur.fetchone()[0]
    if backfill_count > 0:
        logger.info('[DB] Backfilling search_text for %d conversations...', backfill_count)
        _backfill_search_text(conn)

    # ── Backfill search_tsv for existing conversations ──
    cur.execute("SELECT count(*) FROM conversations WHERE search_text != '' AND search_tsv IS NULL")
    tsv_backfill = cur.fetchone()[0]
    if tsv_backfill > 0:
        logger.info('[DB] Backfilling search_tsv for %d conversations...', tsv_backfill)
        _backfill_search_tsv(conn)

    # ── Message queue: migrated onto Core. ──
    from lib.database._core_schema import (
        MESSAGE_QUEUE, create_if_absent,
    )
    create_if_absent(conn, MESSAGE_QUEUE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
    # ── Migration (v26): unified priority turn-source queue columns ──
    for col, sql in {
        'kind':     "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'real'",
        'priority': "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100",
    }.items():
        try:
            cur.execute(sql)
        except Exception as e:
            logger.debug('[DB] PG migration message_queue.%s skipped: %s', col, e)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv_prio ON message_queue(conv_id, priority, position)')

    # paper_reports: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded create is a no-op on existing
    # DBs. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_REPORTS, create_if_absent
    create_if_absent(conn, PAPER_REPORTS, table_exists=_table_exists)
    # Migration: add `meta` (JSON model+usage+cost finish-tag) to existing DBs.
    if not _column_exists(conn, 'paper_reports', 'meta'):
        cur.execute("ALTER TABLE paper_reports ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column meta to paper_reports')

    # paper_library: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded create is a no-op on existing
    # DBs. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_LIBRARY, create_if_absent
    create_if_absent(conn, PAPER_LIBRARY, table_exists=_table_exists)
    # ── Daily cost cache: pre-aggregated per-day LLM costs (avoids full
    # table scans on every calendar render).  date is 'YYYY-MM-DD' local time.
    # conversations_json stores the per-conv breakdown for drill-down.
    # Past days are cached forever (messages are immutable); today is always
    # recomputed live.  Invalidated on conv delete / message delete.
    # daily_cost_cache: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded create is a no-op on existing
    # DBs. Index stays as explicit DDL below. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import DAILY_COST_CACHE, create_if_absent
    create_if_absent(conn, DAILY_COST_CACHE, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_daily_cost_user_date ON daily_cost_cache(user_id, date)')

    cur.execute('CREATE INDEX IF NOT EXISTS idx_paper_lib_user ON paper_library(user_id, updated_at DESC)')

    # ── Paper translations: persistent cache for Babel-mode whole-paper
    # translations (server-owned task; mirrors paper_reports).
    # paper_translations: migrated onto Core (lib/database/_core_schema.py).
    # Parity-verified byte-equivalent; guarded create is a no-op on existing
    # DBs. See tests/test_core_schema_parity.py.
    from lib.database._core_schema import PAPER_TRANSLATIONS, create_if_absent
    create_if_absent(conn, PAPER_TRANSLATIONS, table_exists=_table_exists)

    # Seed default user
    cur.execute("""
        INSERT INTO users (id, username, display_name, password_hash)
        VALUES (1, 'default', 'User', '')
        ON CONFLICT (id) DO NOTHING
    """)

    conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    # pricing_cache + recent_projects: migrated onto Core definitions
    # (lib/database/_core_schema.py). Parity-verified byte-equivalent to the
    # former hand-DDL; guarded creates are no-ops on existing DBs. See
    # tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        PRICING_CACHE, RECENT_PROJECTS, SCHEMA_META, create_if_absent,
    )
    # schema_meta is core-owned and holds the fast-startup version cache; it
    # MUST exist independently of the (optional) trading domain.
    create_if_absent(conn, SCHEMA_META, table_exists=_table_exists)
    create_if_absent(conn, PRICING_CACHE, table_exists=_table_exists)
    create_if_absent(conn, RECENT_PROJECTS, table_exists=_table_exists)

    # error_resolutions (PG-only): migrated onto Core.
    from lib.database._core_schema import ERROR_RESOLUTIONS, create_if_absent
    create_if_absent(conn, ERROR_RESOLUTIONS, table_exists=_table_exists)

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
        # ── Older installs created scheduled_tasks via a minimal CREATE
        #    TABLE in lib/scheduler/manager.py::_init_table; the columns
        #    below need to exist for create_task() to succeed. ──
        ('description', "TEXT DEFAULT ''"),
        ('notify_on_failure', "BOOLEAN NOT NULL DEFAULT TRUE"),
        ('notify_on_success', "BOOLEAN NOT NULL DEFAULT FALSE"),
        ('max_runtime', "INTEGER NOT NULL DEFAULT 300"),
        ('last_result', "TEXT"),
        ('run_count', "INTEGER NOT NULL DEFAULT 0"),
        ('fail_count', "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in _proactive_cols:
        if not _column_exists(conn, 'scheduled_tasks', col_name):
            cur.execute(f'ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}')
            logger.info('[DB] Migration: added column %s to scheduled_tasks', col_name)

    # ── Timer Watcher tables ──
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
    # Mirror of the SQLite block — see ``lib/database/_schema_sqlite.py`` for
    # the template-vs-instance design notes. A run pins a definition snapshot;
    # orchestration_run_events is the durable cursor-replay event log.
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
    # One row per gated request.  The rate_limit decorator counts rows
    # within the per-endpoint sliding window via SELECT COUNT(*).
    # ts_ms is epoch milliseconds.  An auto-increment ``id`` is the PK
    # so multiple sibling requests in the same millisecond don't trip
    # a uniqueness collision (would have been a real bug under burst
    # traffic).  See lib/rate_limit_store.py.
    # rate_limit_events: migrated onto Core (BIGSERIAL/INTEGER AUTOINCREMENT PK).
    from lib.database._core_schema import RATE_LIMIT_EVENTS, create_if_absent
    create_if_absent(conn, RATE_LIMIT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_events(endpoint, ip, ts_ms)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limit_events(ts_ms)')

    # ─────────────────────────────────────────────────────────────────
    #  Billing / multi-tenant tables (mirror of the SQLite block — see
    #  ``lib/database/_schema_sqlite.py`` for design notes on the
    #  micro-credit unit and ledger-as-source-of-truth invariant).
    # ─────────────────────────────────────────────────────────────────
    # tenant_users: migrated onto Core.
    from lib.database._core_schema import TENANT_USERS, create_if_absent
    create_if_absent(conn, TENANT_USERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(email)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_role ON tenant_users(role)')

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

    conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  init_db — top-level entry point
# ═══════════════════════════════════════════════════════════════════════

def init_db(_new_pg_connection, _STATEMENT_TIMEOUT_MS):
    """Initialize all database schemas.

    Uses a schema version cache to skip redundant DDL on subsequent
    startups.

    Args:
        _new_pg_connection: callable that returns a PgConnection.
        _STATEMENT_TIMEOUT_MS: statement timeout for normal operations (restored after DDL).
    """
    logger.info('[DB] Schema initialization started (PostgreSQL)')

    conn = None
    try:
        conn = _new_pg_connection()

        # ── Fast path: check if schema is already at current version AND
        #    the set of optional domains is unchanged. The domain set is part
        #    of the cache key because enabling a new domain (e.g. trading) on a
        #    server that booted without it must re-trigger that domain's DDL —
        #    otherwise the version fast-path would silently skip its tables.
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

        # Raise statement_timeout for DDL
        try:
            cur = conn.cursor()
            cur.execute('SET SESSION statement_timeout = %s', ('600s',))
            conn.commit()
            cur.close()
            logger.debug('[DB] Raised statement_timeout to 600s for schema init')
        except Exception as e:
            logger.debug('[DB] Could not raise statement_timeout for init (non-fatal): %s', e)
            try:
                conn.rollback()
            except Exception as _rb_err:
                logger.debug('[DB] Rollback after statement_timeout raise failed: %s', _rb_err)

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
            audit_log('db_schema_init', backend='pg', version=_SCHEMA_VERSION,
                      domains=want_domains, prev_version=current_version)
        except Exception as _ae:
            logger.debug('[DB] audit_log db_schema_init failed: %s', _ae)

        # Restore normal statement_timeout
        try:
            cur = conn.cursor()
            cur.execute('SET SESSION statement_timeout = %s',
                        (f'{_STATEMENT_TIMEOUT_MS}ms',))
            conn.commit()
            cur.close()
        except Exception as _st_err:
            logger.debug('[DB] Could not restore statement_timeout after DDL (non-fatal): %s', _st_err)

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
            except Exception as _close_err:
                logger.debug('[DB] Error closing schema-init connection: %s', _close_err)
