"""tests/test_simulator_adaptive_stop.py — the stop is stated in sigma, not percent.

WHY A FLAT PERCENTAGE IS THE WRONG SHAPE
----------------------------------------
A 5% stop is not a fixed amount of risk. How often it fires depends on the
asset's volatility and how long the position is held. Measured against the
default 5-trading-day cadence:

    broad ETF   5-day sigma 2.54%  ->  5% sits at 1.97 sigma
    blue chip   5-day sigma 4.23%  ->  5% sits at 1.18 sigma
    growth      5-day sigma 6.34%  ->  5% sits at 0.79 sigma

Below ~2 sigma the stop lives inside the asset's ordinary noise, so it mostly
fires on random wiggles — each one paying a full round trip of fees and
re-entering at a worse price. For growth names that is the common case, not the
exception (~10.8 noise exits per year per position).

Stating the threshold in sigma of the holding period makes it mean the same
thing across assets. Measured thresholds at 2 sigma (realised, from history
only):

    symbol     per-bar sigma   held=1   held=5   held=20
    510300           1.359%     3.00%    6.08%    12.15%
    600519           2.264%     4.53%   10.13%    20.25%
    300750           3.395%     6.79%   15.18%    25.00%

Versus a flat 5% for all three, at every holding length.

MEASURED EFFECT ON NOISE EXITS (10 seeds, zero-drift walk)
    510300: 3 -> 2 stop-outs (-33%)
    600519: 5 -> 4 stop-outs (-20%)
The reduction is modest at the default 5-day cadence because a young position
has few bars and the floor (min_stop_pct) binds; it grows with holding length,
which is where the mis-scaling was worst.

NO LOOK-AHEAD
-------------
sigma is measured from bars with ``date <= as_of`` only. Asserted directly,
because a volatility estimate that peeks at the future would silently make every
stop decision clairvoyant.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

@pytest.fixture
def sim_db(trading_connection_factory):
    conn = trading_connection_factory()
    from tofu_trading.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(conn)
    yield conn
    conn.close()


def _seed(db, symbol, vol, *, seed=11, days=150):
    random.seed(seed)
    sigma = vol / math.sqrt(252)
    px = 10.0
    day = dt.date(2024, 1, 1)
    n = 0
    while n < days:
        prev = px
        px *= math.exp(random.gauss(-0.5 * sigma ** 2, sigma))
        if day.weekday() < 5:
            db.execute(
                'INSERT OR REPLACE INTO trading_sim_prices'
                ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                (symbol, day.strftime('%Y-%m-%d'), round(px, 4),
                 round(prev, 4), round(px, 4)))
            n += 1
        day += dt.timedelta(days=1)
    db.commit()


# ═══════════════════════════════════════════════════════════
#  1. Realised sigma: correct, and blind to the future
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRealisedSigma:
    def test_tracks_actual_volatility(self, sim_db):
        from tofu_trading.trading.llm_simulator import _realised_sigma

        _seed(sim_db, '510300', 0.18)
        _seed(sim_db, '300750', 0.45)
        quiet = _realised_sigma(sim_db, '510300', '2024-05-01')
        wild = _realised_sigma(sim_db, '300750', '2024-05-01')
        assert quiet and wild
        assert wild > quiet * 1.5, (
            f'sigma failed to separate 18% from 45% vol ({quiet:.4f} vs {wild:.4f})')

    def test_ignores_bars_after_as_of(self, sim_db):
        """A volatility estimate must not see the future.

        Injecting a violent move AFTER as_of must not change the estimate; if it
        does, every stop decision downstream is clairvoyant.
        """
        from tofu_trading.trading.llm_simulator import _realised_sigma

        _seed(sim_db, '510300', 0.18)
        before = _realised_sigma(sim_db, '510300', '2024-04-01')
        sim_db.execute(
            'UPDATE trading_sim_prices SET nav=nav*3 WHERE symbol=? AND date>?',
            ('510300', '2024-04-01'))
        sim_db.commit()
        after = _realised_sigma(sim_db, '510300', '2024-04-01')
        assert after == pytest.approx(before), 'sigma peeked past as_of'

    def test_returns_none_on_thin_history(self, sim_db):
        from tofu_trading.trading.llm_simulator import _realised_sigma

        _seed(sim_db, '510300', 0.18, days=5)
        assert _realised_sigma(sim_db, '510300', '2024-01-05') is None


# ═══════════════════════════════════════════════════════════
#  2. The threshold differentiates by asset AND by holding length
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAdaptiveThreshold:
    def test_volatile_asset_gets_a_wider_stop(self, sim_db):
        """The whole point: one number cannot fit both assets."""
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '510300', 0.18)
        _seed(sim_db, '300750', 0.45)
        cfg = SimulatorConfig(adaptive_stop=True, stop_loss_sigma=2.0)
        quiet, b1 = _adaptive_stop_pct(sim_db, '510300', '2024-05-01', 5, cfg)
        wild, b2 = _adaptive_stop_pct(sim_db, '300750', '2024-05-01', 5, cfg)
        assert b1 == b2 == 'adaptive'
        assert wild > quiet * 1.5, (
            f'growth stop {wild:.2f}% not meaningfully wider than ETF {quiet:.2f}%')

    def test_stop_widens_with_holding_length(self, sim_db):
        """A position held longer has had more chances to wander.

        Held flat, the stop becomes a near-certain exit on any long hold.
        """
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '600519', 0.30)
        cfg = SimulatorConfig(adaptive_stop=True, stop_loss_sigma=2.0)
        widths = [_adaptive_stop_pct(sim_db, '600519', '2024-05-01', h, cfg)[0]
                  for h in (1, 5, 20)]
        assert widths == sorted(widths)
        assert widths[2] > widths[0] * 2

    def test_scales_with_sqrt_of_horizon(self, sim_db):
        """Volatility grows with sqrt(time), so the threshold must too."""
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '600519', 0.30)
        cfg = SimulatorConfig(adaptive_stop=True, stop_loss_sigma=2.0,
                              min_stop_pct=0.0, max_stop_pct=100.0)
        w4 = _adaptive_stop_pct(sim_db, '600519', '2024-05-01', 4, cfg)[0]
        w16 = _adaptive_stop_pct(sim_db, '600519', '2024-05-01', 16, cfg)[0]
        assert w16 == pytest.approx(w4 * 2, rel=0.02), (
            f'4->16 bars should double the width, got {w4:.3f} -> {w16:.3f}')

    def test_clamped_within_floor_and_cap(self, sim_db):
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '300750', 0.45)
        cfg = SimulatorConfig(adaptive_stop=True, stop_loss_sigma=2.0,
                              min_stop_pct=4.0, max_stop_pct=9.0)
        for held in (1, 5, 50, 200):
            pct, _ = _adaptive_stop_pct(sim_db, '300750', '2024-05-01', held, cfg)
            assert 4.0 <= pct <= 9.0, f'held={held} gave {pct}'

    def test_falls_back_to_fixed_when_sigma_unknown(self, sim_db):
        """Thin history must degrade to the configured fixed stop, and SAY so."""
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '510300', 0.18, days=5)
        cfg = SimulatorConfig(adaptive_stop=True, stop_loss_pct=5)
        pct, basis = _adaptive_stop_pct(sim_db, '510300', '2024-01-05', 3, cfg)
        assert basis == 'fixed'
        assert pct == pytest.approx(5.0)

    def test_opt_out_restores_the_flat_threshold(self, sim_db):
        from tofu_trading.trading.llm_simulator import (
            SimulatorConfig, _adaptive_stop_pct)

        _seed(sim_db, '300750', 0.45)
        cfg = SimulatorConfig(adaptive_stop=False, stop_loss_pct=7)
        pct, basis = _adaptive_stop_pct(sim_db, '300750', '2024-05-01', 10, cfg)
        assert basis == 'fixed'
        assert pct == pytest.approx(7.0)


# ═══════════════════════════════════════════════════════════
#  3. Exit-trigger stats are readable from the result
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestExitTriggerStats:
    def _run(self, db, monkeypatch, code, **cfgkw):
        import lib.llm_dispatch as LD
        import lib.llm_dispatch.api as LDA
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        state = {'n': 0}

        def fake(*args, **kwargs):
            state['n'] += 1
            if state['n'] == 1:
                return ('<decisions>[{"action":"buy","symbol":"%s","amount":25000,'
                        '"confidence":95,"reason":"x"}]</decisions>' % code, {})
            return ('<decisions>[]</decisions>', {})

        monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
        monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)
        cfg = SimulatorConfig(symbols=[code], start_date='2024-01-01',
                              end_date='2024-06-03', step_days=5,
                              initial_capital=100000, **cfgkw)
        return run_simulation(db, cfg, uid=1)

    def test_metrics_expose_the_breakdown(self, sim_db, monkeypatch):
        """Without this, a run stopped out by noise is indistinguishable from
        one that simply traded — which is the question the adaptive stop exists
        to answer."""
        _seed(sim_db, '600519', 0.30)
        res = self._run(sim_db, monkeypatch, '600519')
        m = res['metrics']
        assert 'exit_triggers' in m and isinstance(m['exit_triggers'], dict)
        assert 'stop_loss_exits' in m
        assert m['stop_loss_exits'] == m['exit_triggers'].get('stop_loss', 0)

    def test_stop_records_its_threshold_and_basis(self, sim_db, monkeypatch):
        """A stop-out must say WHICH threshold fired it, or the number above
        cannot be explained after the fact."""
        _seed(sim_db, '600519', 0.30)
        res = self._run(sim_db, monkeypatch, '600519',
                        adaptive_stop=True, stop_loss_sigma=0.3)
        stops = [t for t in res['trade_log'] if t.get('trigger') == 'stop_loss']
        if not stops:
            pytest.skip('no stop-out in this window')
        assert 'stop_pct' in stops[0] and 'stop_basis' in stops[0]
        assert stops[0]['stop_basis'] in ('adaptive', 'fixed')
        assert stops[0]['bars_held'] >= 1

    def test_adaptive_does_not_increase_noise_exits(
        self, trading_connection_factory, monkeypatch
    ):
        """Measured: 3->2 for the ETF and 5->4 for the stock across 10 seeds.

        Asserted as 'not worse' on a single seed rather than a fixed reduction:
        one seed is too small a sample to demand a specific drop, and a guard
        that hard-codes one is a coin flip dressed as a check.

        Each arm gets its OWN database: two simulations cannot share one in the
        same second (session_id is minted at second resolution against a UNIQUE
        column — a real defect, filed as pt_ca7e1be82b904c48 and pinned there).
        Isolating here sidesteps it without hiding it.
        """
        from tofu_trading.trading.historical_data import _ensure_sim_tables

        def arm(**cfgkw):
            db = trading_connection_factory(isolated=True)
            _ensure_sim_tables(db)
            _seed(db, '600519', 0.30)
            res = self._run(db, monkeypatch, '600519', **cfgkw)
            n = res['metrics']['stop_loss_exits']
            db.close()
            return n

        n_fixed = arm(adaptive_stop=False, stop_loss_pct=5)
        n_adaptive = arm(adaptive_stop=True, stop_loss_sigma=2.0)

        assert n_adaptive <= n_fixed, (
            f'adaptive stop fired MORE often ({n_adaptive} vs {n_fixed}) — '
            'a 2-sigma band should be wider than a flat 5% on a 30%-vol name')


# ═══════════════════════════════════════════════════════════
#  4. Both engines share one position cap
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPositionCapAligned:
    def test_engines_agree_on_max_positions(self):
        """The simulator capped at 5 while the backtest engine allowed 10.

        Fed identical data the two would diversify differently, so any
        quant-vs-LLM comparison across that gap measures the config difference
        as much as the strategies.
        """
        from tofu_trading.trading.llm_simulator import SimulatorConfig
        from tofu_trading.trading_backtest_engine.config import DEFAULT_CONFIG

        assert SimulatorConfig().max_positions == DEFAULT_CONFIG['max_positions']
