# Follow-ups — Native-Async Migration & Review

> Tracked backlog from the native-async handler migration and the senior-review
> bug sweep. Each item is concrete and objectively verifiable. Follows the
> `ROADMAP.md` ground rules: **no dates / SLOs / dollar figures**; items that
> touch a hyperparameter, model-routing table, DB schema, or security-sensitive
> code are flagged `[§10]` and require explicit sign-off + an
> `audit_log('config_change', …, approved_by='user')` entry (CLAUDE.md §10).

## Done (this work stream — for context)

- Native-async stack: `lib/database/aio.py` (await-able DB facade
  `async_execute/fetchone/fetchall/transaction/run_pooled`, leak-safe via a
  dedicated bounded executor + pool checkout/return), `async_parse_body`,
  dual-mode `_db_safe`, and `async_dispatch_stream` (real httpx loop).
- ~140 route handlers converted from sync `def` → native `async def` across 17
  route files (conversations is the gold-reference; billing / artifacts /
  daily_report / paper / trading_* via parallel sweep).
- Bugs fixed: LLM inflight-counter leak; backtest `win_rate` (cost-basis) +
  carry-forward day-count; `content_ref` slice bounds; swarm `RateLimiter`
  permit-while-sleeping; `health_check` hardcoded backend; `_sql_translate`
  nested `json_extract`, unmapped-table silent-drop, missing
  `trading_strategy_compatibility` PK; `vertical.py` proxy bypass; the 4
  async-conversion bugs (app-context-in-thread, `async_parse_body` LocalProxy,
  search `MATCH`-on-PG, trading-boot `app_context`).
- Guards: `tests/test_async_handler_integrity.py` (static blocking-call scan +
  no-unconverted-route-handler check with a documented carve-out allowlist +
  live-app coroutine assertions + billing auth-gate HTTP smoke).

---

## Open follow-ups

### 1. Stage 3 — orchestrator `run_task` is intentionally NOT async
**Status:** decision recorded, not a defect.
`lib/tasks_pkg/orchestrator.py::run_task` runs off the event loop via
`spawn_task` → `asyncio.to_thread`. Converting it to native `async def` would
give **no** loop-unblocking benefit (it is not on the loop) and would actively
risk starving request handlers (CPU-bound work would move onto the loop), plus
a high-risk rewrite of a 900-line hot path with sub-thread spawns and the
SSE-queue bridge. **Recommendation: keep thread-based.** Revisit only if the
data layer becomes natively async (psycopg3-async) end-to-end.

### 2. HTTP test coverage for swarm-converted trading handlers — DONE
`tests/test_trading_handlers_async.py` boots a `TRADING_ENABLED=1` SUBPROCESS
(trading bps aren't registered in the default `TRADING_ENABLED=0` session app)
and asserts every trading view is a coroutine (minus the `common.trading_page`
static-page allowlist). **This caught a real bug:** `lib/rate_limiter.py::
rate_limit` was sync-only, silently breaking 9 `@rate_limit`-decorated async
handlers (4 in `routes/trading_intel.py`). Fixed by making `rate_limit`
dual-mode. LESSON: a non-dual-mode shared decorator is invisible to source-level
checks — only a live-app `iscoroutinefunction(view_functions[name])` probe
catches it. (Full route-body HTTP smoke with auth/data remains a possible
future extension, but the leaked-coroutine class of bug is now guarded.)

### 3. PostgreSQL full-text search is `LIKE`-only
**Gap:** `routes/conversations_search.py` Phase-1 uses SQLite FTS5
`conversations_fts MATCH`, now correctly gated to SQLite only. On PostgreSQL
search falls back to the portable Phase-2 `LIKE` scan — correct but not
index-accelerated.
**Action:** add a PG `tsvector` / `websearch_to_tsquery` Phase-1 path (the PG
schema already has tsvector infrastructure). `[§10]` — touches DB schema if a
generated tsvector column / GIN index is added; requires sign-off +
`_SCHEMA_VERSION` bump.

### 4. Trading quant core unit tests — DONE (core), partial (autopilot/strategy)
`tests/test_trading_quant.py` (42 golden-value tests) now covers
`lib/trading_signals.py` (SMA/EMA/RSI/MACD/Bollinger/momentum/drawdown,
regime detection, signal snapshot + no-future-leak), `lib/trading_risk.py`
(Kelly, vol-target sizing, risk-parity, StopLossManager, DrawdownProtector
circuit-breaker, regime params, `filter_trade_decisions`), and
`lib/trading_backtest_engine/reporting.py::compute_metrics` (total/annualised
return, max-drawdown, cost-basis win-rate). **Still uncovered:**
`lib/trading_autopilot/*`, `lib/trading_strategy_engine/*`, and
`lib/trading/*` (intel/market/screening) — and the duplicated
Sortino/annualisation between `reporting.py` and `risk_metrics.py` should still
be consolidated. Extend the same golden-value pattern there next.

### 5. `_core_schema.py` (SQLAlchemy Core) adoption — DONE (first batch wired)
**Status:** live. `lib/database/_core_schema.py` is no longer inert: 12 tables
(`users`, `conversations`, `task_results`, `task_events`, `chat_artifacts`,
`transcript_archive`, `daily_cost_cache`, `paper_reports`, `paper_library`,
`paper_translations`, + kv stores) are created from their Core definitions via
`create_if_absent` in `_schema_pg.py` / `_schema_sqlite.py`, and ~10 call-sites
use `_core_schema.upsert`. The generated DDL is byte-equivalent to the legacy
hand-DDL (no `_SCHEMA_VERSION` bump), proven by
`tests/test_core_schema_parity.py` (29 tests, green).
**Remaining:** migrate the rest of the hand-DDL tables off `_schema_*.py` +
`_sql_translate.py` opportunistically, and route the **next new table** through
`_core_schema.define_table`. `[§10]` — registering a new live table is a DB
schema change needing sign-off + `_SCHEMA_VERSION` bump.

### 6. `_sql_translate.py` remaining gaps (now tracked by tests)
`tests/test_sql_translate.py::TestPkMapCompleteness` greps the repo for
`INSERT OR REPLACE INTO <table>` and fails if a table is missing from
`_PK_MAP` (prevents silent-drop regressions). Keep this green: any new
`INSERT OR REPLACE` table must add its PK to `_PK_MAP`
(`lib/database/_sql_translate.py`) or PG writes will raise.

### 7. `json_store` is thread-only, not inter-process
`lib/json_store.py` per-path locks are `threading` locks; the docstring
oversells "atomic" for multi-process deployments. Either document the
single-process scope explicitly or add an `flock`-based file lock if
multi-process JSON writes are a real deployment shape.

### 8. Version source-of-truth
`VERSION` and `pyproject.toml` are now aligned (0.11.0). Keep them in sync;
`lib/version.py` reads `VERSION` at runtime, so `pyproject.toml` must be
updated in the same change set as any `VERSION` bump.
