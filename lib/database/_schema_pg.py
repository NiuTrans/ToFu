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

_SCHEMA_VERSION = 10  # Increment when tables/columns/indexes change


def _column_exists(conn, table, column):
    """Check if a column exists in a PostgreSQL table."""
    cur = conn._conn.cursor()
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return cur.fetchone() is not None


def _get_schema_version(conn):
    """Read current schema version from DB.

    Returns:
        int version if found, None if table doesn't exist or key not set.
    """
    try:
        cur = conn._conn.cursor()
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'trading_config'
        """)
        if not cur.fetchone():
            conn._conn.rollback()
            return None
        cur.execute("SELECT value FROM trading_config WHERE key = '_schema_version'")
        row = cur.fetchone()
        conn._conn.rollback()
        if row:
            return int(row[0])
        return None
    except Exception as e:
        logger.debug('[DB] Could not read schema version (expected on first run): %s', e)
        try:
            conn._conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after schema version read failed: %s', _rb_err)
        return None


def _set_schema_version(conn, version):
    """Write schema version to DB after successful DDL."""
    try:
        cur = conn._conn.cursor()
        cur.execute("""
            INSERT INTO trading_config (key, value) VALUES ('_schema_version', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(version),))
        conn._conn.commit()
        logger.info('[DB] Schema version updated to %d', version)
    except Exception as e:
        logger.warning('[DB] Failed to write schema version: %s', e)
        try:
            conn._conn.rollback()
        except Exception as _rb_err:
            logger.debug('[DB] Rollback after schema version write failed: %s', _rb_err)


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

def _backfill_search_text(conn):
    """One-time migration: populate search_text and search_tsv for existing conversations.

    Reads messages JSON in batches, extracts plaintext via build_search_text(),
    and writes back.  Runs only once (when search_text column is newly added).
    """
    import json
    from routes.conversations import build_search_text

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

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New Chat',
            messages JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            settings JSONB NOT NULL DEFAULT '{}'::jsonb,
            msg_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (id, user_id)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_meta ON conversations(user_id, updated_at DESC, id, title, msg_count, created_at)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS task_results (
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
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_conv ON task_results(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_created ON task_results(created_at)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS transcript_archive (
            id SERIAL PRIMARY KEY,
            conv_id TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ta_conv ON transcript_archive(conv_id)')

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
        except Exception:
            pass

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
    # ── GIN index on search_tsv for fast tsvector @@ queries ──
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_search_tsv ON conversations USING gin (search_tsv)')

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

    # ── Agent backend session mapping ──
    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS agent_sessions (
            conv_id TEXT NOT NULL,
            backend TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (conv_id, backend)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_agent_sessions_backend ON agent_sessions(backend)')

    # ── Message queue: server-side pending message queue ──
    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS message_queue (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            config TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 1,
            created_at BIGINT NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')

    # ── Paper reports: persistent cache for paper analysis reports ──
    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS paper_reports (
            paper_hash TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'en',
            report TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL,
            PRIMARY KEY (paper_hash, lang)
        )
    ''')

    # Seed default user
    cur.execute("""
        INSERT INTO users (id, username, display_name, password_hash)
        VALUES (1, 'default', 'User', '')
        ON CONFLICT (id) DO NOTHING
    """)

    conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  Trading Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_trading_schema(conn):
    """Create trading domain tables and run migrations."""
    cur = conn._conn.cursor()

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_price_cache (
            symbol TEXT PRIMARY KEY,
            asset_name TEXT NOT NULL DEFAULT '',
            nav REAL NOT NULL DEFAULT 0,
            nav_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'api',
            updated_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_holdings (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            shares REAL NOT NULL DEFAULT 0,
            buy_price REAL NOT NULL DEFAULT 0,
            buy_date TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
            updated_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_recommendations (
            id SERIAL PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            market_context TEXT NOT NULL DEFAULT '',
            adopted INTEGER NOT NULL DEFAULT 0,
            actual_result TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_transactions (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'buy',
            shares REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            tx_date TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategies (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'observation',
            status TEXT NOT NULL DEFAULT 'active',
            logic TEXT NOT NULL DEFAULT '',
            scenario TEXT NOT NULL DEFAULT '',
            assets TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_daily_briefing (
            date TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            news_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_groups (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            strategy_ids TEXT NOT NULL DEFAULT '[]',
            risk_level TEXT NOT NULL DEFAULT 'medium',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_intel_cache (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'market',
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            raw_content TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            analysis TEXT NOT NULL DEFAULT '',
            relevance_score REAL NOT NULL DEFAULT 0,
            sentiment TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT '',
            analyzed_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            published_date TEXT NOT NULL DEFAULT '',
            date_source TEXT NOT NULL DEFAULT '',
            content_simhash BIGINT NOT NULL DEFAULT 0
        )
    ''')

    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_expires ON trading_intel_cache(expires_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_fetched ON trading_intel_cache(fetched_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_category ON trading_intel_cache(category)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_trade_queue (
            id SERIAL PRIMARY KEY,
            batch_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'buy',
            shares REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            est_fee REAL NOT NULL DEFAULT 0,
            fee_detail TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '',
            executed_at TEXT NOT NULL DEFAULT '',
            rolled_back_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_fee_rules (
            symbol TEXT PRIMARY KEY,
            asset_name TEXT NOT NULL DEFAULT '',
            buy_fee_rate REAL NOT NULL DEFAULT 0.0015,
            sell_fee_rules TEXT NOT NULL DEFAULT '[]',
            management_fee REAL NOT NULL DEFAULT 0,
            custody_fee REAL NOT NULL DEFAULT 0,
            data_source TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_intel_crawl_log (
            id SERIAL PRIMARY KEY,
            crawl_date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'market',
            source_key TEXT NOT NULL DEFAULT '',
            items_fetched INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_crawl_log_unique ON trading_intel_crawl_log(crawl_date, category, source_key)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_performance (
            id SERIAL PRIMARY KEY,
            strategy_id INTEGER NOT NULL,
            strategy_group_id INTEGER,
            period_start TEXT NOT NULL DEFAULT '',
            period_end TEXT NOT NULL DEFAULT '',
            return_pct REAL NOT NULL DEFAULT 0,
            benchmark_return_pct REAL NOT NULL DEFAULT 0,
            max_drawdown REAL NOT NULL DEFAULT 0,
            sharpe_ratio REAL,
            win_rate REAL,
            trade_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'live',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT '',
            decision_id INTEGER,
            actual_outcome TEXT NOT NULL DEFAULT '',
            lesson TEXT NOT NULL DEFAULT '',
            evaluated_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_strat_perf ON trading_strategy_performance(strategy_id)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_decision_history (
            id SERIAL PRIMARY KEY,
            batch_id TEXT NOT NULL DEFAULT '',
            strategy_group_id INTEGER,
            strategy_group_name TEXT NOT NULL DEFAULT '',
            briefing_content TEXT NOT NULL DEFAULT '',
            recommendation_content TEXT NOT NULL DEFAULT '',
            trades_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'generated',
            applied_at TEXT NOT NULL DEFAULT '',
            rolled_back_at TEXT NOT NULL DEFAULT '',
            performance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_intel_analysis (
            id SERIAL PRIMARY KEY,
            intel_id INTEGER,
            analysis_type TEXT NOT NULL DEFAULT 'summary',
            content TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_autopilot_cycles (
            id SERIAL PRIMARY KEY,
            cycle_id TEXT NOT NULL UNIQUE,
            cycle_number INTEGER NOT NULL DEFAULT 1,
            analysis_content TEXT NOT NULL DEFAULT '',
            structured_result TEXT NOT NULL DEFAULT '{}',
            kpi_evaluations TEXT NOT NULL DEFAULT '{}',
            correlations TEXT NOT NULL DEFAULT '[]',
            confidence_score REAL NOT NULL DEFAULT 0,
            market_outlook TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT 'running',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_autopilot_cycle ON trading_autopilot_cycles(cycle_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_autopilot_date ON trading_autopilot_cycles(created_at)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_autopilot_recommendations (
            id SERIAL PRIMARY KEY,
            cycle_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL DEFAULT '',
            asset_name TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'hold',
            amount REAL NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            actual_return REAL,
            evaluated_at TEXT,
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_autopilot_rec_cycle ON trading_autopilot_recommendations(cycle_id)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_bg_tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            params_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            thinking TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_bg_task_status ON trading_bg_tasks(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_bg_task_created ON trading_bg_tasks(created_at)')

    # Migrations
    for col, sql in {
        'published_at':    "ALTER TABLE trading_intel_cache ADD COLUMN published_at TEXT NOT NULL DEFAULT ''",
        'sentiment':       "ALTER TABLE trading_intel_cache ADD COLUMN sentiment TEXT NOT NULL DEFAULT ''",
        'published_date':  "ALTER TABLE trading_intel_cache ADD COLUMN published_date TEXT NOT NULL DEFAULT ''",
        'date_source':     "ALTER TABLE trading_intel_cache ADD COLUMN date_source TEXT NOT NULL DEFAULT ''",
        'content_simhash': "ALTER TABLE trading_intel_cache ADD COLUMN content_simhash BIGINT NOT NULL DEFAULT 0",
    }.items():
        if not _column_exists(conn, 'trading_intel_cache', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to trading_intel_cache', col)

    for col, sql in {
        'decision_id':    "ALTER TABLE trading_strategy_performance ADD COLUMN decision_id INTEGER",
        'actual_outcome': "ALTER TABLE trading_strategy_performance ADD COLUMN actual_outcome TEXT NOT NULL DEFAULT ''",
        'lesson':         "ALTER TABLE trading_strategy_performance ADD COLUMN lesson TEXT NOT NULL DEFAULT ''",
        'evaluated_at':   "ALTER TABLE trading_strategy_performance ADD COLUMN evaluated_at TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'trading_strategy_performance', col):
            cur.execute(sql)
            logger.info('[DB] Migration: added column %s to trading_strategy_performance', col)

    # ── Meta-Strategy & Strategy Learner tables ──
    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_deployments (
            id SERIAL PRIMARY KEY,
            cycle_id TEXT NOT NULL DEFAULT '',
            market_condition_json TEXT NOT NULL DEFAULT '{}',
            strategy_ids_json TEXT NOT NULL DEFAULT '[]',
            strategy_names_json TEXT NOT NULL DEFAULT '[]',
            deployed_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_deploy_cycle ON trading_strategy_deployments(cycle_id)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_combo_outcomes (
            id SERIAL PRIMARY KEY,
            cycle_id TEXT NOT NULL DEFAULT '',
            strategy_ids_json TEXT NOT NULL DEFAULT '[]',
            market_regime TEXT NOT NULL DEFAULT 'unknown',
            actual_return_pct REAL NOT NULL DEFAULT 0,
            benchmark_return_pct REAL NOT NULL DEFAULT 0,
            excess_return_pct REAL NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT '',
            outcome_notes TEXT NOT NULL DEFAULT '',
            evaluated_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_combo_cycle ON trading_strategy_combo_outcomes(cycle_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_combo_outcome ON trading_strategy_combo_outcomes(outcome)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_compatibility (
            pair_key TEXT PRIMARY KEY,
            strategy_id_a INTEGER NOT NULL,
            strategy_id_b INTEGER NOT NULL,
            compatibility_score REAL NOT NULL DEFAULT 0,
            sample_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS trading_strategy_failures (
            id SERIAL PRIMARY KEY,
            strategy_id INTEGER NOT NULL,
            strategy_name TEXT NOT NULL DEFAULT '',
            cycle_id TEXT NOT NULL DEFAULT '',
            market_regime TEXT NOT NULL DEFAULT 'unknown',
            actual_return_pct REAL NOT NULL DEFAULT 0,
            excess_return_pct REAL NOT NULL DEFAULT 0,
            failure_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_failure_strategy ON trading_strategy_failures(strategy_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_failure_regime ON trading_strategy_failures(market_regime)')

    conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS pricing_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS recent_projects (
            path TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 1,
            last_used BIGINT NOT NULL
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS error_resolutions (
            fingerprint TEXT PRIMARY KEY,
            logger_name TEXT NOT NULL DEFAULT '',
            sample_message TEXT NOT NULL DEFAULT '',
            resolved_by TEXT NOT NULL DEFAULT '',
            ticket TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            resolved_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
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
            -- Proactive agent fields (task_type='agent')
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
    ''')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS proactive_poll_log (
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
    ''')
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
    ]
    for col_name, col_def in _proactive_cols:
        if not _column_exists(conn, 'scheduled_tasks', col_name):
            cur.execute(f'ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}')
            logger.info('[DB] Migration: added column %s to scheduled_tasks', col_name)

    # ── Timer Watcher tables ──
    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS timer_watchers (
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
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_status ON timer_watchers(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_conv ON timer_watchers(conv_id)')

    _safe_create_table(cur, '''
        CREATE TABLE IF NOT EXISTS timer_poll_log (
            id SERIAL PRIMARY KEY,
            timer_id TEXT NOT NULL,
            poll_time TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'wait',
            reason TEXT NOT NULL DEFAULT '',
            check_output TEXT NOT NULL DEFAULT '',
            tokens_used INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_poll_log ON timer_poll_log(timer_id, poll_time DESC)')

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

        # ── Fast path: check if schema is already at current version ──
        t0 = time.monotonic()
        current_version = _get_schema_version(conn)
        if current_version == _SCHEMA_VERSION:
            elapsed = time.monotonic() - t0
            logger.info('[DB] Schema version %d is current — skipping DDL '
                        '(fast startup, checked in %.2fs)', _SCHEMA_VERSION, elapsed)
            return

        logger.info('[DB] Schema version %s → %d — running full DDL migration',
                    current_version, _SCHEMA_VERSION)

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
        _init_trading_schema(conn)
        logger.info('[DB] Trading schema initialized')
        _init_system_schema(conn)
        logger.info('[DB] System schema initialized')

        _set_schema_version(conn, _SCHEMA_VERSION)

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
