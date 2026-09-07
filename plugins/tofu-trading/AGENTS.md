# Trading plugin guidance

## Scope

This directory owns the optional `tofu-trading` distribution, including its
entry-point registration, owner-scoped storage namespace, bounded workers,
simulation/backtest engines, risk policy, and application UI.

## Editing rules

- Dependency direction is one-way: the host discovers the plugin; core code
  never imports `tofu_trading`. Do not add a second registration path.
- Durable access uses the declared `tofu.storage` manifest and explicit owner
  identity. Never import a database driver, select a backend, or open a host
  database directly.
- Models propose intent; deterministic risk, permission, idempotency, limit,
  and settlement boundaries authorize execution. Failure defaults to no trade.
- Every worker, feed, retry, cache, simulation, and transaction is bounded and
  shuts down through the plugin lifecycle.
- Host compatibility imports are migration debt and may only shrink; prefer a
  declared public facade/entry-point carrier over new `lib.*` reach-through.

## Verification

Run `uv run --package tofu-trading pytest plugins/tofu-trading/tests`, then the
root plugin registry, storage manifest, identity, and monorepo contract tests.
