"""tests/test_simulator_execution_timing.py — decisions fill at T+1, never same-bar.

THE BIAS THIS REMOVES
---------------------
Signals are built from bars with ``date <= decision_date``, so they see T's
CLOSE. Execution used to read that same close, i.e. the simulator decided at T's
close and filled at T's close. No participant can do that: once T's close is
known the earliest tradeable price is T+1's open.

HOW THIS IS VERIFIED — deterministically, not statistically
------------------------------------------------------------
Feed a series whose open/close relationship is KNOWN, then assert the fill
price directly. With every bar opening 5% above the prior close:

    decision bar close = 10.0        (what the decision saw)
    next bar open      = 10.5        (the earliest tradeable price)
    asserted fill      = 10.5        -> passes only under the T+1 rule
                                        (would be 10.0 under same-bar fills)

One run settles it. No seeds, no averaging, no sampling error.

⚠ AN EARLIER VERSION OF THIS FILE CLAIMED A MEASURED RETURN DROP
(-0.362% ETF / -0.532% stock across 12 seeds, "7/12 seeds down"). **That table
was withdrawn: it was noise, not an effect.** Re-running the same A/B with a
different 16 seeds flipped the sign (+0.106% / +0.136%). The per-seed delta has
a standard deviation of 1.452% / 2.466%, i.e. a standard error of 0.363% /
0.617% — LARGER than the effect being claimed (t = 0.29 / 0.22). "7 of 12 down"
is what a fair coin does.

The deeper problem was that the number was not merely weak, it was INSENSITIVE
to the thing under test: break the fill rule and that metric would still print
a plausible "7/12 down". A measurement that cannot fail when the mechanism
fails is not evidence.

Rule taken from this, applied to any change on a random path: before accepting
"the mean should move", compute the standard error; if the effect is not
comfortably larger than it (t >= 2), switch to a deterministic assertion.

WHY NO FALLBACK IS ALLOWED
--------------------------
When T+1 has no bar (suspension, or the decision lands on the last bar), the
trade must be SKIPPED with a recorded reason. Falling back to T's close looks
like a harmless safety net and silently reinstates the exact bias — so the
no-fallback rule is asserted directly, not left to review.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import random

import pytest

@pytest.fixture
def sim_db(trading_connection_factory):
    conn = trading_connection_factory()
    from tofu_trading.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(conn)
    yield conn
    conn.close()


def _bars(db, symbol, *, days=60, seed=5, vol=0.25, gap=0.4):
    """Seed bars where OPEN differs from the prior CLOSE (an overnight gap).

    The gap is what makes the two execution rules distinguishable: with
    ``open == prior close`` every assertion here would hold under both.
    """
    random.seed(seed)
    sigma = vol / math.sqrt(252)
    px = 10.0
    day = dt.date(2024, 1, 1)
    out = []
    while len(out) < days:
        prev = px
        px *= math.exp(random.gauss(-0.5 * sigma ** 2, sigma))
        if day.weekday() < 5:
            op = round(prev * math.exp(random.gauss(0, sigma * gap)), 4)
            db.execute(
                'INSERT OR REPLACE INTO trading_sim_prices'
                ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                (symbol, day.strftime('%Y-%m-%d'), round(px, 4), op, round(px, 4)))
            out.append({'date': day.strftime('%Y-%m-%d'), 'open': op, 'close': round(px, 4)})
        day += dt.timedelta(days=1)
    db.commit()
    return out


# ═══════════════════════════════════════════════════════════
#  1. The fill resolver itself
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNextBarFill:
    def test_returns_next_bar_open(self, sim_db):
        from tofu_trading.trading.llm_simulator import _next_bar_fill

        bars = _bars(sim_db, '600519', days=10, seed=3)
        price, err = _next_bar_fill(sim_db, '600519', bars[0]['date'], bars[-1]['date'])
        assert err is None
        assert price == pytest.approx(bars[1]['open']), (
            'fill must be the NEXT bar open, not this bar')

    def test_never_returns_the_decision_bar_close(self, sim_db):
        """The whole point: the fill must not be the price the decision saw."""
        from tofu_trading.trading.llm_simulator import _next_bar_fill

        bars = _bars(sim_db, '600519', days=10, seed=3)
        for i in range(len(bars) - 1):
            price, err = _next_bar_fill(
                sim_db, '600519', bars[i]['date'], bars[-1]['date'])
            if err:
                continue
            assert price != pytest.approx(bars[i]['close']), (
                f"bar {i}: filled at the decision bar's own close")

    def test_last_bar_is_unfillable(self, sim_db):
        """A decision on the final bar has no T+1 — it must NOT fill."""
        from tofu_trading.trading.llm_simulator import _next_bar_fill

        bars = _bars(sim_db, '600519', days=10, seed=3)
        price, err = _next_bar_fill(
            sim_db, '600519', bars[-1]['date'], bars[-1]['date'])
        assert price is None
        assert err and 'T+1' in err

    def test_suspension_is_reported_not_papered_over(self, sim_db):
        """A bar with no open must be refused, never silently replaced.

        Substituting that bar's close would not be a look-ahead, but it is still
        not the price an order placed at T would have received — so it is
        reported rather than quietly used.
        """
        from tofu_trading.trading.llm_simulator import _next_bar_fill

        bars = _bars(sim_db, '600519', days=10, seed=3)
        sim_db.execute(
            'UPDATE trading_sim_prices SET open=0 WHERE symbol=? AND date=?',
            ('600519', bars[1]['date']))
        sim_db.commit()

        price, err = _next_bar_fill(
            sim_db, '600519', bars[0]['date'], bars[-1]['date'])
        assert price is None, 'suspended bar was filled anyway'
        assert err and '开盘价' in err

    def test_gap_over_a_weekend_uses_the_next_trading_bar(self, sim_db):
        """'T+1' means the next TRADING bar, not the next calendar day."""
        from tofu_trading.trading.llm_simulator import _next_bar_fill

        bars = _bars(sim_db, '600519', days=15, seed=9)
        for i, bar in enumerate(bars[:-1]):
            wd = dt.datetime.strptime(bar['date'], '%Y-%m-%d').weekday()
            if wd == 4:  # Friday
                price, err = _next_bar_fill(
                    sim_db, '600519', bar['date'], bars[-1]['date'])
                assert err is None
                assert price == pytest.approx(bars[i + 1]['open'])
                return
        pytest.skip('no Friday in the generated window')


# ═══════════════════════════════════════════════════════════
#  2. No same-bar close survives on any execution path
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoSameBarExecution:
    def test_buy_fills_at_next_open(self, sim_db, monkeypatch):
        import lib.llm_dispatch as LD
        import lib.llm_dispatch.api as LDA
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        bars = _bars(sim_db, '600519', days=40, seed=21)
        state = {'n': 0}

        def fake(*args, **kwargs):
            state['n'] += 1
            if state['n'] == 1:
                return ('<decisions>[{"action":"buy","symbol":"600519",'
                        '"amount":25000,"confidence":95,"reason":"x"}]</decisions>', {})
            return ('<decisions>[]</decisions>', {})

        monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
        monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)

        cfg = SimulatorConfig(symbols=['600519'], start_date=bars[0]['date'],
                              end_date=bars[-1]['date'], step_days=1,
                              initial_capital=100000, stop_loss_pct=99,
                              take_profit_pct=999)
        res = run_simulation(sim_db, cfg, uid=1)

        buys = [t for t in res['trade_log'] if t['action'] == 'buy']
        assert buys, 'scripted buy never executed'
        fill = buys[0]['price']
        by_date = {b['date']: b for b in bars}
        decided = buys[0]['date']
        assert fill != pytest.approx(by_date[decided]['close']), (
            'buy filled at the decision bar close — look-ahead still present')
        # It must equal the NEXT bar's open.
        nxt = [b for b in bars if b['date'] > decided][0]
        assert fill == pytest.approx(nxt['open'])

    def test_fill_price_differs_from_close_across_many_trades(self, sim_db, monkeypatch):
        """Aggregate check: over a momentum run, no fill equals its own bar close.

        A single trade could coincide by luck; a whole run cannot.
        """
        import lib.llm_dispatch as LD
        import lib.llm_dispatch.api as LDA
        import tofu_trading.trading.llm_simulator as S

        bars = _bars(sim_db, '510300', days=60, seed=33)
        by_date = {b['date']: b for b in bars}
        dates = [b['date'] for b in bars]
        idx = {d: i for i, d in enumerate(dates)}
        seen = {'d': None}

        original = S._build_signal_context

        def spy(db, syms, as_of):
            seen['d'] = as_of
            return original(db, syms, as_of)

        monkeypatch.setattr(S, '_build_signal_context', spy, raising=True)

        def fake(*args, **kwargs):
            i = idx.get(seen['d'], 0)
            if i >= 1 and by_date[dates[i]]['close'] > by_date[dates[i - 1]]['close']:
                return ('<decisions>[{"action":"buy","symbol":"510300",'
                        '"amount":25000,"confidence":95,"reason":"m"}]</decisions>', {})
            return ('<decisions>[{"action":"sell","symbol":"510300",'
                    '"confidence":95,"reason":"m"}]</decisions>', {})

        monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
        monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)

        cfg = S.SimulatorConfig(symbols=['510300'], start_date=bars[0]['date'],
                                end_date=bars[-1]['date'], step_days=1,
                                initial_capital=100000, stop_loss_pct=99,
                                take_profit_pct=999)
        res = S.run_simulation(sim_db, cfg, uid=1)

        trades = [t for t in res['trade_log'] if t.get('price')]
        assert len(trades) >= 5, f'need several trades to judge, got {len(trades)}'
        same_bar = [t for t in trades
                    if t['date'] in by_date
                    and abs(t['price'] - by_date[t['date']]['close']) < 1e-9]
        # The forced end-of-run liquidation is priced off the last known bar and
        # is not a decision fill, so allow exactly that one.
        assert len(same_bar) <= 1, (
            f'{len(same_bar)}/{len(trades)} fills equal their own bar close')


# ═══════════════════════════════════════════════════════════
#  3. Source-level ban on the silent fallback
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeterministicFillPrice:
    """Every fill path priced against a KNOWN series — one run, no statistics.

    Each bar opens a fixed percentage away from the prior close, so the correct
    fill price is arithmetic rather than a distribution. This is the guard that
    replaced the withdrawn seeded A/B: it fails the instant any path reverts to
    same-bar pricing, whereas the A/B could not tell that apart from noise.

    Stop-loss and take-profit get their own cases deliberately. They changed the
    most (the breach is detected on T's close but the exit fills at T+1's open)
    and previously had no price assertion at all — only the buy path did. The
    specific hazard is P&L being computed from the TRIGGER price instead of the
    FILL price, which no amount of "did it exit?" checking would catch.
    """

    @staticmethod
    def _rigged(db, symbol, *, gap, days=40, start=10.0):
        """Bars where each OPEN is ``gap`` away from the prior CLOSE.

        Returns the bar list. close is held flat at ``start`` so the only price
        that moves is the open — making 'which price filled this' unambiguous.
        """
        day = dt.date(2024, 1, 1)
        out = []
        while len(out) < days:
            if day.weekday() < 5:
                op = round(start * (1 + gap), 4)
                cl = round(start, 4)
                db.execute(
                    'INSERT OR REPLACE INTO trading_sim_prices'
                    ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                    (symbol, day.strftime('%Y-%m-%d'), cl, op, cl))
                out.append({'date': day.strftime('%Y-%m-%d'), 'open': op, 'close': cl})
            day += dt.timedelta(days=1)
        db.commit()
        return out

    @staticmethod
    def _trending(db, symbol, *, close_step, open_gap, days=20, start=10.0):
        """Bars with a KNOWN open/close relationship AND a trending close.

        ``close_step`` moves the close each bar (so a stop/target is actually
        reached — thresholds are evaluated on the close), while ``open_gap``
        offsets each open from the PRIOR close by a fixed, distinctive amount.
        The gap is what makes the fill price identifiable: an open never equals
        any close, so 'which price filled this' has exactly one answer.
        """
        cl = start
        day = dt.date(2024, 1, 1)
        out = []
        while len(out) < days:
            if day.weekday() < 5:
                op = round(cl * (1 + open_gap), 4)
                cl = round(cl * (1 + close_step), 4)
                db.execute(
                    'INSERT OR REPLACE INTO trading_sim_prices'
                    ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                    (symbol, day.strftime('%Y-%m-%d'), cl, op, cl))
                out.append({'date': day.strftime('%Y-%m-%d'), 'open': op, 'close': cl})
            day += dt.timedelta(days=1)
        db.commit()
        return out

    @staticmethod
    def _script(monkeypatch, decisions_by_call):
        import lib.llm_dispatch as LD
        import lib.llm_dispatch.api as LDA

        state = {'n': 0}

        def fake(*args, **kwargs):
            state['n'] += 1
            return (decisions_by_call(state['n']), {})

        monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
        monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)

    def test_buy_fills_at_the_known_next_open(self, sim_db, monkeypatch):
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        bars = self._rigged(sim_db, '600519', gap=0.05)
        self._script(monkeypatch, lambda n: (
            '<decisions>[{"action":"buy","symbol":"600519","amount":30000,'
            '"confidence":95,"reason":"x"}]</decisions>' if n == 1
            else '<decisions>[]</decisions>'))

        cfg = SimulatorConfig(symbols=['600519'], start_date=bars[0]['date'],
                              end_date=bars[-1]['date'], step_days=1,
                              initial_capital=100000, stop_loss_pct=99,
                              take_profit_pct=999)
        res = run_simulation(sim_db, cfg, uid=1)
        buys = [t for t in res['trade_log'] if t['action'] == 'buy']
        assert buys, 'scripted buy never executed'
        assert buys[0]['price'] == pytest.approx(bars[1]['open']), (
            f"buy filled at {buys[0]['price']}, expected the T+1 open "
            f"{bars[1]['open']} (the decision bar close was {bars[0]['close']})")
        assert buys[0]['price'] != pytest.approx(bars[0]['close'])

    def test_discretionary_sell_fills_at_the_known_next_open(self, sim_db, monkeypatch):
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        bars = self._rigged(sim_db, '600519', gap=0.05)

        def script(n):
            if n == 1:
                return ('<decisions>[{"action":"buy","symbol":"600519","amount":30000,'
                        '"confidence":95,"reason":"x"}]</decisions>')
            if n == 3:
                return ('<decisions>[{"action":"sell","symbol":"600519",'
                        '"confidence":95,"reason":"x"}]</decisions>')
            return '<decisions>[]</decisions>'

        self._script(monkeypatch, script)
        cfg = SimulatorConfig(symbols=['600519'], start_date=bars[0]['date'],
                              end_date=bars[-1]['date'], step_days=1,
                              initial_capital=100000, stop_loss_pct=99,
                              take_profit_pct=999)
        res = run_simulation(sim_db, cfg, uid=1)
        sells = [t for t in res['trade_log']
                 if t['action'] == 'sell' and not t.get('trigger')]
        assert sells, 'scripted sell never executed'
        decided = sells[0]['date']
        nxt = [b for b in bars if b['date'] > decided]
        assert nxt, 'sell decided on the final bar — not a valid case here'
        assert sells[0]['price'] == pytest.approx(nxt[0]['open'])

    def test_stop_loss_fills_and_prices_pnl_at_the_next_open(self, sim_db, monkeypatch):
        """The exit price AND the recorded P&L must both come from the fill.

        A stop that exits at the right time but books P&L at the trigger price
        reports a loss that never happened — invisible to any check that only
        asks whether the position was closed.
        """
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        # Close falls 3% a bar so a 5% stop is genuinely breached (thresholds
        # are evaluated on the close); each open sits 2% ABOVE the prior close,
        # so the fill price cannot be confused with any close in the series.
        bars = self._trending(sim_db, '600519', close_step=-0.03, open_gap=0.02)
        self._script(monkeypatch, lambda n: (
            '<decisions>[{"action":"buy","symbol":"600519","amount":30000,'
            '"confidence":95,"reason":"x"}]</decisions>' if n == 1
            else '<decisions>[]</decisions>'))

        cfg = SimulatorConfig(symbols=['600519'], start_date=bars[0]['date'],
                              end_date=bars[-1]['date'], step_days=1,
                              initial_capital=100000, adaptive_stop=False,
                              stop_loss_pct=5, take_profit_pct=999)
        res = run_simulation(sim_db, cfg, uid=1)
        stops = [t for t in res['trade_log'] if t.get('trigger') == 'stop_loss']
        assert stops, 'no stop-loss fired on a series whose close falls 3% a bar'

        stop = stops[0]
        nxt = [b for b in bars if b['date'] > stop['date']]
        assert nxt, 'stop landed on the final bar'
        expected_fill = nxt[0]['open']
        assert stop['price'] == pytest.approx(expected_fill), (
            f"stop filled at {stop['price']}, expected T+1 open {expected_fill}")

        buys = [t for t in res['trade_log'] if t['action'] == 'buy']
        entry = buys[0]['price']
        expected_pnl = (expected_fill - entry) / entry * 100
        assert stop['pnl_pct'] == pytest.approx(expected_pnl, abs=0.01), (
            f"stop booked {stop['pnl_pct']:.3f}% but the fill implies "
            f"{expected_pnl:.3f}% — P&L computed from the trigger, not the fill")

    def test_take_profit_fills_and_prices_pnl_at_the_next_open(self, sim_db, monkeypatch):
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        # Mirror of the stop case: close RISES 3% a bar so a 5% target is
        # reached, with each open 2% BELOW the prior close so the fill price
        # stays distinguishable from every close.
        bars = self._trending(sim_db, '600519', close_step=0.03, open_gap=-0.02)
        self._script(monkeypatch, lambda n: (
            '<decisions>[{"action":"buy","symbol":"600519","amount":30000,'
            '"confidence":95,"reason":"x"}]</decisions>' if n == 1
            else '<decisions>[]</decisions>'))

        cfg = SimulatorConfig(symbols=['600519'], start_date=bars[0]['date'],
                              end_date=bars[-1]['date'], step_days=1,
                              initial_capital=100000, adaptive_stop=False,
                              stop_loss_pct=99, take_profit_pct=5)
        res = run_simulation(sim_db, cfg, uid=1)
        tps = [t for t in res['trade_log'] if t.get('trigger') == 'take_profit']
        assert tps, 'no take-profit fired on a series whose close rises 3% a bar'

        tp = tps[0]
        nxt = [b for b in bars if b['date'] > tp['date']]
        assert nxt, 'take-profit landed on the final bar'
        expected_fill = nxt[0]['open']
        assert tp['price'] == pytest.approx(expected_fill)

        buys = [t for t in res['trade_log'] if t['action'] == 'buy']
        entry = buys[0]['price']
        expected_pnl = (expected_fill - entry) / entry * 100
        assert tp['pnl_pct'] == pytest.approx(expected_pnl, abs=0.01), (
            f"take-profit booked {tp['pnl_pct']:.3f}% but the fill implies "
            f"{expected_pnl:.3f}% — P&L computed from the trigger, not the fill")


@pytest.mark.unit
class TestNoSilentFallback:
    def test_execution_sites_do_not_read_the_decision_bar_price(self):
        """No execution path may resolve its fill from `sim_date`.

        Comments are stripped first: the reason the fallback is forbidden is
        DISCUSSED at length in the comments around these very lines, so a naive
        scan would flag exactly the code that documents the fix.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tofu_trading', 'trading', 'llm_simulator.py')
        stripped = []
        for line in open(path, encoding='utf-8'):
            quote = None
            cut = len(line)
            for i, ch in enumerate(line):
                if quote:
                    if ch == quote:
                        quote = None
                elif ch in '"\'':
                    quote = ch
                elif ch == '#':
                    cut = i
                    break
            stripped.append(line[:cut])
        code = '\n'.join(stripped)

        # Valuation may read the decision bar (marking a position to market at T
        # is correct and necessary); EXECUTION may not. So the ban is scoped to
        # the FILL sites, identified by the variables that become a trade price —
        # a blanket ban on price_data['nav'] would wrongly flag the valuation
        # line `current_nav = price_data['nav']`, which is legitimate.
        assert '_next_bar_fill(' in code
        assert code.count('_next_bar_fill(') >= 4, (
            'expected buy, sell, stop-loss and take-profit to all fill via T+1')
        # Match whole assignments, not substrings: the legitimate valuation line
        # `current_nav = price_data['nav']` ENDS WITH the banned fill pattern
        # `nav = price_data['nav']`, so a substring test flags valid code. Compare
        # the assignment TARGET instead.
        banned_targets = {'nav', 'close_price', 'fill_price', 'price'}
        offenders = []
        for lineno, raw in enumerate(stripped, 1):
            line = raw.strip()
            if '=' not in line or 'price_data' not in line:
                continue
            target = line.split('=', 1)[0].strip()
            if target in banned_targets:
                offenders.append(f'{lineno}: {line}')
        assert not offenders, (
            'fill price taken from the decision bar again: ' + '; '.join(offenders))
