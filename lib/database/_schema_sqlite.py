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

_SCHEMA_VERSION = 10  # Increment when tables/columns/indexes change


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


def _get_schema_version(conn):
    """Read current schema version from DB."""
    try:
        if not _table_exists(conn, 'trading_config'):
            return None
        cur = conn._conn.cursor()
        cur.execute("SELECT value FROM trading_config WHERE key = '_schema_version'")
        row = cur.fetchone()
        if row:
            return int(row[0])
        return None
    except Exception as e:
        logger.debug('[DB] Could not read schema version (expected on first run): %s', e)
        return None


def _set_schema_version(conn, version):
    """Write schema version to DB after successful DDL."""
    try:
        conn._conn.execute(
            "INSERT OR REPLACE INTO trading_config (key, value) VALUES ('_schema_version', ?)",
            (str(version),)
        )
        conn._conn.commit()
        logger.info('[DB] Schema version updated to %d', version)
    except Exception as e:
        logger.warning('[DB] Failed to write schema version: %s', e)


# ═══════════════════════════════════════════════════════════════════════
#  Chat Schema
# ═══════════════════════════════════════════════════════════════════════

def _backfill_search_fts(conn):
    """One-time migration: populate FTS5 table from existing conversations."""
    import json
    from routes.conversations import build_search_text

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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
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
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_conv_meta ON conversations(user_id, updated_at DESC, id, title, msg_count, created_at)')

    cur.execute('''
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
            created_at INTEGER NOT NULL,
            completed_at INTEGER
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_conv ON task_results(conv_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_created ON task_results(created_at)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS transcript_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
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
    cur.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts
        USING fts5(search_text, content='', tokenize='unicode61')
    ''')

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

    # ── Agent backend session mapping ──
    cur.execute('''
        CREATE TABLE IF NOT EXISTS agent_sessions (
            conv_id TEXT NOT NULL,
            backend TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (conv_id, backend)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_agent_sessions_backend ON agent_sessions(backend)')

    # ── Message queue: server-side pending message queue ──
    cur.execute('''
        CREATE TABLE IF NOT EXISTS message_queue (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            config TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_mq_conv ON message_queue(conv_id, position)')

    # ── Paper reports: persistent cache for paper analysis reports ──
    cur.execute('''
        CREATE TABLE IF NOT EXISTS paper_reports (
            paper_hash TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'en',
            report TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (paper_hash, lang)
        )
    ''')

    # Seed default user
    cur.execute("""
        INSERT OR IGNORE INTO users (id, username, display_name, password_hash)
        VALUES (1, 'default', 'User', '')
    """)

    conn._conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  Trading Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_trading_schema(conn):
    """Create trading domain tables and run migrations."""
    cur = conn._conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_price_cache (
            symbol TEXT PRIMARY KEY,
            asset_name TEXT NOT NULL DEFAULT '',
            nav REAL NOT NULL DEFAULT 0,
            nav_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'api',
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            shares REAL NOT NULL DEFAULT 0,
            buy_price REAL NOT NULL DEFAULT 0,
            buy_date TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL DEFAULT '',
            market_context TEXT NOT NULL DEFAULT '',
            adopted INTEGER NOT NULL DEFAULT 0,
            actual_result TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'buy',
            shares REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            tx_date TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_daily_briefing (
            date TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            news_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            strategy_ids TEXT NOT NULL DEFAULT '[]',
            risk_level TEXT NOT NULL DEFAULT 'medium',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_intel_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            content_simhash INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_expires ON trading_intel_cache(expires_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_fetched ON trading_intel_cache(fetched_at)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_intel_cache_category ON trading_intel_cache(category)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_trade_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_intel_crawl_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_decision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_intel_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            intel_id INTEGER,
            analysis_type TEXT NOT NULL DEFAULT 'summary',
            content TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_autopilot_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_autopilot_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
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
        'content_simhash': "ALTER TABLE trading_intel_cache ADD COLUMN content_simhash INTEGER NOT NULL DEFAULT 0",
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

    # Meta-Strategy & Strategy Learner tables
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id TEXT NOT NULL DEFAULT '',
            market_condition_json TEXT NOT NULL DEFAULT '{}',
            strategy_ids_json TEXT NOT NULL DEFAULT '[]',
            strategy_names_json TEXT NOT NULL DEFAULT '[]',
            deployed_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_deploy_cycle ON trading_strategy_deployments(cycle_id)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_combo_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_compatibility (
            pair_key TEXT PRIMARY KEY,
            strategy_id_a INTEGER NOT NULL,
            strategy_id_b INTEGER NOT NULL,
            compatibility_score REAL NOT NULL DEFAULT 0,
            sample_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS trading_strategy_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn._conn.commit()


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS pricing_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS recent_projects (
            path TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 1,
            last_used INTEGER NOT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
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
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS proactive_poll_log (
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

    # Timer Watcher tables
    cur.execute('''
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS timer_poll_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timer_id TEXT NOT NULL,
            poll_time TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'wait',
            reason TEXT NOT NULL DEFAULT '',
            check_output TEXT NOT NULL DEFAULT '',
            tokens_used INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_poll_log ON timer_poll_log(timer_id, poll_time DESC)')

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

        _init_chat_schema(conn)
        logger.info('[DB] Chat schema initialized')
        _init_trading_schema(conn)
        logger.info('[DB] Trading schema initialized')
        _init_system_schema(conn)
        logger.info('[DB] System schema initialized')

        _set_schema_version(conn, _SCHEMA_VERSION)

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
            except Exception:
                pass
