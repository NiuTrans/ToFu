# tofu-trading

Trading subsystem for **Tofu (豆腐)** — extracted from the core repo into a
installable workspace plugin so trading keeps an explicit boundary from the
core agent backend while changing atomically with host contracts.

It provides portfolio tracking, market intel crawling, an autonomous decision
loop (autopilot), a backtest engine, and a strategy DSL/runner, plus the web
UI and REST surface under `/api/v1/trading/*`.

## How it plugs in

The plugin mounts into a Tofu host purely through **entry points** — core has
no compile-time knowledge of trading. The dependency direction is strictly
one-way: `tofu_trading` uses the host's public runtime/storage seams; core
never imports `tofu_trading`.

| Entry-point group | Registrar | What the host does with it |
|---|---|---|
| `tofu.blueprints` | `tofu_trading.web:register` | mounts the trading API/page Blueprints |
| `tofu.startup` | `tofu_trading.web:start_workers` | verifies storage migration, then starts bounded background workers |
| `tofu.task_runtimes` | `tofu_trading.web:get_task_runtimes` | exposes the `trading-sim` task runtime |
| `tofu.storage` | `tofu_trading.storage_manifest:MANIFEST` | exposes a data-only `storage.v1` manifest; no callback or connection crosses the boundary |
| `tofu.flags` | `tofu_trading.flags:register` | declares the `trading_enabled` feature flag |

When the package is installed alongside a compatible `tofu-agent` host, the host's
registries discover these entry points automatically. Nothing else is wired by
hand.

## Install (development)

```bash
# From the Tofu monorepo root:
uv sync --all-packages

# Enable plugin discovery + trading, then (re)start the host:
TOFU_DISCOVER_PLUGINS=1 TRADING_ENABLED=1 uv run server.py
```

On startup the host logs:

```
[BlueprintRegistry] loaded 9 blueprint(s) from plugin 'trading'
[tofu-trading] legacy migration verified tables=36 rows=...
[tofu-trading] sidecar storage ready: migration=legacy-v1 manifest=1 tables=36 rows=...
[tofu-trading] background workers started
```

## Layout

```
tofu_trading/
  trading/                  market data, intel, screening, NAV, portfolio
  trading_autopilot/        autonomous decision loop
  trading_backtest_engine/  backtest simulator
  trading_strategy_engine/  strategy DSL + runner
  trading_signals.py
  trading_risk.py
  trading_tasks.py
  web/
    handlers/               route handler bodies (was routes/trading_*.py)
    v1/                      v1 Blueprint definitions (was routes/api_v1/trading/)
    __init__.py             tofu.blueprints registrar -> register()
  storage_manifest.py       declarative tofu.storage contract
  storage_schema.py         logical row schemas + owner boundaries
  storage.py                owner-bound repository / bounded SQL evaluator
  transactions.py           atomic sidecar transaction boundary
  flags.py                  tofu.flags registrar
  static/ templates/        trading.html, trading.css, static/js/trading/*
```

## Compatibility

Version 0.2 is pinned to `tofu-agent>=0.17,<0.18` and is sidecar-native. Runtime code
does not import `lib.database`, open `data/tofu.db`, choose SQLite/PostgreSQL,
or send SQL over RPC. Existing DB-API-shaped business code runs only against a
private, bounded in-memory evaluator; durable reads and writes are versioned
documents sent through named manifest operations.

## Legacy migration

The first startup after upgrading registers namespace `tofu.trading`, scans
the 36 exactly declared legacy tables inside the sidecar, and copies every row
into the plugin's `rows` collection. Each table is accepted only after its row
count and order-independent SHA-256 digest match. The completion marker is
written last, so a stopped/failed run retries safely.

- Legacy tables are never modified or deleted.
- Rows with an existing `user_id` retain it; older owner-scoped rows are
  assigned to the named default owner (`1`) during this one-time import.
- Runtime keys include logical table + owner + primary key, so one owner's
  repository scan cannot load another owner's rows.
- Migration pages are capped at 500 rows; copy batches are capped at 100 rows
  and 1 MiB. A runtime transaction is atomic across logical tables and capped
  at 1,000 rows / 8 MiB. A query projection is capped at 50,000 rows / 64 MiB.
- The migration is a one-way cutover: old tables remain as recovery evidence
  but are not dual-written after the marker. Back up before downgrading and
  reconcile post-cutover writes explicitly.
