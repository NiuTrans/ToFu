"""Canonical trading row schemas used by the sidecar repository.

Responsibility: define the logical tables once, initialize the repository's
bounded in-memory query evaluator, and expose exact legacy columns for the
one-time sidecar-owned import. No runtime path or host database driver enters
this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


TABLE_DDL: dict[str, str] = {
    'trading_action': "CREATE TABLE trading_action (\n            user_id INTEGER NOT NULL,\n            plan_date TEXT NOT NULL,\n            symbol TEXT NOT NULL,\n            side TEXT NOT NULL DEFAULT 'buy',\n            shares REAL NOT NULL DEFAULT 0,\n            amount REAL NOT NULL DEFAULT 0,\n            price REAL NOT NULL DEFAULT 0,\n            drift_pct REAL NOT NULL DEFAULT 0,\n            reason TEXT NOT NULL DEFAULT '',\n            status TEXT NOT NULL DEFAULT 'pending',\n            acted_at TEXT NOT NULL DEFAULT '',\n            actual_price REAL NOT NULL DEFAULT 0,\n            actual_shares REAL NOT NULL DEFAULT 0,\n            created_at TEXT NOT NULL DEFAULT '',\n            PRIMARY KEY (user_id, plan_date, symbol)\n        )",
    'trading_autopilot_cycles': "CREATE TABLE trading_autopilot_cycles (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            cycle_id TEXT NOT NULL UNIQUE,\n            cycle_number INTEGER NOT NULL DEFAULT 1,\n            analysis_content TEXT NOT NULL DEFAULT '',\n            structured_result TEXT NOT NULL DEFAULT '{}',\n            kpi_evaluations TEXT NOT NULL DEFAULT '{}',\n            correlations TEXT NOT NULL DEFAULT '[]',\n            confidence_score REAL NOT NULL DEFAULT 0,\n            market_outlook TEXT NOT NULL DEFAULT 'unknown',\n            status TEXT NOT NULL DEFAULT 'running',\n            created_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_autopilot_recommendations': "CREATE TABLE trading_autopilot_recommendations (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            cycle_id TEXT NOT NULL DEFAULT '',\n            symbol TEXT NOT NULL DEFAULT '',\n            asset_name TEXT NOT NULL DEFAULT '',\n            action TEXT NOT NULL DEFAULT 'hold',\n            amount REAL NOT NULL DEFAULT 0,\n            confidence REAL NOT NULL DEFAULT 0,\n            reason TEXT NOT NULL DEFAULT '',\n            status TEXT NOT NULL DEFAULT 'pending',\n            actual_return REAL,\n            evaluated_at TEXT,\n            created_at TEXT NOT NULL DEFAULT ''\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_bg_tasks': "CREATE TABLE trading_bg_tasks (\n            task_id TEXT PRIMARY KEY,\n            task_type TEXT NOT NULL DEFAULT '',\n            status TEXT NOT NULL DEFAULT 'running',\n            params_json TEXT NOT NULL DEFAULT '{}',\n            result_json TEXT NOT NULL DEFAULT '{}',\n            thinking TEXT NOT NULL DEFAULT '',\n            error TEXT NOT NULL DEFAULT '',\n            created_at TEXT NOT NULL DEFAULT '',\n            finished_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_config': 'CREATE TABLE trading_config (\n\t"key" TEXT NOT NULL, \n\tvalue TEXT DEFAULT \'\' NOT NULL, \n\tPRIMARY KEY ("key")\n)',
    'trading_daily_briefing': "CREATE TABLE trading_daily_briefing (\n            date TEXT PRIMARY KEY,\n            content TEXT NOT NULL DEFAULT '',\n            news_json TEXT NOT NULL DEFAULT '[]',\n            created_at TEXT NOT NULL DEFAULT ''\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_decision_history': "CREATE TABLE trading_decision_history (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            batch_id TEXT NOT NULL DEFAULT '',\n            strategy_group_id INTEGER,\n            strategy_group_name TEXT NOT NULL DEFAULT '',\n            briefing_content TEXT NOT NULL DEFAULT '',\n            recommendation_content TEXT NOT NULL DEFAULT '',\n            trades_json TEXT NOT NULL DEFAULT '[]',\n            status TEXT NOT NULL DEFAULT 'generated',\n            applied_at TEXT NOT NULL DEFAULT '',\n            rolled_back_at TEXT NOT NULL DEFAULT '',\n            performance_json TEXT NOT NULL DEFAULT '{}',\n            created_at TEXT NOT NULL DEFAULT ''\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_fee_rules': "CREATE TABLE trading_fee_rules (\n            symbol TEXT PRIMARY KEY,\n            asset_name TEXT NOT NULL DEFAULT '',\n            buy_fee_rate REAL NOT NULL DEFAULT 0.0015,\n            sell_fee_rules TEXT NOT NULL DEFAULT '[]',\n            management_fee REAL NOT NULL DEFAULT 0,\n            custody_fee REAL NOT NULL DEFAULT 0,\n            data_source TEXT NOT NULL DEFAULT '',\n            updated_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_holdings': "CREATE TABLE trading_holdings (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            symbol TEXT NOT NULL,\n            asset_name TEXT NOT NULL DEFAULT '',\n            shares REAL NOT NULL DEFAULT 0,\n            buy_price REAL NOT NULL DEFAULT 0,\n            buy_date TEXT NOT NULL DEFAULT '',\n            note TEXT NOT NULL DEFAULT '',\n            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),\n            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_intel_analysis': "CREATE TABLE trading_intel_analysis (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            intel_id INTEGER,\n            analysis_type TEXT NOT NULL DEFAULT 'summary',\n            content TEXT NOT NULL DEFAULT '',\n            metrics_json TEXT NOT NULL DEFAULT '{}',\n            created_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_intel_cache': "CREATE TABLE trading_intel_cache (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            category TEXT NOT NULL DEFAULT 'market',\n            title TEXT NOT NULL DEFAULT '',\n            summary TEXT NOT NULL DEFAULT '',\n            raw_content TEXT NOT NULL DEFAULT '',\n            source_url TEXT NOT NULL DEFAULT '',\n            source_name TEXT NOT NULL DEFAULT '',\n            analysis TEXT NOT NULL DEFAULT '',\n            relevance_score REAL NOT NULL DEFAULT 0,\n            sentiment TEXT NOT NULL DEFAULT '',\n            published_at TEXT NOT NULL DEFAULT '',\n            fetched_at TEXT NOT NULL DEFAULT '',\n            analyzed_at TEXT NOT NULL DEFAULT '',\n            expires_at TEXT NOT NULL DEFAULT '',\n            published_date TEXT NOT NULL DEFAULT '',\n            date_source TEXT NOT NULL DEFAULT '',\n            content_simhash INTEGER NOT NULL DEFAULT 0\n        )",
    'trading_intel_crawl_log': "CREATE TABLE trading_intel_crawl_log (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            crawl_date TEXT NOT NULL,\n            category TEXT NOT NULL DEFAULT 'market',\n            source_key TEXT NOT NULL DEFAULT '',\n            items_fetched INTEGER NOT NULL DEFAULT 0,\n            status TEXT NOT NULL DEFAULT 'ok',\n            started_at TEXT NOT NULL DEFAULT '',\n            finished_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_livetest_journal': 'CREATE TABLE "trading_livetest_journal" ("id" INTEGER NOT NULL, "session_id" TEXT NOT NULL, "entry_type" TEXT NOT NULL, "action" TEXT NOT NULL, "symbol" TEXT NOT NULL, "amount" REAL NOT NULL, "reasoning" TEXT NOT NULL, "signals_json" TEXT NOT NULL, "confidence" INTEGER NOT NULL, "created_at" TEXT NOT NULL, PRIMARY KEY ("id"))',
    'trading_livetest_learning': 'CREATE TABLE "trading_livetest_learning" ("id" INTEGER NOT NULL, "session_id" TEXT NOT NULL, "lesson_type" TEXT NOT NULL, "content" TEXT NOT NULL, "old_value" TEXT NOT NULL, "new_value" TEXT NOT NULL, "trigger_reason" TEXT NOT NULL, "created_at" TEXT NOT NULL, PRIMARY KEY ("id"))',
    'trading_livetest_positions': 'CREATE TABLE "trading_livetest_positions" ("id" INTEGER NOT NULL, "session_id" TEXT NOT NULL, "symbol" TEXT NOT NULL, "asset_name" TEXT NOT NULL, "shares" REAL NOT NULL, "buy_price" REAL NOT NULL, "buy_date" TEXT NOT NULL, "current_price" REAL NOT NULL, "close_price" REAL, "close_date" TEXT, "stop_loss" REAL NOT NULL, "take_profit" REAL NOT NULL, "pnl" REAL, "pnl_pct" REAL, "status" TEXT NOT NULL, "reason" TEXT NOT NULL, "created_at" TEXT NOT NULL, PRIMARY KEY ("id"))',
    'trading_livetest_sessions': 'CREATE TABLE "trading_livetest_sessions" ("id" INTEGER NOT NULL, "session_id" TEXT NOT NULL, "initial_capital" REAL NOT NULL, "current_cash" REAL NOT NULL, "status" TEXT NOT NULL, "total_trades" INTEGER NOT NULL, "winning_trades" INTEGER NOT NULL, "total_pnl" REAL NOT NULL, "max_daily_trades" INTEGER NOT NULL, "config_json" TEXT NOT NULL, "created_at" TEXT NOT NULL, "updated_at" TEXT NOT NULL, PRIMARY KEY ("id"))',
    'trading_position': "CREATE TABLE trading_position (\n            user_id INTEGER NOT NULL,\n            symbol TEXT NOT NULL,\n            asset_name TEXT NOT NULL DEFAULT '',\n            shares REAL NOT NULL DEFAULT 0,\n            cost REAL NOT NULL DEFAULT 0,\n            pending_shares REAL NOT NULL DEFAULT 0,\n            settle_date TEXT NOT NULL DEFAULT '',\n            as_of TEXT NOT NULL DEFAULT '',\n            updated_at TEXT NOT NULL DEFAULT '',\n            PRIMARY KEY (user_id, symbol)\n        )",
    'trading_price_cache': "CREATE TABLE trading_price_cache (\n            symbol TEXT PRIMARY KEY,\n            asset_name TEXT NOT NULL DEFAULT '',\n            nav REAL NOT NULL DEFAULT 0,\n            nav_date TEXT NOT NULL DEFAULT '',\n            source TEXT NOT NULL DEFAULT 'api',\n            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)\n        )",
    'trading_recommendations': "CREATE TABLE trading_recommendations (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            content TEXT NOT NULL DEFAULT '',\n            market_context TEXT NOT NULL DEFAULT '',\n            adopted INTEGER NOT NULL DEFAULT 0,\n            actual_result TEXT NOT NULL DEFAULT '',\n            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_sim_indices': '''CREATE TABLE trading_sim_indices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        secid TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '', open REAL NOT NULL DEFAULT 0,
        close REAL NOT NULL DEFAULT 0, high REAL NOT NULL DEFAULT 0,
        low REAL NOT NULL DEFAULT 0, volume REAL NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0, change_pct REAL NOT NULL DEFAULT 0
    )''',
    'trading_sim_journal': '''CREATE TABLE trading_sim_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '', sim_date TEXT NOT NULL DEFAULT '',
        entry_type TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
        symbol TEXT NOT NULL DEFAULT '', amount REAL NOT NULL DEFAULT 0,
        reasoning TEXT NOT NULL DEFAULT '', signals_json TEXT NOT NULL DEFAULT '{}',
        confidence INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT ''
    )''',
    'trading_sim_macro': '''CREATE TABLE trading_sim_macro (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indicator TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '', value REAL NOT NULL DEFAULT 0
    )''',
    'trading_sim_positions': '''CREATE TABLE trading_sim_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL DEFAULT '',
        asset_name TEXT NOT NULL DEFAULT '', shares REAL NOT NULL DEFAULT 0,
        buy_price REAL NOT NULL DEFAULT 0, buy_date TEXT NOT NULL DEFAULT '',
        current_price REAL NOT NULL DEFAULT 0, stop_loss REAL NOT NULL DEFAULT 5,
        take_profit REAL NOT NULL DEFAULT 10, status TEXT NOT NULL DEFAULT 'open',
        close_price REAL NOT NULL DEFAULT 0, close_date TEXT NOT NULL DEFAULT '',
        pnl REAL NOT NULL DEFAULT 0, pnl_pct REAL NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT ''
    )''',
    'trading_sim_prices': '''CREATE TABLE trading_sim_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '',
        nav REAL NOT NULL DEFAULT 0, acc_nav REAL NOT NULL DEFAULT 0,
        change_pct REAL NOT NULL DEFAULT 0, open REAL NOT NULL DEFAULT 0,
        high REAL NOT NULL DEFAULT 0, low REAL NOT NULL DEFAULT 0,
        close REAL NOT NULL DEFAULT 0, volume REAL NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0
    )''',
    'trading_sim_sessions': '''CREATE TABLE trading_sim_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        initial_capital REAL NOT NULL DEFAULT 100000,
        current_cash REAL NOT NULL DEFAULT 100000,
        symbols TEXT NOT NULL DEFAULT '[]', start_date TEXT NOT NULL DEFAULT '',
        end_date TEXT NOT NULL DEFAULT '', step_days INTEGER NOT NULL DEFAULT 5,
        current_sim_date TEXT NOT NULL DEFAULT '',
        total_steps INTEGER NOT NULL DEFAULT 0,
        completed_steps INTEGER NOT NULL DEFAULT 0,
        total_pnl REAL NOT NULL DEFAULT 0, total_trades INTEGER NOT NULL DEFAULT 0,
        winning_trades INTEGER NOT NULL DEFAULT 0,
        config_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
    )''',
    'trading_strategies': "CREATE TABLE trading_strategies (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            name TEXT NOT NULL DEFAULT '',\n            type TEXT NOT NULL DEFAULT 'observation',\n            status TEXT NOT NULL DEFAULT 'active',\n            logic TEXT NOT NULL DEFAULT '',\n            scenario TEXT NOT NULL DEFAULT '',\n            assets TEXT NOT NULL DEFAULT '',\n            result TEXT NOT NULL DEFAULT '',\n            source TEXT NOT NULL DEFAULT 'manual',\n            created_at TEXT NOT NULL DEFAULT '',\n            updated_at TEXT NOT NULL DEFAULT ''\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_strategy_combo_outcomes': "CREATE TABLE trading_strategy_combo_outcomes (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            cycle_id TEXT NOT NULL DEFAULT '',\n            strategy_ids_json TEXT NOT NULL DEFAULT '[]',\n            market_regime TEXT NOT NULL DEFAULT 'unknown',\n            actual_return_pct REAL NOT NULL DEFAULT 0,\n            benchmark_return_pct REAL NOT NULL DEFAULT 0,\n            excess_return_pct REAL NOT NULL DEFAULT 0,\n            outcome TEXT NOT NULL DEFAULT '',\n            outcome_notes TEXT NOT NULL DEFAULT '',\n            evaluated_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_strategy_compatibility': "CREATE TABLE trading_strategy_compatibility (\n            pair_key TEXT PRIMARY KEY,\n            strategy_id_a INTEGER NOT NULL,\n            strategy_id_b INTEGER NOT NULL,\n            compatibility_score REAL NOT NULL DEFAULT 0,\n            sample_count INTEGER NOT NULL DEFAULT 0,\n            updated_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_strategy_deployments': "CREATE TABLE trading_strategy_deployments (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            cycle_id TEXT NOT NULL DEFAULT '',\n            market_condition_json TEXT NOT NULL DEFAULT '{}',\n            strategy_ids_json TEXT NOT NULL DEFAULT '[]',\n            strategy_names_json TEXT NOT NULL DEFAULT '[]',\n            deployed_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_strategy_failures': "CREATE TABLE trading_strategy_failures (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            strategy_id INTEGER NOT NULL,\n            strategy_name TEXT NOT NULL DEFAULT '',\n            cycle_id TEXT NOT NULL DEFAULT '',\n            market_regime TEXT NOT NULL DEFAULT 'unknown',\n            actual_return_pct REAL NOT NULL DEFAULT 0,\n            excess_return_pct REAL NOT NULL DEFAULT 0,\n            failure_notes TEXT NOT NULL DEFAULT '',\n            created_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_strategy_groups': "CREATE TABLE trading_strategy_groups (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            name TEXT NOT NULL UNIQUE,\n            description TEXT NOT NULL DEFAULT '',\n            strategy_ids TEXT NOT NULL DEFAULT '[]',\n            risk_level TEXT NOT NULL DEFAULT 'medium',\n            created_at TEXT NOT NULL DEFAULT '',\n            updated_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_strategy_performance': "CREATE TABLE trading_strategy_performance (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            strategy_id INTEGER NOT NULL,\n            strategy_group_id INTEGER,\n            period_start TEXT NOT NULL DEFAULT '',\n            period_end TEXT NOT NULL DEFAULT '',\n            return_pct REAL NOT NULL DEFAULT 0,\n            benchmark_return_pct REAL NOT NULL DEFAULT 0,\n            max_drawdown REAL NOT NULL DEFAULT 0,\n            sharpe_ratio REAL,\n            win_rate REAL,\n            trade_count INTEGER NOT NULL DEFAULT 0,\n            source TEXT NOT NULL DEFAULT 'live',\n            detail_json TEXT NOT NULL DEFAULT '{}',\n            created_at TEXT NOT NULL DEFAULT '',\n            decision_id INTEGER,\n            actual_outcome TEXT NOT NULL DEFAULT '',\n            lesson TEXT NOT NULL DEFAULT '',\n            evaluated_at TEXT NOT NULL DEFAULT ''\n        )",
    'trading_target': "CREATE TABLE trading_target (\n            user_id INTEGER NOT NULL,\n            symbol TEXT NOT NULL,\n            asset_name TEXT NOT NULL DEFAULT '',\n            target_weight REAL NOT NULL DEFAULT 0,\n            rationale TEXT NOT NULL DEFAULT '',\n            proposed_by TEXT NOT NULL DEFAULT 'ai',\n            approved INTEGER NOT NULL DEFAULT 0,\n            valid_from TEXT NOT NULL DEFAULT '',\n            updated_at TEXT NOT NULL DEFAULT '',\n            PRIMARY KEY (user_id, symbol)\n        )",
    'trading_trade_queue': "CREATE TABLE trading_trade_queue (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            batch_id TEXT NOT NULL DEFAULT '',\n            symbol TEXT NOT NULL,\n            asset_name TEXT NOT NULL DEFAULT '',\n            action TEXT NOT NULL DEFAULT 'buy',\n            shares REAL NOT NULL DEFAULT 0,\n            amount REAL NOT NULL DEFAULT 0,\n            price REAL NOT NULL DEFAULT 0,\n            est_fee REAL NOT NULL DEFAULT 0,\n            fee_detail TEXT NOT NULL DEFAULT '',\n            reason TEXT NOT NULL DEFAULT '',\n            status TEXT NOT NULL DEFAULT 'pending',\n            created_at TEXT NOT NULL DEFAULT '',\n            executed_at TEXT NOT NULL DEFAULT '',\n            rolled_back_at TEXT NOT NULL DEFAULT ''\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_transactions': "CREATE TABLE trading_transactions (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            symbol TEXT NOT NULL,\n            asset_name TEXT NOT NULL DEFAULT '',\n            type TEXT NOT NULL DEFAULT 'buy',\n            shares REAL NOT NULL DEFAULT 0,\n            price REAL NOT NULL DEFAULT 0,\n            amount REAL NOT NULL DEFAULT 0,\n            note TEXT NOT NULL DEFAULT '',\n            tx_date TEXT NOT NULL DEFAULT '',\n            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)\n        , user_id INTEGER NOT NULL DEFAULT 1)",
    'trading_user_config': "CREATE TABLE trading_user_config (\n            user_id INTEGER NOT NULL,\n            key TEXT NOT NULL,\n            value TEXT NOT NULL DEFAULT '',\n            PRIMARY KEY (user_id, key)\n        )",
}

# Durable owner data is partitioned by an explicit identity in its physical
# sidecar key. Shared reference/cache data uses owner 0. Existing tables that
# predate user_id are assigned owner 1 only by the one-time migration.
OWNER_SCOPED_TABLES = frozenset({
    "trading_action",
    "trading_autopilot_cycles",
    "trading_autopilot_recommendations",
    "trading_bg_tasks",
    "trading_daily_briefing",
    "trading_decision_history",
    "trading_holdings",
    "trading_livetest_journal",
    "trading_livetest_learning",
    "trading_livetest_positions",
    "trading_livetest_sessions",
    "trading_position",
    "trading_recommendations",
    "trading_sim_journal",
    "trading_sim_positions",
    "trading_sim_sessions",
    "trading_strategies",
    "trading_strategy_combo_outcomes",
    "trading_strategy_compatibility",
    "trading_strategy_deployments",
    "trading_strategy_failures",
    "trading_strategy_groups",
    "trading_strategy_performance",
    "trading_target",
    "trading_trade_queue",
    "trading_transactions",
    "trading_user_config",
})

# Constraints required by the runtime upsert statements. Some legacy SQLite
# tables lost these indexes during an earlier SQLAlchemy copy; the sidecar
# representation restores the intended logical contract without mutating them.
UNIQUE_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "trading_intel_crawl_log": (("crawl_date", "category", "source_key"),),
    "trading_livetest_sessions": (("session_id",),),
    "trading_sim_indices": (("secid", "date"),),
    "trading_sim_macro": (("indicator", "date"),),
    "trading_sim_prices": (("symbol", "date"),),
    "trading_sim_sessions": (("session_id",),),
}


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Backend-neutral identity and shape for one logical trading table."""

    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    owner_scoped: bool


def initialize_query_schema(connection: sqlite3.Connection) -> None:
    """Initialize the private in-memory SQL evaluator from canonical DDL."""
    for ddl in TABLE_DDL.values():
        connection.execute(ddl)
    for table_name, keys in UNIQUE_KEYS.items():
        for index, columns in enumerate(keys):
            rendered = ", ".join(f'"{column}"' for column in columns)
            connection.execute(
                f'CREATE UNIQUE INDEX "repo_unique_{index}_{table_name}" '
                f'ON "{table_name}" ({rendered})'
            )


def _describe_tables() -> dict[str, TableSpec]:
    connection = sqlite3.connect(":memory:")
    try:
        initialize_query_schema(connection)
        specs: dict[str, TableSpec] = {}
        for table_name in TABLE_DDL:
            rows = list(connection.execute(f'PRAGMA table_info("{table_name}")'))
            primary_key = tuple(
                row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5]
            )
            if not primary_key:
                raise RuntimeError(f"{table_name} has no primary key")
            specs[table_name] = TableSpec(
                name=table_name,
                columns=tuple(row[1] for row in rows),
                primary_key=primary_key,
                owner_scoped=table_name in OWNER_SCOPED_TABLES,
            )
        return specs
    finally:
        connection.close()


TABLE_SPECS = _describe_tables()
SHARED_TABLES = frozenset(TABLE_SPECS) - OWNER_SCOPED_TABLES


__all__ = [
    "OWNER_SCOPED_TABLES",
    "SHARED_TABLES",
    "TABLE_DDL",
    "TABLE_SPECS",
    "TableSpec",
    "initialize_query_schema",
]
