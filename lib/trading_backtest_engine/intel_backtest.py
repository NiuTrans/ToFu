"""lib/trading_backtest_engine/intel_backtest.py — Intel-Aware Backtesting Engine.

The core innovation: backtesting with TIME-LOCKED intelligence database.

At each decision point T, the engine:
  1. Locks the intel DB to only show items published BEFORE T
  2. Feeds time-locked intel to the meta-strategy selector
  3. The meta-strategy detects market regime from quant signals + intel features
  4. Selects optimal strategy combo for that regime
  5. Executes the selected strategy's trading logic
  6. Records the deployment for the learning engine

This is the ONLY correct way to simulate real decision conditions:
  - No future information leakage
  - Strategy selection mirrors live autopilot behavior
  - Every decision is explainable (which strategies were active and why)

Architecture:
  IntelBacktestConfig  — extends default config with intel-aware params
  IntelBacktestEngine  — wraps BacktestEngine with meta-strategy + intel
  IntelDecisionRecord  — records each decision for post-hoc learning
  run_intel_backtest   — high-level entry point
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from lib.log import get_logger
from lib.trading.intel_timeline import (
    build_intel_context_at,
    build_regime_intel_features,
    get_intel_snapshot_dates,
)
from lib.trading_autopilot.meta_strategy import (
    _SUITABILITY_MATRIX,
    MarketCondition,
    _combo_key,
)
from lib.trading_risk import get_regime_risk_params
from lib.trading_signals import compute_signal_snapshot

from .config import DEFAULT_CONFIG
from .reporting import compute_metrics
from .state import BacktestState, DrawdownProtector, StopLossManager

logger = get_logger(__name__)

__all__ = [
    'IntelBacktestConfig',
    'IntelBacktestEngine',
    'IntelDecisionRecord',
    'run_intel_backtest',
]


# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

class IntelBacktestConfig:
    """Configuration for intel-aware backtesting.

    Extends the standard backtest config with:
      - Intel integration parameters
      - Meta-strategy selection parameters
      - Decision recording parameters
    """

    def __init__(self, **kwargs):
        # ── Standard backtest params ──
        self.initial_capital = kwargs.get('initial_capital', DEFAULT_CONFIG['initial_capital'])
        self.buy_fee_rate = kwargs.get('buy_fee_rate', DEFAULT_CONFIG['buy_fee_rate'])
        self.sell_fee_rate = kwargs.get('sell_fee_rate', DEFAULT_CONFIG['sell_fee_rate'])
        self.short_sell_penalty = kwargs.get('short_sell_penalty', DEFAULT_CONFIG['short_sell_penalty'])
        self.min_holding_days = kwargs.get('min_holding_days', DEFAULT_CONFIG['min_holding_days'])
        self.min_signal_history = kwargs.get('min_signal_history', DEFAULT_CONFIG['min_signal_history'])
        self.max_positions = kwargs.get('max_positions', DEFAULT_CONFIG['max_positions'])
        self.enable_stop_loss = kwargs.get('enable_stop_loss', DEFAULT_CONFIG['enable_stop_loss'])
        self.enable_drawdown_protection = kwargs.get('enable_drawdown_protection', DEFAULT_CONFIG['enable_drawdown_protection'])

        # Signal thresholds
        self.buy_threshold = kwargs.get('buy_threshold', DEFAULT_CONFIG['buy_threshold'])
        self.strong_buy_threshold = kwargs.get('strong_buy_threshold', DEFAULT_CONFIG['strong_buy_threshold'])
        self.sell_threshold = kwargs.get('sell_threshold', DEFAULT_CONFIG['sell_threshold'])
        self.strong_sell_threshold = kwargs.get('strong_sell_threshold', DEFAULT_CONFIG['strong_sell_threshold'])

        # ── Intel-aware params ──
        self.intel_enabled = kwargs.get('intel_enabled', True)
        self.only_confident_dates = kwargs.get('only_confident_dates', True)
        self.intel_lookback_days = kwargs.get('intel_lookback_days', 14)
        self.decision_frequency = kwargs.get('decision_frequency', 5)  # every 5 days

        # ── Meta-strategy params ──
        self.meta_strategy_enabled = kwargs.get('meta_strategy_enabled', True)
        self.max_strategies_per_decision = kwargs.get('max_strategies_per_decision', 5)

        # ── Learning params ──
        self.record_decisions = kwargs.get('record_decisions', True)
        self.record_strategy_combos = kwargs.get('record_strategy_combos', True)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


# ═══════════════════════════════════════════════════════════
#  Decision Record
# ═══════════════════════════════════════════════════════════

class IntelDecisionRecord:
    """Records a single decision point for post-hoc learning."""

    def __init__(self, date: str, regime: str, volatility: str):
        self.date = date
        self.regime = regime
        self.volatility = volatility
        self.selected_strategies: list[str] = []
        self.strategy_scores: dict[str, float] = {}
        self.intel_features: dict[str, Any] = {}
        self.quant_signals: dict[str, Any] = {}
        self.actions: list[dict[str, Any]] = []
        self.portfolio_value_before: float = 0
        self.portfolio_value_after: float = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'date': self.date,
            'regime': self.regime,
            'volatility': self.volatility,
            'selected_strategies': self.selected_strategies,
            'strategy_scores': self.strategy_scores,
            'intel_features': self.intel_features,
            'actions': self.actions,
            'portfolio_value_before': round(self.portfolio_value_before, 2),
            'portfolio_value_after': round(self.portfolio_value_after, 2),
            'return_pct': round(
                (self.portfolio_value_after - self.portfolio_value_before)
                / max(self.portfolio_value_before, 1) * 100, 3
            ) if self.portfolio_value_before > 0 else 0,
        }


# ═══════════════════════════════════════════════════════════
#  Intel-Aware Backtest Engine
# ═══════════════════════════════════════════════════════════

class IntelBacktestEngine:
    """Backtest engine that integrates time-locked intelligence with meta-strategy selection.

    At each decision point, it:
      1. Queries intel DB with time lock (only intel published before decision date)
      2. Extracts regime features from intel
      3. Computes quant signals from price data (no future leak)
      4. Detects market condition (regime + intel features)
      5. Selects optimal strategy combination via suitability matrix
      6. Applies selected strategies' trading logic
      7. Records the decision for learning
    """

    def __init__(self, db: Any, config: IntelBacktestConfig | None = None):
        self.db = db
        self.config = config or IntelBacktestConfig()
        self.decision_records: list[IntelDecisionRecord] = []

    def run(
        self,
        asset_prices: dict[str, list[dict[str, Any]]],
        benchmark_navs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute full intel-aware backtest.

        Args:
            asset_prices: {symbol: [{'date': 'YYYY-MM-DD', 'nav': float}, ...]} sorted ASC
            benchmark_navs: optional benchmark NAV series

        Returns:
            Comprehensive result dict with standard metrics + intel/strategy analysis.
        """
        cfg = self.config

        if not asset_prices:
            return {'error': 'No trading data provided'}

        # ── Build unified date index ──
        all_dates = sorted(set(
            n['date'] for navs in asset_prices.values() for n in navs
        ))

        min_required = cfg.min_signal_history + 20
        if len(all_dates) < min_required:
            return {'error': f'Insufficient data: {len(all_dates)} days (need {min_required}+)'}

        # ── Build lookup tables ──
        nav_lookup = {}
        for code, navs in asset_prices.items():
            for n in navs:
                nav_lookup[(code, n['date'])] = n['nav']

        bm_lookup = {}
        if benchmark_navs:
            for n in benchmark_navs:
                bm_lookup[n['date']] = n['nav']

        # ── Get intel coverage dates (for knowing when intel is available) ──
        intel_dates = set()
        if cfg.intel_enabled:
            try:
                intel_dates = set(get_intel_snapshot_dates(
                    self.db, start_date=all_dates[0], end_date=all_dates[-1]
                ))
            except Exception as e:
                logger.warning('[IntelBacktest] Failed to get intel dates: %s', e)

        # ── Initialize state ──
        state = BacktestState(
            initial_capital=cfg.initial_capital,
            all_dates=all_dates,
            symbols=list(asset_prices.keys()),
        )

        stop_mgr = StopLossManager() if cfg.enable_stop_loss else None
        dd_protector = DrawdownProtector(cfg.initial_capital) if cfg.enable_drawdown_protection else None

        # ── Main simulation loop ──
        decision_counter = 0
        last_selected_strategies: list[str] = []
        last_market_condition: MarketCondition | None = None

        for day_idx, current_date in enumerate(all_dates):
            # ── Update portfolio value ──
            portfolio_value = state.cash
            for code, pos in state.positions.items():
                nav = nav_lookup.get((code, current_date), pos.get('last_nav', 0))
                pos['last_nav'] = nav
                pos['current_value'] = pos['shares'] * nav
                portfolio_value += pos['current_value']

            state.record_daily(current_date, portfolio_value)

            # ── Execute pending orders (T+1) ──
            self._execute_pending_orders(state, nav_lookup, current_date, stop_mgr)

            # ── Check stop-loss ──
            if stop_mgr:
                for code in list(state.positions.keys()):
                    nav = nav_lookup.get((code, current_date))
                    if nav is None:
                        continue
                    action = stop_mgr.update(code, nav, current_date)
                    if action:
                        if action['action'] == 'stop_loss':
                            self._place_sell_order(state, code, 1.0, current_date,
                                                   f"Stop-loss: {action['reason']}")
                        elif action['action'] == 'take_profit':
                            self._place_sell_order(state, code,
                                                   action.get('sell_pct', 0.5), current_date,
                                                   f"Take-profit: {action['reason']}")

            # ── Drawdown protection ──
            if dd_protector:
                dd_status = dd_protector.update(portfolio_value)
                state.drawdown_levels.append({'date': current_date, **dd_status})
                if dd_status.get('force_sell') and dd_status['force_sell_pct'] > 0:
                    for code in list(state.positions.keys()):
                        self._place_sell_order(state, code, dd_status['force_sell_pct'],
                                               current_date,
                                               f"Circuit breaker: {dd_status['level']}")

            # ── Skip until warmup complete ──
            if day_idx < cfg.min_signal_history:
                continue

            # ── Decision frequency gate ──
            decision_counter += 1
            if decision_counter % cfg.decision_frequency != 0:
                continue

            # ════════════════════════════════════════════════
            #  CORE: Intel-Aware Decision Making
            # ════════════════════════════════════════════════

            # Step 1: Compute quant signals (STRICT: only data up to today)
            signals = {}
            for code, navs in asset_prices.items():
                hist_slice = [n for n in navs if n['date'] <= current_date]
                if len(hist_slice) >= cfg.min_signal_history:
                    try:
                        snap = compute_signal_snapshot(hist_slice)
                        if 'error' not in snap:
                            signals[code] = snap
                    except Exception as e:
                        logger.debug('Signal failed for %s on %s: %s', code, current_date, e)

            if not signals:
                continue

            # Step 2: TIME-LOCKED intel features (the key innovation)
            intel_features = {}
            market_condition = None
            selected_strategy_types: list[str] = []
            adaptive_decision_detail = None

            if cfg.intel_enabled and cfg.meta_strategy_enabled:
                try:
                    intel_features = build_regime_intel_features(
                        self.db, current_date,
                        lookback_days=cfg.intel_lookback_days,
                    )
                except Exception as e:
                    logger.debug('[IntelBacktest] Intel features failed for %s: %s',
                                 current_date, e)

                # Step 3+4: Unified strategy selection via AdaptiveDecisionEngine
                # This uses the SAME logic as live decisions: suitability matrix
                # + learning data + compatibility checks + failure restrictions.
                # The old _select_strategy_types only used the raw matrix.
                try:
                    from lib.trading_autopilot.adaptive_decision_engine import (
                        AdaptiveDecisionEngine,
                    )
                    ade = AdaptiveDecisionEngine(self.db)
                    adaptive_decision_detail = ade.make_decision(
                        quant_signals=signals,
                        as_of=current_date,
                        max_strategies=cfg.max_strategies_per_decision,
                    )
                    # Extract the market condition and selected strategy types
                    mc_dict = adaptive_decision_detail.get('market_condition', {})
                    market_condition = MarketCondition(
                        regime=mc_dict.get('regime', 'unknown'),
                        volatility=mc_dict.get('volatility', 'normal'),
                        trend_strength=mc_dict.get('trend_strength', 0),
                        sentiment_score=mc_dict.get('sentiment_score', 0),
                        policy_signal=mc_dict.get('policy_signal', 0),
                        risk_signal=mc_dict.get('risk_signal', 0),
                        opportunity_signal=mc_dict.get('opportunity_signal', 0),
                        intel_velocity=mc_dict.get('intel_velocity', 0),
                        as_of=current_date,
                    )
                    last_market_condition = market_condition

                    # Collect strategy types from the unified selection
                    selected_strategy_types = list(dict.fromkeys(
                        s['profile']['type']
                        for s in adaptive_decision_detail.get('selected_strategies', [])
                    ))

                    # Incorporate the fused signal from the engine
                    fused = adaptive_decision_detail.get('fused_signal', {})
                    if fused.get('risk_veto'):
                        # Force risk_control to the front
                        if 'risk_control' in selected_strategy_types:
                            selected_strategy_types.remove('risk_control')
                        selected_strategy_types.insert(0, 'risk_control')

                    last_selected_strategies = selected_strategy_types

                except Exception as e:
                    logger.warning('[IntelBacktest] AdaptiveDecisionEngine failed, '
                                   'falling back to matrix selection: %s', e,
                                   exc_info=True)
                    # Fallback: detect condition manually + select via matrix
                    market_condition = self._detect_condition(
                        signals, intel_features, current_date,
                    )
                    last_market_condition = market_condition
                    selected_strategy_types = self._select_strategy_types(
                        market_condition, signals,
                    )
                    last_selected_strategies = selected_strategy_types

            else:
                # Fallback: use adaptive strategy (no intel)
                selected_strategy_types = ['adaptive']

            # Step 5: Create decision record
            record = None
            if cfg.record_decisions:
                record = IntelDecisionRecord(
                    date=current_date,
                    regime=market_condition.regime if market_condition else 'unknown',
                    volatility=market_condition.volatility if market_condition else 'normal',
                )
                record.selected_strategies = selected_strategy_types
                record.intel_features = intel_features
                record.portfolio_value_before = portfolio_value
                # Attach the full adaptive decision detail for richer learning
                if adaptive_decision_detail:
                    record.strategy_scores = {
                        s['profile']['name']: s['selection_score']
                        for s in adaptive_decision_detail.get('selected_strategies', [])
                    }

            # Step 6: Execute selected strategies' trading logic
            actions = self._execute_strategies(
                state, signals, nav_lookup, current_date,
                selected_strategy_types, market_condition,
                stop_mgr, dd_protector,
            )

            if record:
                record.actions = actions

            # Record portfolio value AFTER decisions
            post_value = state.cash
            for code, pos in state.positions.items():
                post_value += pos.get('current_value', 0)

            if record:
                record.portfolio_value_after = post_value
                self.decision_records.append(record)

        # ── Final metrics ──
        result = compute_metrics(state, bm_lookup, all_dates, cfg.initial_capital)
        result['config'] = cfg.to_dict()
        result['strategy'] = 'intel_adaptive'
        result['period'] = {
            'start': all_dates[0], 'end': all_dates[-1],
            'trading_days': len(all_dates),
        }

        # ── Intel-specific results ──
        result['intel_analysis'] = {
            'total_decisions': len(self.decision_records),
            'intel_enabled': cfg.intel_enabled,
            'intel_coverage_dates': len(intel_dates),
            'decision_records': [r.to_dict() for r in self.decision_records[-50:]],
            'regime_distribution': self._compute_regime_distribution(),
            'strategy_usage': self._compute_strategy_usage(),
        }

        return result

    # ═══════════════════════════════════════════════════════
    #  Market Condition Detection (backtest-safe)
    # ═══════════════════════════════════════════════════════

    def _detect_condition(
        self,
        signals: dict[str, Any],
        intel_features: dict[str, Any],
        as_of: str,
    ) -> MarketCondition:
        """Detect market condition from quant signals + intel features.

        Mirrors the live meta_strategy.detect_market_condition but works
        entirely from backtest-local data (no DB queries for quant).
        """
        regime_votes: dict[str, int] = {}
        vol_votes: dict[str, int] = {}
        composite_scores: list[float] = []

        for _code, snap in signals.items():
            r = snap.get('trend_regime', 'sideways')
            regime_votes[r] = regime_votes.get(r, 0) + 1
            v = snap.get('volatility_regime', 'normal_vol')
            vol_votes[v] = vol_votes.get(v, 0) + 1
            cs = snap.get('composite_score', 0)
            composite_scores.append(cs)

        regime = max(regime_votes, key=regime_votes.get) if regime_votes else 'unknown'

        _VOL_MAP = {
            'low_vol': 'low', 'normal_vol': 'normal',
            'high_vol': 'high', 'extreme_vol': 'extreme',
        }
        vol_raw = max(vol_votes, key=vol_votes.get) if vol_votes else 'normal_vol'
        volatility = _VOL_MAP.get(vol_raw, 'normal')

        trend_strength = 0.0
        if composite_scores:
            avg_score = sum(composite_scores) / len(composite_scores)
            trend_strength = max(-1, min(1, avg_score / 50.0))

        return MarketCondition(
            regime=regime,
            volatility=volatility,
            trend_strength=trend_strength,
            sentiment_score=intel_features.get('sentiment_score', 0.0),
            policy_signal=intel_features.get('policy_signal', 0.0),
            risk_signal=intel_features.get('risk_signal', 0.0),
            opportunity_signal=intel_features.get('opportunity_signal', 0.0),
            intel_velocity=intel_features.get('intel_velocity', 0.0),
            as_of=as_of,
        )

    # ═══════════════════════════════════════════════════════
    #  Strategy Type Selection (from suitability matrix)
    # ═══════════════════════════════════════════════════════

    def _select_strategy_types(
        self,
        condition: MarketCondition,
        signals: dict[str, Any],
    ) -> list[str]:
        """Select which strategy types to activate based on market conditions.

        Uses the same suitability matrix as the live meta-strategy,
        but operates on strategy TYPES (not individual DB strategy records).

        Returns ordered list of strategy type names, highest suitability first.
        """
        strategy_types = [
            'risk_control', 'buy_signal', 'sell_signal',
            'allocation', 'timing', 'observation',
        ]

        scored: list[tuple[float, str]] = []
        regime = condition.regime

        for stype in strategy_types:
            base = _SUITABILITY_MATRIX.get((stype, regime), 0.5)

            # Intel modifiers (same as live meta_strategy)
            modifier = 1.0
            if condition.risk_level in ('critical', 'high'):
                if stype == 'risk_control':
                    modifier *= 1.3
                elif stype == 'buy_signal':
                    modifier *= 0.7
            if condition.sentiment_score > 0.3 and stype == 'buy_signal':
                modifier *= 1.15
            elif condition.sentiment_score < -0.3 and stype == 'sell_signal':
                modifier *= 1.15
            if condition.policy_signal > 0.3 and stype == 'allocation':
                modifier *= 1.2

            scored.append((base * modifier, stype))

        scored.sort(reverse=True)

        # Return top types above threshold
        threshold = 0.4
        selected = [stype for score, stype in scored if score >= threshold]

        # Always include risk_control
        if 'risk_control' not in selected:
            selected.append('risk_control')

        return selected[:self.config.max_strategies_per_decision]

    # ═══════════════════════════════════════════════════════
    #  Strategy Execution (mapping types to trading actions)
    # ═══════════════════════════════════════════════════════

    def _execute_strategies(
        self,
        state: BacktestState,
        signals: dict[str, Any],
        nav_lookup: dict,
        current_date: str,
        strategy_types: list[str],
        condition: MarketCondition | None,
        stop_mgr: StopLossManager | None,
        dd_protector: DrawdownProtector | None,
    ) -> list[dict[str, Any]]:
        """Execute trading logic based on selected strategy types.

        Maps abstract strategy types to concrete trading actions:
          - risk_control → stop-loss, position sizing, drawdown checks
          - buy_signal   → buy opportunities based on quant signals
          - sell_signal   → sell triggers based on quant signals
          - timing        → mean-reversion / trend-following entries
          - allocation    → rebalancing across positions

        Returns list of action dicts for decision recording.
        """
        cfg = self.config
        actions = []
        portfolio_value = state.get_portfolio_value()

        for code, snap in signals.items():
            score = snap.get('composite_score', 0)
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            risk_params = get_regime_risk_params(regime, vol_regime)
            bb_pos = snap.get('bb_position')
            rsi_val = snap.get('rsi_14')
            ma_align = snap.get('ma_alignment', 'unknown')
            macd_bull = snap.get('macd_bullish', False)

            # ── Sell logic (from sell_signal + risk_control) ──
            if code in state.positions:
                pos = state.positions[code]
                pos_weight = (pos.get('current_value', 0) / portfolio_value
                              if portfolio_value > 0 else 0)

                should_sell = False
                sell_fraction = 0.0
                sell_reason = ''

                # Risk control: emergency exits
                if 'risk_control' in strategy_types:
                    if score <= cfg.strong_sell_threshold:
                        should_sell = True
                        sell_fraction = max(sell_fraction, 0.8)
                        sell_reason = f"Risk control: strong sell score={score:.1f}"
                    elif condition and condition.risk_level == 'critical':
                        should_sell = True
                        sell_fraction = max(sell_fraction, 0.5)
                        sell_reason = "Risk control: critical risk level"

                # Sell signal: quant-driven
                if 'sell_signal' in strategy_types:
                    if score <= cfg.sell_threshold:
                        should_sell = True
                        sell_fraction = max(sell_fraction, 0.4)
                        sell_reason = f"Sell signal: score={score:.1f}"
                    elif regime in ('downtrend', 'capitulation'):
                        if ma_align in ('bearish_aligned',) and not macd_bull:
                            should_sell = True
                            sell_fraction = max(sell_fraction, 0.6)
                            sell_reason = "Sell signal: bearish regime + MA aligned"

                # Timing: mean-reversion exit
                if 'timing' in strategy_types:
                    if bb_pos is not None and bb_pos > 85:
                        should_sell = True
                        sell_fraction = max(sell_fraction, 0.5)
                        sell_reason = f"Timing: BB upper band exit ({bb_pos:.0f})"

                if should_sell and sell_fraction > 0:
                    self._place_sell_order(state, code, sell_fraction,
                                           current_date, sell_reason)
                    actions.append({
                        'type': 'sell', 'code': code,
                        'fraction': sell_fraction,
                        'reason': sell_reason,
                    })

                # ── Add to position ──
                elif ('buy_signal' in strategy_types
                      and score >= cfg.strong_buy_threshold
                      and pos_weight < 0.30):
                    max_add = portfolio_value * 0.08 * risk_params.get('buy_scale', 1.0)
                    max_add = min(max_add, state.cash * 0.4)
                    if max_add > 100:
                        nav = nav_lookup.get((code, current_date))
                        if nav and nav > 0:
                            self._place_buy_order(
                                state, code, max_add, current_date,
                                f"Add: score={score:.1f}, weight={pos_weight:.1%}",
                                stop_mgr, nav, risk_params,
                            )
                            actions.append({
                                'type': 'add', 'code': code,
                                'amount': max_add, 'reason': 'Strong signal add',
                            })

            else:
                # ── Buy logic (new positions) ──
                should_buy = False
                buy_amount = 0.0
                buy_reason = ''

                # Buy signal: conviction-scaled
                if 'buy_signal' in strategy_types:
                    if score >= cfg.strong_buy_threshold:
                        max_pct = risk_params.get('new_position_max_pct', 0.15)
                        conviction = min(
                            (score - cfg.strong_buy_threshold) / 30.0 + 0.6, 1.0
                        )
                        should_buy = True
                        buy_amount = portfolio_value * max_pct * conviction
                        buy_amount *= risk_params.get('buy_scale', 1.0)
                        buy_reason = f"Strong buy: score={score:.1f}, conviction={conviction:.0%}"
                    elif score >= cfg.buy_threshold:
                        max_pct = risk_params.get('new_position_max_pct', 0.15) * 0.5
                        should_buy = True
                        buy_amount = portfolio_value * max_pct
                        buy_amount *= risk_params.get('buy_scale', 1.0)
                        buy_reason = f"Buy signal: score={score:.1f}"

                # Timing: mean-reversion entry
                if ('timing' in strategy_types and not should_buy
                        and bb_pos is not None and rsi_val is not None):
                    if bb_pos < 15 and rsi_val < 35:
                        if regime not in ('capitulation',):
                            should_buy = True
                            buy_amount = portfolio_value * 0.10
                            buy_reason = f"Timing: MR entry BB={bb_pos:.0f}, RSI={rsi_val:.0f}"

                # Timing: trend entry
                if ('timing' in strategy_types and not should_buy
                        and ma_align == 'bullish_aligned' and macd_bull
                        and score > 5):
                    should_buy = True
                    buy_amount = portfolio_value * risk_params.get('new_position_max_pct', 0.15)
                    buy_reason = "Timing: trend entry MA bullish, MACD bullish"

                # ── Intel-based modifiers ──
                if should_buy and condition:
                    # Reduce buy in high-risk environments
                    if condition.risk_level in ('critical', 'high'):
                        buy_amount *= 0.5
                        buy_reason += " (risk-adjusted ×0.5)"
                    # Boost in high-opportunity environments
                    elif condition.opportunity_signal > 0.5:
                        buy_amount *= 1.2
                        buy_reason += " (opportunity-boosted ×1.2)"

                # Position size limits
                if should_buy:
                    buy_amount = min(buy_amount, state.cash * 0.80)
                    # Max positions check
                    existing = set(state.positions.keys()) | set(
                        o.get('code') for o in state.pending_orders
                        if o.get('type') == 'buy'
                    )
                    if code not in existing and len(existing) >= cfg.max_positions:
                        should_buy = False

                if should_buy and buy_amount > 100:
                    nav = nav_lookup.get((code, current_date))
                    if nav and nav > 0:
                        self._place_buy_order(
                            state, code, buy_amount, current_date,
                            buy_reason, stop_mgr, nav, risk_params,
                        )
                        actions.append({
                            'type': 'buy', 'code': code,
                            'amount': buy_amount, 'reason': buy_reason,
                        })

        return actions

    # ═══════════════════════════════════════════════════════
    #  Order Management (mirrors BacktestEngine)
    # ═══════════════════════════════════════════════════════

    def _place_buy_order(self, state, code, amount, date, reason, stop_mgr, nav, risk_params):
        cfg = self.config
        amount = min(amount, state.cash * 0.95)
        if amount < 100:
            return

        existing = set(state.positions.keys()) | set(
            o.get('code') for o in state.pending_orders if o.get('type') == 'buy'
        )
        if code not in existing and len(existing) >= cfg.max_positions:
            return

        fee = amount * cfg.buy_fee_rate
        state.cash -= amount
        state.pending_orders.append({
            'type': 'buy', 'code': code, 'amount': amount - fee, 'fee': fee,
            'placed_date': date, 'reason': reason, 'risk_params': risk_params,
        })

    def _place_sell_order(self, state, code, fraction, date, reason):
        if code not in state.positions:
            return
        pos = state.positions[code]
        shares_to_sell = pos['shares'] * min(fraction, 1.0)
        if shares_to_sell <= 0:
            return
        state.pending_orders.append({
            'type': 'sell', 'code': code, 'shares': shares_to_sell,
            'placed_date': date, 'reason': reason,
        })

    def _execute_pending_orders(self, state, nav_lookup, date, stop_mgr):
        cfg = self.config
        executed = []
        remaining = []

        for order in state.pending_orders:
            placed = order.get('placed_date', '')
            if placed >= date:
                remaining.append(order)
                continue

            code = order['code']
            nav = nav_lookup.get((code, date))
            if nav is None or nav <= 0:
                remaining.append(order)
                continue

            if order['type'] == 'buy':
                net_amount = order['amount']
                shares = net_amount / nav
                fee = order['fee']

                if code in state.positions:
                    pos = state.positions[code]
                    pos['shares'] += shares
                    pos['cost'] += net_amount
                    pos['avg_cost_nav'] = pos['cost'] / pos['shares'] if pos['shares'] > 0 else nav
                    pos['last_nav'] = nav
                    pos['current_value'] = pos['shares'] * nav
                else:
                    state.positions[code] = {
                        'shares': shares, 'cost': net_amount,
                        'avg_cost_nav': nav, 'entry_date': date,
                        'last_nav': nav, 'current_value': net_amount,
                    }
                    if stop_mgr:
                        stop_mgr.register(code, nav, order.get('risk_params', {}))

                state.total_fees += fee
                state.trade_log.append({
                    'date': date, 'code': code, 'type': 'buy',
                    'nav': nav, 'shares': shares, 'amount': net_amount + fee,
                    'fee': fee, 'reason': order['reason'],
                })
                executed.append(order)

            elif order['type'] == 'sell':
                if code not in state.positions:
                    continue
                pos = state.positions[code]
                shares_to_sell = min(order['shares'], pos['shares'])
                if shares_to_sell <= 0:
                    continue

                gross_amount = shares_to_sell * nav
                entry_date = pos.get('entry_date', '')
                hold_days = 30
                if entry_date:
                    try:
                        hold_days = (datetime.strptime(date, '%Y-%m-%d')
                                     - datetime.strptime(entry_date, '%Y-%m-%d')).days
                    except (ValueError, TypeError) as _e:
                        logger.debug('[IntelBacktest] hold_days date parse failed: %s', _e)

                fee_rate = (cfg.short_sell_penalty
                            if hold_days < cfg.min_holding_days
                            else cfg.sell_fee_rate)
                fee = gross_amount * fee_rate
                net_proceeds = gross_amount - fee

                pos['shares'] -= shares_to_sell
                cost_basis = pos['avg_cost_nav'] * shares_to_sell
                pos['cost'] = max(pos['cost'] - cost_basis, 0)
                state.cash += net_proceeds
                state.total_fees += fee

                state.trade_log.append({
                    'date': date, 'code': code, 'type': 'sell',
                    'nav': nav, 'shares': shares_to_sell, 'amount': gross_amount,
                    'fee': fee, 'net_proceeds': net_proceeds,
                    'reason': order['reason'], 'hold_days': hold_days,
                })

                if pos['shares'] < 0.001:
                    del state.positions[code]
                    if stop_mgr:
                        stop_mgr.remove(code)
                else:
                    pos['current_value'] = pos['shares'] * nav

                executed.append(order)

        state.pending_orders = remaining

    # ═══════════════════════════════════════════════════════
    #  Analysis Helpers
    # ═══════════════════════════════════════════════════════

    def _compute_regime_distribution(self) -> dict[str, int]:
        """Count how many decisions were made in each market regime."""
        dist: dict[str, int] = {}
        for r in self.decision_records:
            dist[r.regime] = dist.get(r.regime, 0) + 1
        return dist

    def _compute_strategy_usage(self) -> dict[str, int]:
        """Count how many times each strategy type was selected."""
        usage: dict[str, int] = {}
        for r in self.decision_records:
            for s in r.selected_strategies:
                usage[s] = usage.get(s, 0) + 1
        return usage


# ═══════════════════════════════════════════════════════════
#  High-Level Entry Point
# ═══════════════════════════════════════════════════════════

def run_intel_backtest(
    db: Any,
    asset_prices: dict[str, list[dict[str, Any]]],
    benchmark_navs: list[dict[str, Any]] | None = None,
    config: IntelBacktestConfig | None = None,
) -> dict[str, Any]:
    """High-level entry point for intel-aware backtesting.

    Creates the engine, runs the backtest, and returns results
    with full intel/strategy analysis.
    """
    engine = IntelBacktestEngine(db, config)
    return engine.run(asset_prices, benchmark_navs)
