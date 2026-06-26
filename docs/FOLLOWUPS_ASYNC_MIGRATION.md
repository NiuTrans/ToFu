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

### 2. HTTP test coverage for swarm-converted trading handlers — DONE / MOOT (trading externalized)
> ⚠️ The trading subsystem was extracted to the standalone `tofu-trading` package
> (2026-06); `tests/test_trading_handlers_async.py` and the trading modules below
> NO LONGER EXIST in this repo. This item is historical — any remaining trading
> test work now lives in the `tofu-trading` package, not here.

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

### 3. PostgreSQL full-text search tsvector Phase-1 — DONE
**Status:** implemented (verified 2026-06-25). `routes/conversations_search.py`
Phase-1 now has a real PG path: a GIN-indexed `search_tsv` tsvector
(`idx_conv_search_tsv`) queried via `search_tsv @@ to_tsquery('simple', 'w1:* &
w2:* …')` prefix match (`conversations_search.py:~80`), gated on the PG backend;
SQLite keeps FTS5 `MATCH`. The portable `LIKE` scan remains only as the Phase-2
substring fallback for matches Phase-1 misses (e.g. mid-word substrings). The
original "PG is LIKE-only" gap is closed — no remaining action.

### 4. Trading quant core unit tests — MOOT (trading externalized to tofu-trading)
> ⚠️ `lib/trading_signals.py`, `lib/trading_risk.py`, `tests/test_trading_quant.py`,
> `lib/trading_autopilot/*`, `lib/trading_strategy_engine/*` and the rest of the
> trading code referenced below were EXTRACTED to the `tofu-trading` package
> (2026-06) and are gone from this repo. The remaining-coverage backlog moved
> with them — it is no longer actionable here. (Original note retained below for
> history.)

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

### 7. `json_store` inter-process locking — DONE (2026-06-25)
`update_json_atomic` now wraps its read-modify-write in a blocking POSIX
`fcntl.flock(LOCK_EX)` on a sidecar `<path>.lock` file (inside the existing
`threading.Lock`), so concurrent PROCESSES on a shared mount can't lose updates.
Degrades to a no-op where advisory locks are unavailable (Windows / no `fcntl` /
FS without flock), preserving the in-process guarantee. Sidecar (not the data
file) is locked because `os.replace` swaps the data inode. Guarded by
`tests/test_json_store.py::test_update_json_atomic_inter_process_safe` (4 procs ×
30 cross-process increments, no lost updates). Module docstring corrected.

### 8. Version source-of-truth
`VERSION` and `pyproject.toml` are aligned at **0.13.0** (re-synced 2026-06-25;
they had drifted — `VERSION`=0.13.0 vs `pyproject.toml`=0.11.0). Keep them in
sync: `lib/version.py` reads `VERSION` at runtime, so `pyproject.toml` (packaging
metadata) MUST be bumped in the same change set as any `VERSION` bump — the two
have no automatic link, which is exactly how the drift happened.


### 9. `async_dispatch_stream` — NOW LIVE via `POST /api/v1/chat/stream-direct` (2026-06-25)
**Status:** DONE. The native-async streaming path is in production via a new
on-loop endpoint — `routes/api_v1/chat_direct.py::chat_stream_direct` →
`run_direct_stream` drives `async_dispatch_stream` directly on the event loop
(no `spawn_task`, no thread worker), bridging its on-loop `on_content`/
`on_thinking` callbacks through an `asyncio.Queue` into an async SSE generator.
Single-turn, pure-text(±thinking), SSE-only; admission-gated; NO tool loop/MCP/
multi-round (those stay on the thread orchestrator via `/chat/completions`). The
thread-worker path is untouched. Guarded by `tests/test_chat_stream_direct.py`
(6) + the async-integrity scan now covering `chat_direct.py` +
`api_v1_chat_direct.` coroutine-ness. Original reserved-by-design note (now
historical):

**Was:** not a defect, not dead code (verified 2026-06-25).
`lib/llm_dispatch/api.py::async_dispatch_stream` is a fully-implemented,
test-covered (`tests/test_async_dispatch_stream.py`) native-async streaming
dispatcher that drives the httpx async transport ON the event loop. It shares
`_StreamRetryState` + `_adapt_stream_body_for_slot` with the sync
`dispatch_stream`, so the two stay in lockstep.
**Why it has no production caller:** EVERY streaming path — the UI chat worker
AND the `/v1/chat/completions` + `/v1/messages` compat endpoints — runs the task
via `spawn_task` → `asyncio.to_thread` on an OFF-loop worker thread (the `async
def` route just tails the task event buffer). A sync worker thread cannot
`await`, and correctly uses the sync `dispatch_stream` (`requests`). Making this
go live requires EITHER an async `run_task` (rejected — see §1) OR a genuinely
on-loop streaming endpoint that bypasses the thread worker (a real feature, not
a wiring change).
**Do NOT** "wire it" by wrapping it in `asyncio.run()` inside the thread worker —
that spins a throwaway event loop per stream and is a pure regression over the
sync path. Keep it as the tested substrate for a future on-loop streaming
caller. See the ⚠️ block in its docstring.
