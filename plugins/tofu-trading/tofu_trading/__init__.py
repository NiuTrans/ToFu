"""tofu_trading — Trading subsystem plugin for the Tofu (豆腐) host.

A self-contained feature package that mounts into a Tofu host via entry points
(``tofu.blueprints`` / ``tofu.storage`` / ``tofu.flags``).  The dependency
direction is strictly one-way: this package imports core infrastructure from
``lib.*`` (storage, llm, api_response, …); core never imports ``tofu_trading``.

Sub-packages:
  trading/                  — market data, intel, screening, NAV, portfolio
  trading_autopilot/        — autonomous decision loop
  trading_backtest_engine/  — backtest simulator
  trading_strategy_engine/  — strategy DSL + runner
  trading_signals / _risk / _tasks — top-level helpers
  web/                      — Flask blueprints (handlers + v1 defs + registrar)
  storage_manifest.py / flags.py — declarative entry points
"""

__version__ = '0.2.0'
