"""lib/trading_backtest_engine/strategies.py — Strategy Implementations

Each strategy is a method on the StrategyMixin class, which BacktestEngine
inherits.  Strategies share a common interface:

    _strategy_*(self, state, signals|nav_lookup, date, ...)

Strategies:
  - buy_and_hold  (init only — no daily decisions)
  - dca           (dollar-cost averaging)
  - signal_driven (conviction-scaled position management)
  - dca_signal    (DCA + signal overlay)
  - mean_reversion (BB + RSI contrarian)
  - trend_following (MA alignment + MACD)
  - adaptive      (regime-aware blend of above)
"""

from lib.trading_risk import get_regime_risk_params

__all__ = [
    "StrategyMixin",
]


class StrategyMixin:
    """Mixin providing all strategy implementations.

    Expects the host class to expose:
        self.buy_threshold, self.strong_buy_threshold
        self.sell_threshold, self.strong_sell_threshold
        self.initial_capital, self.buy_fee_rate
        self.max_positions, self.dca_amount, self.dca_signal_boost
        self._place_buy_order(state, code, amount, date, reason, stop_mgr, nav, risk_params)
        self._place_sell_order(state, code, fraction, date, reason)
    """

    # ── Buy & Hold ────────────────────────────────────────

    def _init_buy_and_hold(self, state, nav_lookup, all_dates):
        """Buy equal-weight on the FIRST available date — no warmup penalty."""
        first_date = all_dates[0]
        all_codes = sorted(set(k[0] for k in nav_lookup))
        if not all_codes:
            return
        # Invest 99% of capital equally across all assets on day 1
        amount_per = state.cash / len(all_codes) * 0.99
        for code in all_codes:
            nav = nav_lookup.get((code, first_date))
            if nav and nav > 0 and amount_per > 10:
                fee = amount_per * self.buy_fee_rate
                net_amount = amount_per - fee
                shares = net_amount / nav
                state.positions[code] = {
                    'shares': shares,
                    'cost': net_amount,
                    'avg_cost_nav': nav,
                    'entry_date': first_date,
                    'last_nav': nav,
                    'current_value': net_amount,
                }
                state.cash -= amount_per
                state.total_fees += fee
                state.trade_log.append({
                    'date': first_date, 'code': code, 'type': 'buy',
                    'nav': nav, 'shares': shares, 'amount': amount_per, 'fee': fee,
                    'reason': 'Buy & Hold: initial equal-weight allocation'
                })

    # ── Dollar-Cost Averaging ─────────────────────────────

    def _strategy_dca(self, state, nav_lookup, date, counter):
        """Dollar-cost averaging: invest fixed amount every ~20 trading days."""
        if counter % 20 != 0:
            return
        all_codes = sorted(set(k[0] for k in nav_lookup))
        if not all_codes:
            return
        dca_amount = self.initial_capital / 25.0
        amount_each = min(dca_amount / len(all_codes), state.cash / max(len(all_codes), 1) * 0.95)
        for code in all_codes:
            nav = nav_lookup.get((code, date))
            if nav and nav > 0 and amount_each > 10 and state.cash > amount_each:
                fee = amount_each * self.buy_fee_rate
                net_amount = amount_each - fee
                shares = net_amount / nav
                if code in state.positions:
                    pos = state.positions[code]
                    pos['shares'] += shares
                    pos['cost'] += net_amount
                    pos['avg_cost_nav'] = pos['cost'] / pos['shares'] if pos['shares'] > 0 else nav
                    pos['last_nav'] = nav
                    pos['current_value'] = pos['shares'] * nav
                else:
                    state.positions[code] = {
                        'shares': shares,
                        'cost': net_amount,
                        'avg_cost_nav': nav,
                        'entry_date': date,
                        'last_nav': nav,
                        'current_value': net_amount,
                    }
                state.cash -= amount_each
                state.total_fees += fee
                state.trade_log.append({
                    'date': date, 'code': code, 'type': 'buy',
                    'nav': nav, 'shares': shares, 'amount': amount_each, 'fee': fee,
                    'reason': f'DCA: periodic investment #{counter // 20}'
                })

    # ── Signal-Driven ─────────────────────────────────────

    def _strategy_signal_driven(self, state, signals, nav_lookup, date, dd_protector, stop_mgr):
        """v2 Signal-driven strategy with gradual position building.

        Key changes from v1:
        - Lower thresholds (8/20 vs 20/45) to actually trigger trades
        - Position size scales linearly with signal conviction
        - Existing positions get topped up on strong signals
        - Partial sells rather than all-or-nothing
        """
        portfolio_value = state.get_portfolio_value()

        for code, snap in signals.items():
            score = snap.get('composite_score', 0)
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            risk_params = get_regime_risk_params(regime, vol_regime)

            if code in state.positions:
                pos = state.positions[code]
                pos_weight = pos.get('current_value', 0) / portfolio_value if portfolio_value > 0 else 0

                # ── Sell logic (gradual) ──
                if score <= self.strong_sell_threshold:
                    # Strong sell: liquidate 80%
                    self._place_sell_order(state, code, 0.8, date,
                                           f"Strong sell signal: score={score:.1f}")
                elif score <= self.sell_threshold:
                    # Moderate sell: trim 40%
                    self._place_sell_order(state, code, 0.4, date,
                                           f"Sell signal: score={score:.1f}")
                elif score <= 0 and pos_weight > 0.25:
                    # Negative score + overweight: trim to target
                    self._place_sell_order(state, code, 0.2, date,
                                           f"Trim overweight: score={score:.1f}, weight={pos_weight:.1%}")

                # ── Add to existing position on very strong signal ──
                elif score >= self.strong_buy_threshold and pos_weight < 0.30:
                    max_add = portfolio_value * 0.08 * risk_params.get('buy_scale', 1.0)
                    max_add = min(max_add, state.cash * 0.4)
                    if max_add > 100:
                        nav = nav_lookup.get((code, date))
                        if nav and nav > 0:
                            self._place_buy_order(state, code, max_add, date,
                                                  f"Add to position: score={score:.1f}",
                                                  stop_mgr, nav, risk_params)
            else:
                # ── Buy logic (scaled by conviction) ──
                if score >= self.strong_buy_threshold and state.cash > portfolio_value * 0.05:
                    # Strong buy: larger position
                    max_pct = risk_params.get('new_position_max_pct', 0.15)
                    # Scale position size by how far above threshold
                    conviction = min((score - self.strong_buy_threshold) / 30.0 + 0.6, 1.0)
                    amount = portfolio_value * max_pct * conviction * risk_params.get('buy_scale', 1.0)
                    amount = min(amount, state.cash * 0.80)
                    if amount > 100:
                        nav = nav_lookup.get((code, date))
                        if nav and nav > 0:
                            self._place_buy_order(state, code, amount, date,
                                                  f"Strong buy: score={score:.1f}, conviction={conviction:.0%}",
                                                  stop_mgr, nav, risk_params)
                elif score >= self.buy_threshold and state.cash > portfolio_value * 0.08:
                    # Moderate buy: smaller starter position
                    max_pct = risk_params.get('new_position_max_pct', 0.15) * 0.5
                    amount = portfolio_value * max_pct * risk_params.get('buy_scale', 1.0)
                    amount = min(amount, state.cash * 0.50)
                    if amount > 100:
                        nav = nav_lookup.get((code, date))
                        if nav and nav > 0:
                            self._place_buy_order(state, code, amount, date,
                                                  f"Buy: score={score:.1f}",
                                                  stop_mgr, nav, risk_params)

    # ── DCA + Signal ──────────────────────────────────────

    def _strategy_dca_signal(self, state, signals, nav_lookup, date, dd_protector, stop_mgr, counter):
        """DCA base + signal-enhanced: regular investment boosted/reduced by signals."""
        portfolio_value = state.get_portfolio_value()
        is_dca_day = counter % 20 == 0

        for code, snap in signals.items():
            score = snap.get('composite_score', 0)
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            risk_params = get_regime_risk_params(regime, vol_regime)

            # Check sells regardless of DCA timing
            if code in state.positions:
                if score <= self.strong_sell_threshold:
                    self._place_sell_order(state, code, 0.5, date,
                                           f"Strong sell in DCA mode: score={score:.1f}")
                elif score <= self.sell_threshold:
                    self._place_sell_order(state, code, 0.25, date,
                                           f"Sell in DCA mode: score={score:.1f}")

            # DCA buys on schedule
            if is_dca_day and state.cash > self.dca_amount * 0.5:
                if score >= self.strong_buy_threshold:
                    amount = self.dca_amount * self.dca_signal_boost * 1.5
                elif score >= self.buy_threshold:
                    amount = self.dca_amount * self.dca_signal_boost
                elif score >= 0:
                    amount = self.dca_amount
                elif score >= self.sell_threshold:
                    amount = self.dca_amount * 0.5
                else:
                    amount = 0  # Skip DCA when signal is sell

                if amount > 100:
                    n_assets = max(len(signals), 1)
                    amount_each = min(amount / n_assets, state.cash * 0.5 / n_assets)
                    nav = nav_lookup.get((code, date))
                    if nav and nav > 0 and amount_each > 100:
                        self._place_buy_order(state, code, amount_each, date,
                                              f"DCA buy (signal={score:.1f})",
                                              stop_mgr, nav, risk_params)

    # ── Mean Reversion ────────────────────────────────────

    def _strategy_mean_reversion(self, state, signals, nav_lookup, date, dd_protector, stop_mgr):
        """Mean reversion: buy when oversold/at BB lower, sell when overbought/at BB upper."""
        portfolio_value = state.get_portfolio_value()

        for code, snap in signals.items():
            bb_pos = snap.get('bb_position')
            rsi_val = snap.get('rsi_14')
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            risk_params = get_regime_risk_params(regime, vol_regime)

            if bb_pos is None or rsi_val is None:
                continue

            if code in state.positions:
                if bb_pos > 85 and rsi_val > 65:
                    self._place_sell_order(state, code, 0.6, date,
                                           f"Mean reversion sell: BB={bb_pos:.0f}, RSI={rsi_val:.0f}")
                elif bb_pos > 95:
                    self._place_sell_order(state, code, 0.8, date,
                                           f"BB upper breakout sell: BB={bb_pos:.0f}")
            else:
                if bb_pos < 15 and rsi_val < 35:
                    if regime not in ('strong_bear',):
                        amount = portfolio_value * risk_params.get('new_position_max_pct', 0.15)
                        amount = min(amount, state.cash * 0.8)
                        if amount > 100:
                            nav = nav_lookup.get((code, date))
                            if nav and nav > 0:
                                self._place_buy_order(state, code, amount, date,
                                                      f"Mean reversion buy: BB={bb_pos:.0f}, RSI={rsi_val:.0f}",
                                                      stop_mgr, nav, risk_params)
                elif bb_pos < 5:
                    amount = portfolio_value * 0.05
                    amount = min(amount, state.cash * 0.5)
                    if amount > 100:
                        nav = nav_lookup.get((code, date))
                        if nav and nav > 0:
                            self._place_buy_order(state, code, amount, date,
                                                  f"Extreme mean reversion: BB={bb_pos:.0f}",
                                                  stop_mgr, nav, risk_params)

    # ── Trend Following ───────────────────────────────────

    def _strategy_trend_following(self, state, signals, nav_lookup, date, dd_protector, stop_mgr):
        """Trend following: buy in uptrend, sell in downtrend. Uses MA alignment + MACD."""
        portfolio_value = state.get_portfolio_value()

        for code, snap in signals.items():
            ma_align = snap.get('ma_alignment', 'unknown')
            macd_bull = snap.get('macd_bullish', False)
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            risk_params = get_regime_risk_params(regime, vol_regime)
            score = snap.get('composite_score', 0)

            if code in state.positions:
                if ma_align in ('bearish_aligned',) and not macd_bull:
                    self._place_sell_order(state, code, 1.0, date,
                                           "Trend reversal: MA bearish aligned")
                elif ma_align in ('bearish_mixed',) and not macd_bull:
                    self._place_sell_order(state, code, 0.5, date,
                                           "Weakening trend: MA bearish mixed")

                recent_ma = snap.get('recent_ma_crossovers', [])
                for cross in recent_ma:
                    if cross.get('type') == 'death' and cross.get('days_ago', 99) <= 3:
                        self._place_sell_order(state, code, 0.7, date,
                                               f"Recent death cross ({cross['days_ago']}d ago)")
            else:
                if ma_align == 'bullish_aligned' and macd_bull and score > 5:
                    amount = portfolio_value * risk_params.get('new_position_max_pct', 0.15)
                    amount = min(amount, state.cash * 0.8)
                    if amount > 100:
                        nav = nav_lookup.get((code, date))
                        if nav and nav > 0:
                            self._place_buy_order(state, code, amount, date,
                                                  f"Trend entry: MA bullish, MACD bullish, score={score:.1f}",
                                                  stop_mgr, nav, risk_params)

                recent_ma = snap.get('recent_ma_crossovers', [])
                for cross in recent_ma:
                    if cross.get('type') == 'golden' and cross.get('days_ago', 99) <= 3:
                        if code not in state.positions and code not in [o.get('code') for o in state.pending_orders]:
                            amount = portfolio_value * 0.10
                            amount = min(amount, state.cash * 0.6)
                            if amount > 100:
                                nav = nav_lookup.get((code, date))
                                if nav and nav > 0:
                                    self._place_buy_order(state, code, amount, date,
                                                          f"Golden cross entry ({cross['days_ago']}d ago)",
                                                          stop_mgr, nav, risk_params)

    # ── Adaptive ──────────────────────────────────────────

    def _strategy_adaptive(self, state, signals, nav_lookup, date, dd_protector, stop_mgr, counter):
        """v2 Adaptive strategy: switches between trend-following and mean-reversion
        based on detected market regime.

        - Bull/Strong Bull → trend-following (ride momentum)
        - Bear/Strong Bear → defensive (tighter stops, smaller positions, mean-reversion only at extremes)
        - Sideways → mean-reversion (buy dips, sell rallies)
        """
        portfolio_value = state.get_portfolio_value()

        for code, snap in signals.items():
            score = snap.get('composite_score', 0)
            regime = snap.get('trend_regime', 'sideways')
            vol_regime = snap.get('volatility_regime', 'normal_vol')
            bb_pos = snap.get('bb_position')
            rsi_val = snap.get('rsi_14')
            ma_align = snap.get('ma_alignment', 'unknown')
            macd_bull = snap.get('macd_bullish', False)
            risk_params = get_regime_risk_params(regime, vol_regime)

            pos_exists = code in state.positions

            # ── Adaptive sell logic (applies in all regimes) ──
            if pos_exists:
                pos = state.positions[code]
                pos_weight = pos.get('current_value', 0) / portfolio_value if portfolio_value > 0 else 0

                # Hard stop: strong sell signal in any regime
                if score <= self.strong_sell_threshold:
                    self._place_sell_order(state, code, 0.8, date,
                                           f"Adaptive strong sell: score={score:.1f}")
                    continue

                if regime in ('strong_bear', 'bear'):
                    # Bear: tight sells — get out on weak signals
                    if score <= 0 or (ma_align in ('bearish_aligned', 'bearish_mixed') and not macd_bull):
                        sell_pct = 0.6 if regime == 'strong_bear' else 0.4
                        self._place_sell_order(state, code, sell_pct, date,
                                               f"Adaptive bear exit: regime={regime}, score={score:.1f}")
                elif regime == 'sideways':
                    # Sideways: sell at upper BB (mean reversion exit)
                    if bb_pos is not None and bb_pos > 85:
                        self._place_sell_order(state, code, 0.5, date,
                                               f"Adaptive MR sell: BB={bb_pos:.0f}")
                    elif score <= self.sell_threshold:
                        self._place_sell_order(state, code, 0.3, date,
                                               f"Adaptive sideways sell: score={score:.1f}")
                else:
                    # Bull: only sell on confirmed reversal
                    if ma_align == 'bearish_aligned' and not macd_bull and score <= self.sell_threshold:
                        self._place_sell_order(state, code, 0.5, date,
                                               f"Adaptive bull reversal sell: score={score:.1f}")

            # ── Adaptive buy logic ──
            else:
                if regime in ('strong_bull', 'bull'):
                    # Bull: trend-following entry
                    if score >= self.buy_threshold and ma_align in ('bullish_aligned', 'bullish_mixed'):
                        conviction = min((score - self.buy_threshold) / 25.0 + 0.4, 1.0)
                        amount = portfolio_value * risk_params.get('new_position_max_pct', 0.15) * conviction
                        amount = min(amount, state.cash * 0.70)
                        if amount > 100:
                            nav = nav_lookup.get((code, date))
                            if nav and nav > 0:
                                self._place_buy_order(state, code, amount, date,
                                                      f"Adaptive trend buy: regime={regime}, score={score:.1f}",
                                                      stop_mgr, nav, risk_params)

                elif regime == 'sideways':
                    # Sideways: mean-reversion entry at lower BB
                    if bb_pos is not None and rsi_val is not None and bb_pos < 20 and rsi_val < 40:
                        amount = portfolio_value * 0.10
                        amount = min(amount, state.cash * 0.50)
                        if amount > 100:
                            nav = nav_lookup.get((code, date))
                            if nav and nav > 0:
                                self._place_buy_order(state, code, amount, date,
                                                      f"Adaptive MR buy: BB={bb_pos:.0f}, RSI={rsi_val:.0f}",
                                                      stop_mgr, nav, risk_params)

                elif regime in ('strong_bear', 'bear'):
                    # Bear: only buy at extreme oversold (very small position)
                    if bb_pos is not None and rsi_val is not None and bb_pos < 5 and rsi_val < 25:
                        amount = portfolio_value * 0.04  # Tiny position
                        amount = min(amount, state.cash * 0.20)
                        if amount > 100:
                            nav = nav_lookup.get((code, date))
                            if nav and nav > 0:
                                self._place_buy_order(state, code, amount, date,
                                                      f"Adaptive bear bottom-fish: BB={bb_pos:.0f}, RSI={rsi_val:.0f}",
                                                      stop_mgr, nav, risk_params)
