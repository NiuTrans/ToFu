"""Chat domain schema bootstrap — PostgreSQL backend.

``_init_chat_schema`` creates the chat-domain Core tables and runs the PG-only
extras: indexes, tsvector/pg_trgm/GIN full-text infra, the search_tsv + rev
sync triggers, ALTER migrations, and the one-time search backfills.
"""

from lib.log import get_logger

from lib.database._schema_pg._meta import _column_exists, _table_exists
from lib.database._schema_pg._selfheal import (
    _backfill_search_text, _backfill_search_tsv,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

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
        'search_results': "ALTER TABLE task_results ADD COLUMN IF NOT EXISTS search_results TEXT",
        'metadata':       "ALTER TABLE task_results ADD COLUMN IF NOT EXISTS metadata TEXT",
        # segments (v36): the typed-segment timeline (epic pt_cb8f98b0cb9b47fb).
        # Nullable TEXT/JSON string — mirrors SQLite; pre-existing rows stay
        # NULL and readers fall back to deriving from the legacy channels.
        'segments':       "ALTER TABLE task_results ADD COLUMN IF NOT EXISTS segments TEXT",
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
        # rev (v37): server-issued monotonic message-version, trigger-bumped.
        'rev': "ALTER TABLE conversations ADD COLUMN rev INTEGER NOT NULL DEFAULT 0",
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

    # ── Trigger: bump rev whenever the messages column actually changes ──
    # rev is the server-issued monotonic message-version powering CAS + the
    # rev-based reconcile winner. Bumping it in a BEFORE UPDATE trigger — NOT in
    # any application writer — makes it (a) fire in the SAME statement as every
    # writer (all 11+ message writers get it for free), (b) impossible for a
    # future writer to forget, and (c) uniformly guarded to advance ONLY on a
    # genuine messages change: a settings-only / title-only / rename write does
    # NOT touch messages, so `IS DISTINCT FROM` leaves rev untouched and cannot
    # cause a false CAS 409. `OF messages` scopes the trigger so it doesn't even
    # fire on non-messages updates. INSERT is intentionally NOT covered: a fresh
    # row starts at rev=0 (the column default), matching every pre-CAS client.
    cur.execute('''
        CREATE OR REPLACE FUNCTION conversations_rev_bump() RETURNS trigger AS $$
        BEGIN
            IF (NEW.messages IS DISTINCT FROM OLD.messages) THEN
                NEW.rev := COALESCE(OLD.rev, 0) + 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    ''')
    cur.execute('DROP TRIGGER IF EXISTS conversations_rev_bump_trg ON conversations')
    cur.execute('''
        CREATE TRIGGER conversations_rev_bump_trg
        BEFORE UPDATE OF messages ON conversations
        FOR EACH ROW EXECUTE FUNCTION conversations_rev_bump();
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
    # Migration: add `folder_id` (optional folder grouping) to existing DBs.
    if not _column_exists(conn, 'paper_library', 'folder_id'):
        cur.execute("ALTER TABLE paper_library ADD COLUMN folder_id TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column folder_id to paper_library')
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
