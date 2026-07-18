"""Database schema initialization — SQLite backend: chat domain.

Chat-domain tables, indexes, ALTER migrations, the FTS5 full-text search
virtual table, and the search_text sync/backfill. Native SQLite DDL.
"""

from lib.log import get_logger

from lib.database._schema_sqlite._meta import _column_exists, _table_exists
from lib.database._schema_sqlite._selfheal import _backfill_search_fts

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

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
        # segments (v36): the typed-segment timeline. Nullable TEXT/JSON — a
        # pre-existing row stays NULL until its task is re-persisted, and every
        # reader treats absent segments as "derive from the legacy channels".
        'segments':       "ALTER TABLE task_results ADD COLUMN segments TEXT",
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
        # rev (v37): server-issued monotonic message-version, trigger-bumped.
        'rev':          "ALTER TABLE conversations ADD COLUMN rev INTEGER NOT NULL DEFAULT 0",
    }.items():
        if not _column_exists(conn, 'conversations', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to conversations', col)

    # ── Trigger: bump rev whenever the messages column actually changes ──
    # The SQLite mirror of the PG conversations_rev_bump_trg. SQLite can't
    # mutate NEW in a BEFORE trigger, so this is an AFTER UPDATE OF messages
    # trigger running a nested rev-only UPDATE. Non-recursion is guaranteed two
    # ways even with PRAGMA recursive_triggers=ON: (1) `OF messages` scopes the
    # trigger so the nested `SET rev=...` (which does NOT touch messages) never
    # re-fires it; (2) the WHEN guard fires only on a genuine messages change.
    # rev advances ONLY on a real messages change, so settings/title-only writes
    # never bump it (no false CAS 409). Matches the PG semantics byte-for-byte.
    cur.execute('DROP TRIGGER IF EXISTS conversations_rev_bump_trg')
    cur.execute('''
        CREATE TRIGGER conversations_rev_bump_trg
        AFTER UPDATE OF messages ON conversations
        FOR EACH ROW WHEN NEW.messages IS NOT OLD.messages
        BEGIN
            UPDATE conversations SET rev = OLD.rev + 1
            WHERE id = NEW.id AND user_id = NEW.user_id;
        END;
    ''')

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
