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


_CONVERSATIONS_REV_TRIGGER_FUNCTION_SQL = '''
    CREATE OR REPLACE FUNCTION conversations_rev_bump() RETURNS trigger AS $$
    BEGIN
        IF (NEW.messages IS DISTINCT FROM OLD.messages) THEN
            NEW.rev := COALESCE(OLD.rev, 0) + 1;
            NEW.msg_count := CASE
                WHEN jsonb_typeof(NEW.messages) = 'array'
                THEN jsonb_array_length(NEW.messages)
                ELSE 0
            END;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
'''


_LZ4_COMPRESSION_STATEMENTS = (
    # Metadata-only policy: PostgreSQL uses it for newly written/re-written
    # oversized values; existing TOAST rows are never rewritten at startup.
    # LZ4 is attempted separately from the correctness/runtime-tuning
    # transaction because a PostgreSQL build compiled without LZ4 support must
    # remain fully usable (it simply keeps its current pglz policy).
    'ALTER TABLE conversations '
    'ALTER COLUMN messages SET COMPRESSION lz4, '
    'ALTER COLUMN search_text SET COMPRESSION lz4',
    'ALTER TABLE conversation_message_archives '
    'ALTER COLUMN messages SET COMPRESSION lz4',
    'ALTER TABLE conversation_messages '
    'ALTER COLUMN content SET COMPRESSION lz4, '
    'ALTER COLUMN content_json SET COMPRESSION lz4, '
    'ALTER COLUMN thinking SET COMPRESSION lz4, '
    'ALTER COLUMN translated_content SET COMPRESSION lz4, '
    'ALTER COLUMN meta SET COMPRESSION lz4, '
    'ALTER COLUMN translation_state SET COMPRESSION lz4, '
    'ALTER COLUMN meta_light SET COMPRESSION lz4',
    'ALTER TABLE task_events '
    'ALTER COLUMN payload SET COMPRESSION lz4',
    'ALTER TABLE task_results '
    'ALTER COLUMN content SET COMPRESSION lz4, '
    'ALTER COLUMN thinking SET COMPRESSION lz4, '
    'ALTER COLUMN error SET COMPRESSION lz4, '
    'ALTER COLUMN tool_rounds SET COMPRESSION lz4, '
    'ALTER COLUMN search_results SET COMPRESSION lz4, '
    'ALTER COLUMN metadata SET COMPRESSION lz4, '
    'ALTER COLUMN segments SET COMPRESSION lz4',
    'ALTER TABLE transcript_archive '
    'ALTER COLUMN messages_json SET COMPRESSION lz4, '
    'ALTER COLUMN summary SET COMPRESSION lz4',
)


def _apply_lz4_compression_policy(conn) -> bool:
    """Prefer fast TOAST compression for future large values, fail-soft.

    ``SET COMPRESSION`` changes only column metadata.  It neither rewrites nor
    locks through gigabytes of existing transcript history.  PostgreSQL 14+
    understands the syntax, but an installation built without LZ4 rejects the
    method; that capability miss is an optimization skip, never a startup
    failure and never a reason to roll back the independent autovacuum/index
    tuning transaction.
    """
    cur = None
    try:
        cur = conn._conn.cursor()
        for statement in _LZ4_COMPRESSION_STATEMENTS:
            cur.execute(statement)
        conn.commit()
        return True
    except Exception as exc:
        logger.debug('[DB] LZ4 TOAST policy unavailable; keeping PostgreSQL '
                     'default compression: %s', exc)
        try:
            conn.rollback()
        except Exception as rollback_error:
            logger.debug('[DB] LZ4 policy rollback failed: %s', rollback_error)
        return False
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception as close_error:
                logger.debug('[DB] LZ4 policy cursor close failed: %s',
                             close_error)


def _apply_chat_runtime_tuning(conn):
    """Converge mutable PG storage settings on every boot, best-effort.

    These settings are not schema-versioned data migrations: changing an
    autovacuum threshold or disabling statistics on a large payload column
    must also reach installations whose schema version is already current.
    Keeping this pass separate lets the normal fast path remain fast while
    avoiding the bug where newly-added tuning lived forever behind a skipped
    full-DDL branch.
    """
    statements = (
        # This function is a data invariant, not a versioned migration.  CREATE
        # OR REPLACE updates the already-wired trigger on current-schema installs
        # without dropping it or forcing the expensive full DDL path.
        _CONVERSATIONS_REV_TRIGGER_FUNCTION_SQL,
        "ALTER TABLE conversations SET ("
        "autovacuum_vacuum_scale_factor=0.02, "
        "autovacuum_analyze_scale_factor=0.01, "
        "autovacuum_vacuum_threshold=100, "
        "autovacuum_analyze_threshold=100, "
        "autovacuum_vacuum_cost_limit=1000, "
        "autovacuum_vacuum_cost_delay=10)",
        'ALTER TABLE conversations ALTER COLUMN messages SET STATISTICS 0',
        "ALTER TABLE task_results SET ("
        "autovacuum_vacuum_scale_factor=0.02, "
        "autovacuum_analyze_scale_factor=0.01, "
        "autovacuum_vacuum_threshold=200, "
        "autovacuum_analyze_threshold=200, "
        "autovacuum_vacuum_cost_limit=1000, "
        "autovacuum_vacuum_cost_delay=10)",
        'ALTER TABLE task_results ALTER COLUMN content SET STATISTICS 0',
        'ALTER TABLE task_results ALTER COLUMN thinking SET STATISTICS 0',
        # Cover retention discovery without touching wide/TOASTed task-result
        # rows. PostgreSQL keeps its measured event-time-first stream plan;
        # this index also serves the bounded terminal-result reaper.
        "CREATE INDEX IF NOT EXISTS idx_task_terminal_retention "
        "ON task_results(completed_at, task_id) INCLUDE (conv_id) "
        "WHERE completed_at IS NOT NULL AND status IN ("
        "'done','error','aborted','interrupted')",
        "ALTER TABLE task_events SET ("
        "autovacuum_vacuum_scale_factor=0.01, "
        "autovacuum_analyze_scale_factor=0.005, "
        "autovacuum_vacuum_threshold=5000, "
        "autovacuum_analyze_threshold=5000, "
        "autovacuum_vacuum_cost_limit=1000, "
        "autovacuum_vacuum_cost_delay=10)",
        'ALTER TABLE task_events ALTER COLUMN payload SET STATISTICS 0',
        # The 15-second streaming-event reaper must be able to prove the
        # steady-state empty tier without walking the much larger 30-day
        # structural-event population.  INCLUDE keeps the ordered selector
        # covering while the predicate bounds index residency to the 6h tier.
        "CREATE INDEX IF NOT EXISTS idx_task_events_stream_ts "
        "ON task_events(ts_ms) INCLUDE (task_id, event_id) "
        "WHERE type NOT IN ("
        "'messages_snapshot','round_usage','round_start','round_end')",
        "ALTER TABLE conversation_messages SET ("
        "autovacuum_vacuum_scale_factor=0.02, "
        "autovacuum_analyze_scale_factor=0.01, "
        "autovacuum_vacuum_threshold=200, "
        "autovacuum_analyze_threshold=200, "
        "autovacuum_vacuum_cost_limit=1000, "
        "autovacuum_vacuum_cost_delay=10)",
        # The primary key already supplies this exact (conv_id, seq) btree.
        # Current-schema installs take this fast path, so the historical
        # duplicate must be removed here as well as in the full DDL path.
        'DROP INDEX IF EXISTS idx_conv_msgs_conv',
    )
    cur = None
    try:
        cur = conn._conn.cursor()
        for statement in statements:
            cur.execute(statement)
        conn.commit()
        # Independent best-effort transaction: unsupported LZ4 must not undo
        # the trigger/autovacuum/index policies above.
        _apply_lz4_compression_policy(conn)
        return True
    except Exception as exc:
        logger.warning('[DB] Runtime PG table tuning deferred: %s', exc)
        try:
            conn.rollback()
        except Exception as rollback_error:
            logger.debug('[DB] Runtime tuning rollback failed: %s', rollback_error)
        return False
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception as close_error:
                logger.debug('[DB] Runtime tuning cursor close failed: %s',
                             close_error)


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
    cur.execute(
        'SELECT id FROM conversations GROUP BY id HAVING COUNT(*)>1 LIMIT 1')
    if cur.fetchone() is not None:
        raise RuntimeError(
            'conversation id is not globally unique; normalized message rows '
            'cannot safely identify their owner')
    cur.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_global_id '
        'ON conversations(id)')
    cur.execute(
        'CREATE INDEX IF NOT EXISTS idx_conv_rows_authority '
        'ON conversations(id) '
        'INCLUDE (user_id, rev, messages_rows_rev, msg_count)')
    from lib.database._core_schema import CONVERSATION_MESSAGE_ARCHIVES
    create_if_absent(
        conn, CONVERSATION_MESSAGE_ARCHIVES, table_exists=_table_exists)
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
    # These tables are append/update heavy but contain large TOAST payloads.
    # Default autovacuum waits for 20% dead rows — millions of events or
    # hundreds of multi-megabyte result/conversation versions. Per-table low
    # thresholds keep index-only scans visible and reclaim dead tuples early;
    # the cost delay prevents a backlog cleanup from monopolising FUSE I/O.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_conv ON task_results(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_created ON task_results(created_at)')
    # Partial index for the startup stale-task sweep (recover_stale_tasks_on_startup
    # selects WHERE status='running'). task_results grows unbounded (25k+ done
    # rows) while running/interrupted stay tiny, so a partial index keeps the
    # sweep at ~0.04ms instead of a full seq scan that grows with table size.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON task_results(status) "
                "WHERE status IN ('running', 'interrupted')")
    # Exact range/order index shared with SQLite's orphan-running watchdog.
    # The older status-only index filters rows but still sorts completed_at.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_running_completed "
                "ON task_results(completed_at) "
                "WHERE status='running' AND completed_at IS NOT NULL")
    # Bounded terminal-result retention scans oldest completed rows.  Keep the
    # index partial so live/running rows and the large TOAST payload columns do
    # not inflate it; the maintenance query never needs those payloads.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_terminal_completed "
                "ON task_results(completed_at) WHERE completed_at IS NOT NULL "
                "AND status IN ('done', 'error', 'aborted', 'interrupted')")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_terminal_retention "
                "ON task_results(completed_at, task_id) INCLUDE (conv_id) "
                "WHERE completed_at IS NOT NULL AND status IN "
                "('done', 'error', 'aborted', 'interrupted')")

    # ── task_events: persisted SSE event log (durable Last-Event-ID resumption) ──
    # Replaces in-memory task['events'] for cross-restart and post-cleanup
    # replay. event_id is monotonic per task, mirrored in the SSE 'id:' field.
    create_if_absent(conn, TASK_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_events_ts ON task_events(ts_ms)')
    # (task_id, type) — the Request Inspector's access pattern. Every read it
    # makes is "this task's rows, optionally of one type":
    # request_inspector._read_events (WHERE task_id=?), the per-conv kind tally
    # (WHERE task_id IN (…) AND type='messages_snapshot'), and the tiered prune
    # (WHERE ts_ms<? AND type [NOT] IN (…)). Without this, those degrade to a
    # seq scan that DETOASTS every snapshot payload — measured 5.9s on a ~900MB
    # table, which would show up as a multi-second stall when opening the drawer.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_events_task_type ON task_events(task_id, type)')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_task_events_stream_ts "
                "ON task_events(ts_ms) INCLUDE (task_id, event_id) "
                "WHERE type NOT IN ("
                "'messages_snapshot','round_usage','round_start','round_end')")

    # ── conversation_messages: Phase 5 messages-as-rows (migrator-first) ──
    # Empty on existing installs until the TOFU_MESSAGES_ROWS-gated backfill /
    # dual-write populates it (lib/database/messages_rows.py). No data depends
    # on it until reads are flipped (a separate, verification-gated step).
    from lib.database._core_schema import CONVERSATION_MESSAGES
    create_if_absent(conn, CONVERSATION_MESSAGES, table_exists=_table_exists)
    if not _column_exists(conn, 'conversation_messages', 'meta_light'):
        # Nullable by design: do not rewrite/detoast the whole table during
        # startup. The online backfill fills it in bounded per-conversation
        # transactions; the read gate rejects any mirror containing NULL.
        cur.execute('ALTER TABLE conversation_messages ADD COLUMN meta_light JSONB')
        logger.info('[DB] Migration: added nullable meta_light to conversation_messages')
    if not _column_exists(conn, 'conversation_messages', 'translation_state'):
        cur.execute('ALTER TABLE conversation_messages ADD COLUMN translation_state JSONB')
        logger.info('[DB] Migration: added nullable translation_state to conversation_messages')
    if not _column_exists(conn, 'conversation_messages', 'message_ts'):
        # Nullable/no default keeps this a metadata-only upgrade; projection
        # backfill is resumable and the reader falls back to meta_light.
        cur.execute('ALTER TABLE conversation_messages ADD COLUMN message_ts BIGINT')
        logger.info('[DB] Migration: added nullable message_ts to conversation_messages')
    if not _column_exists(conn, 'conversation_messages', 'billing_meta'):
        # Nullable/no default is a metadata-only upgrade. The delayed
        # projection worker fills it without extending synchronous startup.
        cur.execute('ALTER TABLE conversation_messages ADD COLUMN billing_meta JSONB')
        logger.info('[DB] Migration: added nullable billing_meta to conversation_messages')
    cur.execute(
        'CREATE INDEX IF NOT EXISTS idx_conv_msgs_incomplete_projection '
        'ON conversation_messages(conv_id) WHERE meta_light IS NULL '
        'OR message_ts IS NULL OR billing_meta IS NULL')
    _apply_chat_runtime_tuning(conn)
    # The composite PRIMARY KEY already owns an identical btree on
    # (conv_id, seq).  Keeping a second copy doubles index writes/WAL and cache
    # residency without enabling any additional plan.  Remove the historical
    # duplicate during the normal idempotent startup migration; fresh installs
    # never create it.
    cur.execute('DROP INDEX IF EXISTS idx_conv_msgs_conv')
    # Partial UNIQUE: _msgId is the per-conv addressing key WHEN PRESENT, but
    # legacy/un-backfilled messages carry msg_id='' and several may coexist in
    # one conversation, so empty ids are excluded from the uniqueness guarantee.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_msgs_msgid ON conversation_messages(conv_id, msg_id) WHERE msg_id <> ''")

    # ── Turn / attempt v2 authority ───────────────────────────────────
    from lib.database._core_schema import (
        CONVERSATION_TURNS, GENERATION_ATTEMPTS, ATTEMPT_EVENTS,
    )
    create_if_absent(conn, CONVERSATION_TURNS, table_exists=_table_exists)
    create_if_absent(conn, GENERATION_ATTEMPTS, table_exists=_table_exists)
    create_if_absent(conn, ATTEMPT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_turns_conversation_order '
                'ON conversation_turns(conversation_id, lane_id, ordinal)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_turns_current_attempt '
                'ON conversation_turns(current_attempt_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_attempts_turn_created '
                'ON generation_attempts(turn_id, created_at DESC)')
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_attempts_task_id "
                "ON generation_attempts(task_id) WHERE task_id <> ''")
    cur.execute('CREATE INDEX IF NOT EXISTS idx_attempt_events_created '
                'ON attempt_events(created_at)')

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
        'abort_requested_at': "ALTER TABLE task_results ADD COLUMN IF NOT EXISTS abort_requested_at BIGINT NOT NULL DEFAULT 0",
        'abort_source': "ALTER TABLE task_results ADD COLUMN IF NOT EXISTS abort_source TEXT NOT NULL DEFAULT ''",
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
    from lib.database._access_policy import rows_authority_configured
    if not rows_authority_configured():
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
    else:
        logger.debug('[DB] Skipping legacy messages payload migration: '
                     'normalized rows are authoritative')

    for col, sql in {
        'settings':  "ALTER TABLE conversations ADD COLUMN settings JSONB NOT NULL DEFAULT '{}'::jsonb",
        'msg_count': "ALTER TABLE conversations ADD COLUMN msg_count INTEGER NOT NULL DEFAULT 0",
        'search_text': "ALTER TABLE conversations ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
        # rev (v37): server-issued monotonic message-version, trigger-bumped.
        'rev': "ALTER TABLE conversations ADD COLUMN rev INTEGER NOT NULL DEFAULT 0",
        # v46: proven row-mirror revision; -1 is fail-closed/unverified.
        'messages_rows_rev': "ALTER TABLE conversations ADD COLUMN messages_rows_rev INTEGER NOT NULL DEFAULT -1",
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
    cur.execute(_CONVERSATIONS_REV_TRIGGER_FUNCTION_SQL)
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
        MESSAGE_QUEUE, SCOPED_SEQUENCES, create_if_absent,
    )
    create_if_absent(conn, MESSAGE_QUEUE, table_exists=_table_exists)
    create_if_absent(conn, SCOPED_SEQUENCES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')
    # ── Migration (v26): unified priority turn-source queue columns ──
    for col, sql in {
        'kind':     "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'real'",
        'priority': "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100",
        # Dispatch lease columns (pt_4ab943fa) — dequeue leases, delete lands
        # only after spawn succeeds; reaper reclaims expired dead leases.
        'leased_until':  "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS leased_until BIGINT",
        'lease_task_id': "ALTER TABLE message_queue ADD COLUMN IF NOT EXISTS lease_task_id TEXT NOT NULL DEFAULT ''",
    }.items():
        try:
            cur.execute(sql)
        except Exception as e:
            logger.debug('[DB] PG migration message_queue.%s skipped: %s', col, e)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv_prio ON message_queue(conv_id, priority, position)')
    cur.execute(
        "INSERT INTO scoped_sequences(namespace, scope_key, value) "
        "SELECT 'message_queue_position', conv_id, MAX(position) "
        "FROM message_queue GROUP BY conv_id "
        "ON CONFLICT(namespace, scope_key) DO UPDATE SET value="
        "GREATEST(scoped_sequences.value, EXCLUDED.value)")

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
    # Migration: add `parser_version` (parse-once cache key — the extractor+
    # version that produced parsed_text) to existing DBs. Legacy rows keep ''
    # and miss the harvest probe by construction (self-heal on next touch).
    if not _column_exists(conn, 'paper_library', 'parser_version'):
        cur.execute("ALTER TABLE paper_library ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column parser_version to paper_library')
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

    # ── Paper podcasts: spoken-script + audio cache (paper podcast feature,
    # docs/PAPER_PODCAST_DESIGN.md). Core-defined; guarded create is a no-op
    # on existing DBs. Audio binaries live on disk; this table holds the
    # script JSON + metadata (same text-in-DB / binary-on-disk convention).
    from lib.database._core_schema import PAPER_PODCASTS, create_if_absent
    create_if_absent(conn, PAPER_PODCASTS, table_exists=_table_exists)

    # ── Paper notes: reader margin notes (reading-xp P4). Core-defined;
    # guarded create is a no-op on existing DBs.
    from lib.database._core_schema import PAPER_NOTES
    create_if_absent(conn, PAPER_NOTES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_paper_notes_hash ON paper_notes(paper_hash, lang)')

    # Seed default user
    cur.execute("""
        INSERT INTO users (id, username, display_name, password_hash)
        VALUES (1, 'default', 'User', '')
        ON CONFLICT (id) DO NOTHING
    """)

    conn.commit()
