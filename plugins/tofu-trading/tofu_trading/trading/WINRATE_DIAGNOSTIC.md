# Win-Rate Discrepancy Diagnostic: 75% Backtest → 41% Live

**Date:** 2026-04-03
**Author:** Auto-generated diagnostic from codebase analysis

---

## Executive Summary

The 75% backtest win-rate vs 41% live win-rate is not a parameter tuning problem —
it is a **fundamental architecture problem**. The backtest engine and live trading
system are two completely separate decision systems that share almost no logic.

---

## Root Cause 1: Different Decision Systems

### Backtest Engine (75% win-rate)
- **Location:** `lib/trading_backtest_engine/strategies.py` — `StrategyMixin` class
- **Decision method:** 7 hardcoded quantitative strategies:
  - `_strategy_signal_driven()` — uses RSI/MACD/EMA thresholds
  - `_strategy_dca()` — mechanical dollar-cost averaging
  - `_strategy_mean_reversion()` — Bollinger Band + RSI contrarian
  - `_strategy_trend_following()` — MA alignment + MACD confirmation
  - `_strategy_adaptive()` — regime-aware blend of above
  - `_strategy_dca_signal()` — DCA with signal overlay
  - `_init_buy_and_hold()` — initial equal-weight allocation
- **Signal source:** `lib/trading_signals.compute_signal_snapshot()` — deterministic
  technical indicators (EMA, MACD, RSI, Bollinger, Volume)
- **Risk management:** `lib/trading_risk` — `StopLossManager`, `DrawdownProtector`,
  Kelly sizing — all with fixed numeric thresholds

### Live Trading (41% win-rate)
- **Location:** `lib/trading_autopilot/cycle.py` → `run_autopilot_cycle()`
  and `lib/trading/brain/pipeline.py` → `run_brain_analysis()`
- **Decision method:** LLM mega-prompt containing:
  - Holdings context, cash, fees
  - Intelligence crawl results (news, macro, policy)
  - KPI evaluations (uses same signals as backtest, but as **input** to LLM)
  - Bull vs Bear debate output
  - Adaptive Decision Engine output
  - Strategy evolution context
- **Signal source:** Quant signals are computed but then **passed to the LLM
  as text**, which may override, misinterpret, or ignore them
- **Risk management:** LLM is **instructed** to set stop-loss/take-profit
  but compliance is voluntary — no hard enforcement

### The Gap
The backtest tests **Strategy X** (deterministic quant rules).
Live uses **Strategy Y** (LLM natural language reasoning).
The 75% number tells us nothing about how Strategy Y will perform.

---

## Root Cause 2: Perfect vs Imperfect Data

### Backtest
- `BacktestEngine.run()` in `engine.py` receives complete `nav_data: dict[str, list[dict]]`
  with gap-free daily NAV series
- `compute_signal_snapshot()` always has sufficient history for all indicators
  (14-day RSI, 26-day MACD, 20-day Bollinger)
- Intel-aware backtest (`intel_backtest.py`) uses time-locked DB queries but
  still operates on complete, verified historical data

### Live Trading
- `_gather_context()` in `cycle.py` calls `get_latest_price()` which can fail
  (stale cache, market closed, API timeout)
- NAV data may be from yesterday if fetched after market close
- Intelligence crawl may fail partially — some categories may have no data
- Market data (`lib/trading/market.py`) may timeout on indices/sectors

### Impact
When the backtest runs signal_driven strategy, it always has perfect data to
compute signals. Live, signals may be computed on stale or incomplete data,
leading to different decisions.

---

## Root Cause 3: No Shared Strategy Interface

There is no common interface between:
1. `StrategyMixin._strategy_signal_driven()` — backtest strategy
2. `AdaptiveDecisionEngine.make_decision()` — live adaptive engine
3. LLM mega-prompt in `build_autopilot_prompt()` / `_build_brain_prompt()`

Each uses its own:
- Signal interpretation thresholds
- Position sizing logic
- Entry/exit rules
- Risk management approach

The backtest engine can't run "what the LLM would have decided" because
there's no way to serialize the LLM's decision logic into backtest strategy code.

**Exception:** `lib/trading/llm_simulator.py` (the Simulator tab) DOES use
LLM-based decisions during simulation. Its results would be a more accurate
predictor of live performance than the backtest engine results.

---

## Root Cause 4: No Feedback Loop

### What exists
- `lib/trading_autopilot/backtest_learner.py` — analyzes backtest decisions
  to find strategy × regime effectiveness, creates learning reports
- `lib/trading_autopilot/strategy_learner.py` — tracks live strategy outcomes
- `lib/trading_autopilot/outcome.py` — tracks recommendation accuracy

### What's missing
1. **Backtest engine never reads learning data** — `StrategyMixin` strategies
   use hardcoded thresholds (`buy_threshold=8`, `strong_buy=20` in `config.py`)
   that never change based on live outcomes
2. **LLM prompt includes learning context but doesn't enforce it** — the learning
   report is injected into the mega-prompt as text, but the LLM may ignore it
3. **No closed-loop parameter optimization** — live win-rate data doesn't flow
   back to adjust backtest parameters or strategy weights

---

## Actionable Recommendations

### Immediate (this session)
1. ✅ Create `lib/trading/strategy_interface.py` — unified strategy Protocol
   that both backtest and live systems can implement
2. ✅ Document this diagnostic for the team

### Short-term
3. **Use LLM Simulator results instead of backtest engine** for performance
   expectations. The simulator (`lib/trading/llm_simulator.py`) uses actual
   LLM decisions, making its results more comparable to live performance.
4. **Add hard risk enforcement after LLM decisions** — parse LLM output,
   then apply `StopLossManager` / `DrawdownProtector` rules from
   `lib/trading_risk` to veto or modify LLM recommendations that violate
   quantitative risk limits.

### Medium-term
5. **Create an LLM-backtest strategy** — implement a `StrategyMixin` method
   that replays historical decisions through an LLM (expensive but accurate).
   Use the same approach as `llm_simulator.py` but within the backtest engine.
6. **Close the feedback loop** — have `backtest_learner.py` output directly
   update `config.py` thresholds (with approval gate) instead of just
   generating text reports.

---

## Key File References

| Component | File | Key Function |
|---|---|---|
| Backtest strategies | `lib/trading_backtest_engine/strategies.py` | `StrategyMixin._strategy_signal_driven()` |
| Backtest engine | `lib/trading_backtest_engine/engine.py` | `BacktestEngine.run()` |
| Backtest config (thresholds) | `lib/trading_backtest_engine/config.py` | `BUY_THRESHOLD`, `STRONG_BUY_THRESHOLD` |
| Live autopilot cycle | `lib/trading_autopilot/cycle.py` | `run_autopilot_cycle()` |
| Live brain pipeline | `lib/trading/brain/pipeline.py` | `run_brain_analysis()` |
| Adaptive engine | `lib/trading_autopilot/adaptive_decision_engine.py` | `AdaptiveDecisionEngine.make_decision()` |
| LLM simulator | `lib/trading/llm_simulator.py` | `LLMSimulator.simulate()` |
| Signal computation | `lib/trading_signals.py` | `compute_signal_snapshot()` |
| Risk management | `lib/trading_risk.py` | `StopLossManager`, `DrawdownProtector` |
| Backtest learning | `lib/trading_autopilot/backtest_learner.py` | `analyze_backtest_decisions()` |
| Strategy learning | `lib/trading_autopilot/strategy_learner.py` | `build_learning_prompt_section()` |
| Outcome tracking | `lib/trading_autopilot/outcome.py` | `track_recommendation_outcomes()` |
| Unified strategy interface | `lib/trading/strategy_interface.py` | `TradingStrategy` Protocol (**NEW**) |
