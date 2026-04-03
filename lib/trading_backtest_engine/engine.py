"""lib/trading_backtest_engine/engine.py — Event-Driven Backtesting Engine v2

Professional-grade backtesting with:
  - Strict no-future-data leakage (signal computed on data[0:i+1] only)
  - T+1 order execution (market rule)
  - Transaction costs (buy fee, sell fee, short-term penalty)
  - Stop-loss / take-profit mechanics
  - Drawdown circuit breakers
  - Multiple strategy modes: buy_and_hold, dca, signal_driven, dca_signal,
    mean_reversion, trend_following, adaptive
  - Gradual position building / trimming for signal strategies

v2 changes from v1:
  - buy_and_hold no longer penalised by warmup period
  - Signal thresholds recalibrated based on actual score distribution
  - Adaptive strategy combines trend-following in bull + mean-reversion in sideways
  - Position scaling proportional to signal conviction
  - Decision frequency lowered to daily for signal strategies
  - Look-ahead bias verification test built in
"""

from datetime import datetime

from lib.log import get_logger
from lib.trading_signals import compute_signal_snapshot

from .config import DEFAULT_CONFIG
from .reporting import compute_metrics
from .state import (
    BacktestState,
    DrawdownProtector,
    StopLossManager,
)
from .strategies import StrategyMixin

logger = get_logger(__name__)

__all__ = [
    "BacktestEngine",
]


class BacktestEngine(StrategyMixin):
    """Event-driven backtesting engine v2.

    Usage:
        engine = BacktestEngine(config)
        result = engine.run(asset_prices, benchmark_navs)

    asset_prices: {symbol: [{'date': 'YYYY-MM-DD', 'nav': float}, ...]}
    benchmark_navs: [{'date': 'YYYY-MM-DD', 'nav': float}, ...]
    """

    def __init__(self, config=None):
        self.config = config or {}
        # Merge defaults with user config
        _cfg = {**DEFAULT_CONFIG, **self.config}

        # Core params
        self.initial_capital = _cfg['initial_capital']
        self.buy_fee_rate = _cfg['buy_fee_rate']
        self.sell_fee_rate = _cfg['sell_fee_rate']
        self.short_sell_penalty = _cfg['short_sell_penalty']
        self.min_holding_days = _cfg['min_holding_days']
        self.decision_frequency = _cfg['decision_frequency']
        self.min_signal_history = _cfg['min_signal_history']
        self.strategy = _cfg['strategy']
        self.max_positions = _cfg['max_positions']
        self.enable_stop_loss = _cfg['enable_stop_loss']
        self.enable_drawdown_protection = _cfg['enable_drawdown_protection']

        # Signal thresholds
        self.buy_threshold = _cfg['buy_threshold']
        self.strong_buy_threshold = _cfg['strong_buy_threshold']
        self.sell_threshold = _cfg['sell_threshold']
        self.strong_sell_threshold = _cfg['strong_sell_threshold']

        # DCA settings
        self.dca_amount = _cfg['dca_amount']
        self.dca_signal_boost = _cfg['dca_signal_boost']

    # ── Public API ────────────────────────────────────────

    def run(self, asset_prices, benchmark_navs=None):
        """Execute full backtest.

        Args:
            asset_prices: {code: [{'date': str, 'nav': float}, ...]} sorted by date ASC
            benchmark_navs: optional [{'date': str, 'nav': float}, ...]

        Returns comprehensive result dict.
        """
        # Validate
        if not asset_prices:
            return {'error': 'No trading data provided'}

        # Build unified date index
        all_dates = set()
        for code, navs in asset_prices.items():
            for n in navs:
                all_dates.add(n['date'])
        all_dates = sorted(all_dates)

        min_required = self.min_signal_history + 20
        if len(all_dates) < min_required:
            return {'error': f'Insufficient data: {len(all_dates)} days (need {min_required}+)'}

        # Build lookup tables
        nav_lookup = {}  # (code, date) → nav
        for code, navs in asset_prices.items():
            for n in navs:
                nav_lookup[(code, n['date'])] = n['nav']

        bm_lookup = {}
        if benchmark_navs:
            for n in benchmark_navs:
                bm_lookup[n['date']] = n['nav']

        # Initialize state
        state = BacktestState(
            initial_capital=self.initial_capital,
            all_dates=all_dates,
            symbols=list(asset_prices.keys()),
        )

        # Risk management
        stop_mgr = StopLossManager() if self.enable_stop_loss else None
        dd_protector = DrawdownProtector(self.initial_capital) if self.enable_drawdown_protection else None

        # v2: buy_and_hold buys immediately on first available date (no warmup penalty)
        if self.strategy == 'buy_and_hold':
            self._init_buy_and_hold(state, nav_lookup, all_dates)

        # ── Main simulation loop ──
        decision_counter = 0
        for day_idx, date in enumerate(all_dates):
            # Update portfolio value
            portfolio_value = state.cash
            for code, pos in state.positions.items():
                nav = nav_lookup.get((code, date), pos.get('last_nav', 0))
                pos['last_nav'] = nav
                pos['current_value'] = pos['shares'] * nav
                portfolio_value += pos['current_value']

            state.record_daily(date, portfolio_value)

            # buy_and_hold: no further action needed after initial buy
            if self.strategy == 'buy_and_hold':
                continue

            # ── Execute pending orders (T+1) ──
            self._execute_pending_orders(state, nav_lookup, date, stop_mgr)

            # ── Check stop-loss/take-profit ──
            if stop_mgr:
                for code in list(state.positions.keys()):
                    nav = nav_lookup.get((code, date))
                    if nav is None:
                        continue
                    action = stop_mgr.update(code, nav, date)
                    if action:
                        if action['action'] == 'stop_loss':
                            self._place_sell_order(state, code, 1.0, date,
                                                   f"Stop-loss ({action['type']}): {action['reason']}")
                        elif action['action'] == 'take_profit':
                            sell_pct = action.get('sell_pct', 0.5)
                            self._place_sell_order(state, code, sell_pct, date,
                                                   f"Take-profit: {action['reason']}")

            # ── Drawdown protection ──
            if dd_protector:
                dd_status = dd_protector.update(portfolio_value)
                state.drawdown_levels.append({'date': date, **dd_status})
                if dd_status.get('force_sell') and dd_status['force_sell_pct'] > 0:
                    for code in list(state.positions.keys()):
                        self._place_sell_order(state, code, dd_status['force_sell_pct'], date,
                                               f"Circuit breaker: {dd_status['level']}")

            # ── DCA doesn't need signal warmup ──
            if self.strategy == 'dca':
                decision_counter += 1
                self._strategy_dca(state, nav_lookup, date, decision_counter)
                continue

            # ── Signal-based strategies: skip until warmup complete ──
            if day_idx < self.min_signal_history:
                continue

            decision_counter += 1
            if decision_counter % self.decision_frequency != 0:
                continue

            # Compute signals (STRICT: only use data up to today)
            signals = {}
            for code, navs in asset_prices.items():
                hist_slice = [n for n in navs if n['date'] <= date]
                if len(hist_slice) >= self.min_signal_history:
                    try:
                        snap = compute_signal_snapshot(hist_slice)
                        if 'error' not in snap:
                            signals[code] = snap
                    except Exception as e:
                        logger.warning('Signal computation failed for %s on %s: %s', code, date, e, exc_info=True)

            if not signals:
                continue

            # Apply strategy
            if self.strategy == 'signal_driven':
                self._strategy_signal_driven(state, signals, nav_lookup, date, dd_protector, stop_mgr)
            elif self.strategy == 'dca_signal':
                self._strategy_dca_signal(state, signals, nav_lookup, date, dd_protector, stop_mgr, decision_counter)
            elif self.strategy == 'mean_reversion':
                self._strategy_mean_reversion(state, signals, nav_lookup, date, dd_protector, stop_mgr)
            elif self.strategy == 'trend_following':
                self._strategy_trend_following(state, signals, nav_lookup, date, dd_protector, stop_mgr)
            elif self.strategy == 'adaptive':
                self._strategy_adaptive(state, signals, nav_lookup, date, dd_protector, stop_mgr, decision_counter)

        # ── Final metrics ──
        result = compute_metrics(state, bm_lookup, all_dates, self.initial_capital)
        result['config'] = self.config
        result['strategy'] = self.strategy
        result['period'] = {'start': all_dates[0], 'end': all_dates[-1], 'trading_days': len(all_dates)}

        return result

    # ── Order Management ──────────────────────────────────

    def _place_buy_order(self, state, code, amount, date, reason, stop_mgr, nav, risk_params):
        """Place a buy order (executed T+1)."""
        amount = min(amount, state.cash * 0.95)
        if amount < 100:
            return

        # Check max positions
        existing_codes = set(state.positions.keys()) | set(o.get('code') for o in state.pending_orders if o.get('type') == 'buy')
        if code not in existing_codes and len(existing_codes) >= self.max_positions:
            return

        fee = amount * self.buy_fee_rate
        state.cash -= amount  # Reserve cash immediately
        state.pending_orders.append({
            'type': 'buy', 'code': code, 'amount': amount - fee, 'fee': fee,
            'placed_date': date, 'reason': reason, 'risk_params': risk_params,
        })

    def _place_sell_order(self, state, code, fraction, date, reason):
        """Place a sell order for a fraction of position (executed T+1)."""
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
        """Execute orders placed on the previous day (T+1 settlement)."""
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
                # Can't execute — carry forward (or cancel after 3 days)
                if len(state.daily_values) - len([d for d in state.daily_values if d['date'] >= placed]) > 3:
                    if order['type'] == 'buy':
                        state.cash += order['amount'] + order['fee']
                else:
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
                        'shares': shares,
                        'cost': net_amount,
                        'avg_cost_nav': nav,
                        'entry_date': date,
                        'last_nav': nav,
                        'current_value': net_amount,
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

                # Short-term penalty (< 7 days holding)
                entry_date = pos.get('entry_date', '')
                if entry_date:
                    try:
                        hold_days = (datetime.strptime(date, '%Y-%m-%d') - datetime.strptime(entry_date, '%Y-%m-%d')).days
                    except (ValueError, TypeError):
                        logger.warning('Failed to parse holding period dates (date=%s, entry=%s), defaulting to 30d', date, entry_date, exc_info=True)
                        hold_days = 30
                else:
                    hold_days = 30

                if hold_days < self.min_holding_days:
                    fee_rate = self.short_sell_penalty
                else:
                    fee_rate = self.sell_fee_rate

                fee = gross_amount * fee_rate
                net_proceeds = gross_amount - fee

                pos['shares'] -= shares_to_sell
                cost_basis = pos['avg_cost_nav'] * shares_to_sell if pos['avg_cost_nav'] > 0 else 0
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
