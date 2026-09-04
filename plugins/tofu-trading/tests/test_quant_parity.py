"""PARITY COPY of chatui tests/test_trading_quant.py with imports rewritten
to tofu_trading.* — proves the relocated quant core is functionally identical.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _navs(values, start_day=1):
    """Build a nav series [{'date','nav'}] from a list of floats."""
    return [{'date': f'2024-01-{start_day + i:02d}', 'nav': float(v)}
            for i, v in enumerate(values)]


# ═══════════════════════════════════════════════════════════
#  Technical indicators — lib/trading_signals.py
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestIndicators:
    def test_sma_basic(self):
        from tofu_trading.trading_signals import sma
        navs = _navs([1, 2, 3, 4, 5])
        out = sma(navs, 3)
        assert out[:2] == [None, None]
        assert out[2] == pytest.approx(2.0)   # (1+2+3)/3
        assert out[3] == pytest.approx(3.0)
        assert out[4] == pytest.approx(4.0)

    def test_sma_too_short(self):
        from tofu_trading.trading_signals import sma
        assert sma(_navs([1, 2]), 5) == [None, None]

    def test_ema_seeds_with_sma(self):
        from tofu_trading.trading_signals import ema
        navs = _navs([1, 2, 3, 4, 5])
        out = ema(navs, 3)
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(2.0)   # seed = SMA(1,2,3)
        # k = 2/(3+1) = 0.5; ema[3] = 4*0.5 + 2*0.5 = 3.0
        assert out[3] == pytest.approx(3.0)
        # ema[4] = 5*0.5 + 3*0.5 = 4.0
        assert out[4] == pytest.approx(4.0)

    def test_rsi_all_gains_is_100(self):
        from tofu_trading.trading_signals import rsi
        navs = _navs([float(i) for i in range(1, 20)])  # strictly increasing
        out = rsi(navs, 14)
        # avg_loss == 0 → RSI 100
        assert out[14] == pytest.approx(100.0)

    def test_rsi_range_bounded(self):
        from tofu_trading.trading_signals import rsi
        navs = _navs([10, 11, 10.5, 12, 11, 13, 12.5, 14, 13, 15,
                      14.5, 16, 15, 17, 16.5, 18, 17, 19])
        out = rsi(navs, 14)
        vals = [v for v in out if v is not None]
        assert vals, 'RSI should produce at least one value'
        assert all(0 <= v <= 100 for v in vals)

    def test_daily_returns(self):
        from tofu_trading.trading_signals import daily_returns
        out = daily_returns(_navs([100, 110, 99]))
        assert out[0] == 0.0
        assert out[1] == pytest.approx(0.10)
        assert out[2] == pytest.approx(-0.10)

    def test_momentum(self):
        from tofu_trading.trading_signals import momentum
        navs = _navs([100, 101, 102, 103, 104, 110])  # idx5 vs idx0
        out = momentum(navs, 5)
        assert out[5] == pytest.approx(10.0)  # (110-100)/100*100

    def test_rolling_max_drawdown(self):
        from tofu_trading.trading_signals import rolling_max_drawdown
        # peak 100 → trough 80 = -20%
        navs = _navs([100, 90, 80, 85])
        out = rolling_max_drawdown(navs, 4)
        assert out[3] == pytest.approx(-20.0)

    def test_bollinger_bands_symmetry(self):
        from tofu_trading.trading_signals import bollinger_bands
        navs = _navs([10] * 5 + [10, 12, 8, 11, 9] * 4)
        up, mid, low, bw = bollinger_bands(navs, 20, 2.0)
        last = len(navs) - 1
        # band is symmetric around the mean
        assert up[last] - mid[last] == pytest.approx(mid[last] - low[last])

    def test_ma_crossover_golden_and_death(self):
        from tofu_trading.trading_signals import detect_ma_crossover
        fast = [1, 2, 3, 2, 1]
        slow = [2, 2, 2, 2, 2]
        sigs = detect_ma_crossover(fast, slow)
        types = [s['type'] for s in sigs]
        assert 'golden' in types  # fast crosses above
        assert 'death' in types   # fast crosses back below


@pytest.mark.unit
class TestRegimeDetection:
    def test_strong_bull_on_steady_uptrend(self):
        from tofu_trading.trading_signals import detect_trend_regime
        navs = _navs([100 * (1.01 ** i) for i in range(80)])  # +1%/day
        out = detect_trend_regime(navs)
        assert out[-1] in ('strong_bull', 'bull')

    def test_bear_on_downtrend(self):
        from tofu_trading.trading_signals import detect_trend_regime
        navs = _navs([100 * (0.99 ** i) for i in range(80)])
        out = detect_trend_regime(navs)
        assert out[-1] in ('strong_bear', 'bear')

    def test_volatility_regime_low_on_flat(self):
        from tofu_trading.trading_signals import detect_volatility_regime
        navs = _navs([100 + (0.01 * (i % 2)) for i in range(40)])  # nearly flat
        out = detect_volatility_regime(navs)
        assert out[-1] == 'low_vol'

    def test_signal_snapshot_needs_60(self):
        from tofu_trading.trading_signals import compute_signal_snapshot
        snap = compute_signal_snapshot(_navs([100, 101, 102]))
        assert 'error' in snap

    def test_signal_snapshot_full_shape(self):
        from tofu_trading.trading_signals import compute_signal_snapshot
        navs = _navs([100 * (1.005 ** i) for i in range(80)])
        snap = compute_signal_snapshot(navs)
        assert 'error' not in snap
        assert -100 <= snap['composite_score'] <= 100
        assert snap['signal'] in ('strong_buy', 'buy', 'weak_buy', 'neutral',
                                  'weak_sell', 'sell', 'strong_sell')
        # uptrend → non-negative score
        assert snap['composite_score'] > 0

    def test_signal_series_no_future_leak(self):
        from tofu_trading.trading_signals import compute_signal_series
        navs = _navs([100 * (1.003 ** i) for i in range(70)])
        series = compute_signal_series(navs, compute_every=5)
        # first computed index is >= MIN_HISTORY (60)
        assert all(idx >= 60 for idx, _, _ in series)


# ═══════════════════════════════════════════════════════════
#  Position sizing — lib/trading_risk.py
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKelly:
    def test_kelly_positive_edge(self):
        from tofu_trading.trading_risk import kelly_fraction
        # p=0.6, win=loss=1 → full kelly = (0.6*1 - 0.4)/1 = 0.2; half = 0.1
        assert kelly_fraction(0.6, 1.0, 1.0) == pytest.approx(0.1)

    def test_kelly_capped_at_quarter(self):
        from tofu_trading.trading_risk import kelly_fraction
        # huge edge → clipped to 0.25
        assert kelly_fraction(0.9, 5.0, 1.0) == 0.25

    def test_kelly_no_edge_is_zero(self):
        from tofu_trading.trading_risk import kelly_fraction
        # p=0.4 losing edge → max(0, …) = 0
        assert kelly_fraction(0.4, 1.0, 1.0) == 0

    def test_kelly_degenerate_inputs(self):
        from tofu_trading.trading_risk import kelly_fraction
        assert kelly_fraction(0.6, 1.0, 0) == 0   # avg_loss 0
        assert kelly_fraction(0, 1.0, 1.0) == 0
        assert kelly_fraction(1, 1.0, 1.0) == 0


@pytest.mark.unit
class TestVolatilityTarget:
    def test_weight_inverse_to_vol(self):
        from tofu_trading.trading_risk import volatility_target_position
        out = volatility_target_position(100000, 0.30, target_volatility=0.15)
        # weight = 0.15/0.30 = 0.5 → capped at 0.40
        assert out['weight'] == pytest.approx(0.40)
        assert out['amount'] == pytest.approx(40000.0)

    def test_weight_min_floor(self):
        from tofu_trading.trading_risk import volatility_target_position
        # vol=10 → raw weight 0.15/10 = 0.015 < 0.02 floor → clamped to 0.02
        out = volatility_target_position(100000, 10.0, target_volatility=0.15)
        assert out['weight'] == pytest.approx(0.02)  # min floor

    def test_unknown_vol_conservative(self):
        from tofu_trading.trading_risk import volatility_target_position
        out = volatility_target_position(100000, 0, target_volatility=0.15)
        assert out['weight'] == pytest.approx(0.10)


@pytest.mark.unit
class TestRiskParity:
    def test_inverse_vol_weights_sum_to_one(self):
        from tofu_trading.trading_risk import risk_parity_weights
        w = risk_parity_weights({'A': 0.10, 'B': 0.20})
        # inv: A=10, B=5, total=15 → A=0.6667, B=0.3333
        assert w['A'] == pytest.approx(0.6667, abs=1e-3)
        assert w['B'] == pytest.approx(0.3333, abs=1e-3)
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)

    def test_empty(self):
        from tofu_trading.trading_risk import risk_parity_weights
        assert risk_parity_weights({}) == {}


# ═══════════════════════════════════════════════════════════
#  Stop-loss / take-profit + drawdown protector
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStopLoss:
    def test_fixed_stop_triggers(self):
        from tofu_trading.trading_risk import StopLossManager
        m = StopLossManager()
        m.add_position('X', entry_nav=1.0, entry_date='2024-01-01', fixed_stop_pct=-0.08)
        assert m.update('X', 1.0, '2024-01-02') is None  # flat, no trigger
        action = m.update('X', 0.91, '2024-01-03')       # -9% < -8%
        assert action and action['action'] == 'stop_loss' and action['type'] == 'fixed'

    def test_take_profit_partial(self):
        from tofu_trading.trading_risk import StopLossManager
        m = StopLossManager()
        m.add_position('X', entry_nav=1.0, entry_date='2024-01-01', take_profit_pct=0.25)
        action = m.update('X', 1.30, '2024-01-10')  # +30% >= +25%
        assert action and action['action'] == 'take_profit' and action['partial'] is True

    def test_trailing_stop_after_profit(self):
        from tofu_trading.trading_risk import StopLossManager
        m = StopLossManager()
        m.add_position('X', entry_nav=1.0, entry_date='2024-01-01',
                       trailing_stop_pct=-0.06)
        m.update('X', 1.20, '2024-01-05')             # peak 1.20 → trail 1.128
        action = m.update('X', 1.10, '2024-01-06')    # +10% profit, below trail
        assert action and action['action'] == 'stop_loss' and action['type'] == 'trailing'


@pytest.mark.unit
class TestDrawdownProtector:
    def test_levels_escalate(self):
        from tofu_trading.trading_risk import DrawdownProtector
        p = DrawdownProtector(100000)
        assert p.update(100000)['level'] == 'normal'
        assert p.update(94000)['level'] == 'warning'   # -6%
        assert p.update(89000)['level'] == 'caution'   # -11%, buys stop
        crit = p.update(84000)                          # -16%
        assert crit['level'] == 'critical'
        assert crit['force_sell'] is True and crit['force_sell_pct'] == 0.3

    def test_force_sell_fires_once_per_level(self):
        from tofu_trading.trading_risk import DrawdownProtector
        p = DrawdownProtector(100000)
        p.update(84000)                       # critical → force sell
        again = p.update(83000)               # still critical, deeper
        assert again['force_sell'] is False   # already triggered this level

    def test_new_high_resets(self):
        from tofu_trading.trading_risk import DrawdownProtector
        p = DrawdownProtector(100000)
        p.update(84000)                       # critical, triggered
        p.update(110000)                      # new high → reset peak + triggers
        back = p.update(93500)                # -15% from 110k = critical again
        assert back['force_sell'] is True     # re-armed after new high


# ═══════════════════════════════════════════════════════════
#  Regime params + trade filter
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRegimeRiskParams:
    def test_strong_bull_raises_equity(self):
        from tofu_trading.trading_risk import get_regime_risk_params
        p = get_regime_risk_params('strong_bull', 'low_vol')
        assert p['max_equity_pct'] >= 0.80
        assert p['buy_scale'] >= 1.0

    def test_extreme_vol_caps_equity(self):
        from tofu_trading.trading_risk import get_regime_risk_params
        p = get_regime_risk_params('bull', 'extreme_vol')
        assert p['max_equity_pct'] <= 0.35
        assert p['buy_scale'] < 1.0


@pytest.mark.unit
class TestFilterTradeDecisions:
    def _ctx(self, buy_scale=1.0, dd_force=False):
        risk_params = {'buy_scale': buy_scale, 'max_equity_pct': 0.40,
                       'new_position_max_pct': 0.15}
        dd = {'buy_scale': 0 if dd_force else buy_scale, 'level': 'caution',
              'drawdown_pct': -11, 'force_sell': False, 'force_sell_pct': 0}
        risk = {'total_value': 100000}
        return risk, risk_params, dd

    def test_buy_blocked_when_circuit_breaker(self):
        from tofu_trading.trading_risk import filter_trade_decisions
        risk, rp, dd = self._ctx(dd_force=True)
        approved, blocked = filter_trade_decisions(
            [{'symbol': 'X', 'action': 'buy', 'amount': 1000, 'signal_score': 50}],
            risk, rp, dd, {})
        assert not approved and len(blocked) == 1
        assert 'Circuit breaker' in blocked[0]['block_reason']

    def test_buy_scaled_down(self):
        from tofu_trading.trading_risk import filter_trade_decisions
        risk, rp, dd = self._ctx(buy_scale=0.5)
        approved, blocked = filter_trade_decisions(
            [{'symbol': 'X', 'action': 'buy', 'amount': 1000, 'signal_score': 50}],
            risk, rp, dd, {})
        assert len(approved) == 1
        assert approved[0]['amount'] == pytest.approx(500.0)

    def test_negative_signal_blocked(self):
        from tofu_trading.trading_risk import filter_trade_decisions
        risk, rp, dd = self._ctx()
        approved, blocked = filter_trade_decisions(
            [{'symbol': 'X', 'action': 'buy', 'amount': 1000, 'signal_score': -30}],
            risk, rp, dd, {})
        assert not approved and 'Negative signal' in blocked[0]['block_reason']

    def test_sell_always_approved(self):
        from tofu_trading.trading_risk import filter_trade_decisions
        risk, rp, dd = self._ctx(dd_force=True)
        approved, blocked = filter_trade_decisions(
            [{'symbol': 'X', 'action': 'sell', 'amount': 1000, 'signal_score': 0}],
            risk, rp, dd, {})
        assert len(approved) == 1 and approved[0]['action'] == 'sell'

    def test_position_size_capped(self):
        from tofu_trading.trading_risk import filter_trade_decisions
        risk, rp, dd = self._ctx()
        # existing 35% weight, max_equity 40% → can only add 5% = 5000
        positions = {'X': {'weight': 0.35, 'current_value': 35000}}
        approved, blocked = filter_trade_decisions(
            [{'symbol': 'X', 'action': 'buy', 'amount': 20000, 'signal_score': 50}],
            risk, rp, dd, positions)
        assert len(approved) == 1
        assert approved[0]['amount'] == pytest.approx(5000.0)
        assert 'capped_reason' in approved[0]


# ═══════════════════════════════════════════════════════════
#  Backtest reporting metrics — lib/trading_backtest_engine/reporting.py
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBacktestMetrics:
    def _state(self, values, trade_log=None):
        class _S:
            pass
        s = _S()
        s.daily_values = [{'date': f'2024-01-{i + 1:02d}', 'value': float(v)}
                          for i, v in enumerate(values)]
        s.trade_log = trade_log or []
        s.positions = {}
        s.total_fees = 0.0
        s.drawdown_levels = []
        return s

    def test_total_return_and_no_drawdown_on_monotonic_curve(self):
        from tofu_trading.trading_backtest_engine.reporting import compute_metrics
        values = [100000 * (1.01 ** i) for i in range(22)]  # +1%/day, monotonic
        m = compute_metrics(self._state(values), {},
                            [d['date'] for d in self._state(values).daily_values],
                            100000)
        summ = m['summary']
        # +1%/day for 21 steps ≈ +23.2%
        assert summ['total_return_pct'] == pytest.approx(23.24, abs=0.1)
        # monotonic up → zero drawdown, positive sharpe
        assert summ['max_drawdown_pct'] == pytest.approx(0.0, abs=1e-6)
        assert summ['sharpe_ratio'] > 0

    def test_max_drawdown_detected(self):
        from tofu_trading.trading_backtest_engine.reporting import compute_metrics
        # peak 100k → trough 80k → recovery
        values = [100000, 105000, 90000, 80000, 95000]
        st = self._state(values)
        m = compute_metrics(st, {}, [d['date'] for d in st.daily_values], 100000)
        # max dd from peak 105k to 80k = -23.8%
        assert m['summary']['max_drawdown_pct'] == pytest.approx(23.81, abs=0.1)

    def test_win_rate_uses_cost_basis(self):
        from tofu_trading.trading_backtest_engine.reporting import compute_metrics
        # one winning sell (proceeds > cost), one losing (proceeds < cost)
        log = [
            {'type': 'sell', 'amount': 1300, 'net_proceeds': 1290, 'cost_basis': 1000},
            {'type': 'sell', 'amount': 900, 'net_proceeds': 890, 'cost_basis': 1000},
        ]
        st = self._state([100000, 101000], trade_log=log)
        m = compute_metrics(st, {}, [d['date'] for d in st.daily_values], 100000)
        assert m['summary']['win_rate_pct'] == pytest.approx(50.0)

    def test_empty_state(self):
        from tofu_trading.trading_backtest_engine.reporting import compute_metrics
        class _Empty:
            daily_values = []
        m = compute_metrics(_Empty(), {}, [], 100000)
        assert 'error' in m
