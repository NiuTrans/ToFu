"""tests/test_simulator_end_to_end.py — run_simulation actually executed.

WHY THIS SUITE EXISTS (the gap it closes)
-----------------------------------------
The fee wiring and the ledger reconciliation were verified through
``BacktestEngine``, ``_compute_metrics`` and ``compute_fee`` in isolation — but
NOTHING called ``run_simulation``. Measured before this file existed: zero tests
referenced it, and ``total_pnl`` was asserted nowhere in the suite. So the five
simulator fee sites and the final curve point that makes
``metrics.final_value`` agree with the DB were changed and reasoned about, but
never once EXECUTED.

That is the exact failure shape this project keeps hitting: logic reads correct,
guards are green, and the path in question never ran. ``final_value == initial +
total_pnl`` is a RUNTIME equality; only running it can prove it.

WHY A REAL DB WRAPPER IS REQUIRED
---------------------------------
``_ensure_sim_tables`` still emits the legacy DB-API SQL surface. The fixture
below runs it through ``TradingConnection``, whose bounded private evaluator
is the compatibility seam over sidecar documents. This executes the same path
production uses without opening a database file from the plugin process.

DETERMINISM
-----------
Prices are a fixed-seed zero-drift walk and the LLM is stubbed with a fixed
decision script, so every number here is reproducible. Zero drift also means
any equity change is attributable to fees.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

INITIAL = 100_000.0
START = '2024-01-01'
END = '2024-06-03'

# Extreme stop levels so a scripted position CANNOT be stopped out mid-run.
# Default thresholds (5%/15%) trigger on almost any multi-week holding, which
# made "hold to liquidation" structurally unreachable in earlier versions of
# these tests — the position was stopped out on day 5 and every assertion
# about the final liquidation point silently degenerated.
_FORCE_HOLD = {'stop_loss_pct': 99.0, 'take_profit_pct': 999.0}


@pytest.fixture
def sim_db(trading_connection_factory):
    """Production SQL compatibility seam over an in-memory document store."""
    conn = trading_connection_factory()
    from tofu_trading.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(conn)
    yield conn
    conn.close()


def _seed_prices(db, symbol: str, vol: float, *, seed: int, days: int = 150):
    """Insert a zero-drift walk so any equity change is attributable to fees.

    Writes ``open`` as well as ``nav``/``close``: execution fills at the NEXT
    bar's OPEN (decisions may only use data up to T, so the earliest tradeable
    price is T+1's open). A fixture that omits ``open`` makes every trade
    unfillable and the run silently does nothing.
    """
    random.seed(seed)
    sigma = vol / math.sqrt(252)
    px = 10.0
    d = dt.date(2024, 1, 1)
    rows = 0
    while rows < days:
        prev = px
        px *= math.exp(random.gauss(-0.5 * sigma ** 2, sigma))
        if d.weekday() < 5:
            op = round(prev * math.exp(random.gauss(0, sigma * 0.4)), 4)
            db.execute(
                'INSERT OR REPLACE INTO trading_sim_prices'
                ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                (symbol, d.strftime('%Y-%m-%d'), round(px, 4), op, round(px, 4)))
            rows += 1
        d += dt.timedelta(days=1)
    db.commit()


def _stub_llm(monkeypatch, buys):
    """Stub the LLM with a fixed script: buy on step 1, then hold forever.

    MUST patch ``lib.llm_dispatch.smart_chat`` itself. The simulator imports it
    INSIDE the decision function (llm_simulator.py:886), so the name is looked
    up on the dispatch module at call time and setting an attribute on
    ``llm_simulator`` has no effect whatsoever.

    That mistake is not merely ineffective, it is silently catastrophic for a
    test: the simulator then calls the REAL LLM, every call fails (measured:
    HTTP 402 quota exhausted), no decision is ever parsed, no trade executes —
    and assertions about fees and curves pass vacuously against a run in which
    nothing happened. ``_assert_traded`` below exists to make that impossible.
    """
    import lib.llm_dispatch as LD

    state = {'n': 0}
    decisions = ', '.join(
        f'{{"action":"buy","symbol":"{sym}","amount":{amt},'
        f'"confidence":95,"reason":"scripted"}}' for sym, amt in buys)

    def fake_chat(*args, **kwargs):
        state['n'] += 1
        if state['n'] == 1:
            return (f'<decisions>[{decisions}]</decisions>\n'
                    f'<strategies_used>[]</strategies_used>', {})
        return ('<decisions>[]</decisions>\n<strategies_used>[]</strategies_used>', {})

    monkeypatch.setattr(LD, 'smart_chat', fake_chat, raising=True)
    # lib.llm_dispatch is a package; smart_chat is DEFINED in lib.llm_dispatch.api
    # and re-exported. Patching only the package attribute did NOT intercept the
    # simulator's function-level `from lib.llm_dispatch import smart_chat` in the
    # test process (it resolved to api.smart_chat), which is why the first
    # version of this suite silently called the real LLM. Patch the defining
    # module too, at the name the import actually binds.
    try:
        import lib.llm_dispatch.api as LDA
        monkeypatch.setattr(LDA, 'smart_chat', fake_chat, raising=True)
    except (ImportError, AttributeError) as e:
        raise AssertionError(f'cannot stub smart_chat for the simulator: {e}')
    return state


def _assert_traded(res, *, expect_buys=1):
    """Fail loudly if the scripted decisions never became trades.

    Without this, a broken stub yields a run with zero trades and zero fees, and
    every downstream assertion holds trivially — the exact shape of a guard that
    cannot fail.

    A run that holds a position to the very end has exactly ONE sell — the
    forced liquidation — recorded on the curve tail, not necessarily in
    trade_log's mid-run entries. So 'no sell in trade_log' is NOT a failure when
    the curve tail shows the position was closed; asserting otherwise would make
    force-hold scenarios unassertable.
    """
    log = res.get('trade_log') or []
    buys = [t for t in log if t.get('action') == 'buy']
    sells = [t for t in log if t.get('action') == 'sell']
    assert len(buys) >= expect_buys, (
        f'scripted buy never executed (trade_log={log!r}) — LLM stub not in effect')
    assert res['metrics']['total_fees'] > 0, 'zero fees: no trade was billed'
    tail = (res.get('daily_values') or [{}])[-1]
    liquidated = tail.get('positions') == 0
    assert sells or liquidated, (
        'no sell recorded and the position was never liquidated')
    return buys, sells


def _run(db, symbols, buys, **overrides):
    from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

    cfg = SimulatorConfig(
        symbols=list(symbols), start_date=START, end_date=END,
        step_days=5, initial_capital=INITIAL, **overrides)
    return cfg, run_simulation(db, cfg, uid=1)


def _db_final(db, session_id: str) -> float:
    row = db.execute(
        'SELECT total_pnl FROM trading_sim_sessions WHERE session_id=?',
        (session_id,)).fetchone()
    assert row is not None, 'session row missing'
    return INITIAL + (row['total_pnl'] or 0.0)


# ═══════════════════════════════════════════════════════════
#  1. The reconciliation criterion — a runtime equality
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestLedgerReconciliation:
    def test_final_value_matches_db_total_pnl(self, sim_db, monkeypatch):
        """metrics.final_value MUST equal initial + DB total_pnl.

        These used to be two different numbers: final_value came from the equity
        curve tail (pre-liquidation) while total_pnl came from cash
        (post-liquidation), so the reported return and the stored P&L could
        disagree with no way to tell which was right.
        """
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        _cfg, res = _run(sim_db, ['600519'], [('600519', 30000)], **_FORCE_HOLD)

        assert res['status'] == 'completed'
        _assert_traded(res)
        metrics = res['metrics']
        assert metrics['final_value'] == pytest.approx(
            _db_final(sim_db, res['session_id']), abs=0.01), (
            'metrics.final_value and DB total_pnl disagree — two sources again')
        # After the forced liquidation the run holds NO position, so the curve
        # tail is pure cash — the exact thing that was previously absent.
        tail = res['daily_values'][-1]
        assert tail['positions'] == 0 and tail['cash'] == pytest.approx(
            tail['value'], abs=0.01), 'liquidation not reflected in the curve tail'

    def test_curve_ends_at_end_date(self, sim_db, monkeypatch):
        """The forced liquidation must appear in the equity curve.

        daily_values is appended at the TOP of each step, i.e. before that
        step's trades, so without an explicit final point the last step's
        decisions and the liquidation were both absent and total_return_pct
        lagged a full step behind.
        """
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        cfg, res = _run(sim_db, ['600519'], [('600519', 30000)], **_FORCE_HOLD)

        curve = res['daily_values']
        _assert_traded(res)
        assert curve, 'equity curve is empty'
        assert curve[-1]['date'] == cfg.end_date, (
            f"curve ends at {curve[-1]['date']}, not end_date {cfg.end_date}")
        # A DISTINCT liquidation point: the tail must be pure cash (positions=0).
        # If the final append is removed, the curve tail instead repeats the
        # pre-liquidation decision point, which still carries the open position —
        # that is what this pins, not merely the tail date (identical either way,
        # which is why the previous date-only assertion could not fail).
        assert curve[-1]['positions'] == 0, (
            'curve tail still holds a position — liquidation point missing')

    def test_return_pct_consistent_with_final_value(self, sim_db, monkeypatch):
        """total_return_pct must be derivable from final_value, not drift from it."""
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        _cfg, res = _run(sim_db, ['600519'], [('600519', 30000)])

        metrics = res['metrics']
        _assert_traded(res)
        implied = (metrics['final_value'] - INITIAL) / INITIAL * 100
        assert metrics['total_return_pct'] == pytest.approx(implied, abs=0.02)


# ═══════════════════════════════════════════════════════════
#  2. The simulator's fee sites really run through fee_book
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSimulatorBillsPerAssetType:
    """Each case gets its OWN database.

    Kept per-case even though the session_id collision it originally worked
    around is fixed (pt_ca7e1be82b904c48): a fee comparison should not depend on
    what a previous run left in the table, and isolation makes each case
    readable on its own. TestSessionIdCollision now asserts the shared-database
    case directly, so this isolation cannot hide a regression.
    """

    def _fees_for(self, trading_connection_factory, monkeypatch, symbol):
        db = trading_connection_factory(isolated=True)
        from tofu_trading.trading.historical_data import _ensure_sim_tables
        _ensure_sim_tables(db)
        _seed_prices(db, symbol, 0.22, seed=77)
        _stub_llm(monkeypatch, [(symbol, 30000)])
        _cfg, res = _run(db, [symbol], [(symbol, 30000)], **_FORCE_HOLD)
        assert res['status'] == 'completed'
        _assert_traded(res)
        fees = res['metrics']['total_fees']
        db.close()
        return fees, res

    def test_etf_and_stock_are_billed_differently(
        self, trading_connection_factory, monkeypatch
    ):
        """Same series, same script — only the code differs.

        Stamp tax is sell-side and stock-only, so a fee gap proves the simulator
        resolved the asset type instead of applying one scalar to everything.
        """
        etf_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '510300')
        stock_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '600519')

        assert etf_fees > 0 and stock_fees > 0
        assert stock_fees > etf_fees, (
            f'stock {stock_fees:.2f} not dearer than ETF {etf_fees:.2f} — '
            'stamp tax missing, so asset type was not resolved')

    def test_open_end_fund_is_dearest(
        self, trading_connection_factory, monkeypatch
    ):
        """A fund carries a subscription fee plus a tiered redemption fee.

        This is also the only path where holding_days matters: if any simulator
        sell site forgot to pass it, the fund would be billed the 0-day tier and
        this ordering would collapse.
        """
        etf_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '510300')
        fund_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '003003')

        assert fund_fees > etf_fees * 3, (
            f'fund {fund_fees:.2f} vs ETF {etf_fees:.2f} — fund schedule not applied')

    def test_fund_fee_uses_holding_period(
        self, trading_connection_factory, monkeypatch
    ):
        """holding_days must reach fee_book — pinned numerically.

        For a fund bought 2024-01-01 and liquidated 2024-06-03 (154 days), the
        correct tier is 0.25%; dropping holding_days (=0) charges the 1.5%
        short-term tier instead. Measured full-cycle cost: ~119.62 correct vs
        ~492.75 with the omission — a 4x gap. 'fund > 3x ETF' cannot see it
        because a fund is dearer than an ETF under BOTH; only an absolute
        anchor distinguishes them.
        """
        fund_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '003003')
        assert fund_fees == pytest.approx(119.62, rel=0.15), (
            f'fund fees {fund_fees:.2f} — holding_days not reaching fee_book '
            f'(would be ~492.75 if dropped, ~119.62 if honoured)')
        """Pin the magnitude: the old 0.65% round-trip would land far above this.

        A 30,000 buy-and-liquidate at the old flat rates cost ~195; the real ETF
        schedule costs ~15. An upper bound catches a silent revert that a
        relative comparison would miss.
        """
        etf_fees, _ = self._fees_for(
            trading_connection_factory, monkeypatch, '510300')
        assert etf_fees < 60, (
            f'ETF round-trip fees {etf_fees:.2f} — legacy flat rate may be back')


# ═══════════════════════════════════════════════════════════
#  3. Fee provenance is recorded on the run
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFeeProvenanceRecorded:
    def test_provenance_present_for_traded_symbols(self, sim_db, monkeypatch):
        """A stored run must say WHICH fee schedule produced its numbers."""
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        _cfg, res = _run(sim_db, ['600519'], [('600519', 30000)])

        prov = res['config'].get('fee_provenance', {})
        _assert_traded(res)
        assert '600519' in prov, f'no provenance recorded (keys={sorted(prov)})'
        assert prov['600519']['source'], 'provenance carries no source string'
        assert prov['600519']['asset_type'] == 'stock'

    def test_fund_is_flagged_as_estimated(self, sim_db, monkeypatch):
        """Open-end funds use a class default, and the run must admit it.

        Exchange-traded schedules are fully determined; a fund's per-product
        subscription rate is not, so its numbers are estimates and the run says
        so rather than presenting them as exact.
        """
        _seed_prices(sim_db, '003003', 0.22, seed=11)
        _stub_llm(monkeypatch, [('003003', 30000)])
        _cfg, res = _run(sim_db, ['003003'], [('003003', 30000)])

        estimated = res['config'].get('fee_estimated_symbols', [])
        assert '003003' in estimated, (
            f'fund not flagged as estimated (got {estimated})')

    def test_exchange_traded_is_not_flagged(self, sim_db, monkeypatch):
        _seed_prices(sim_db, '510300', 0.18, seed=11)
        _stub_llm(monkeypatch, [('510300', 30000)])
        _cfg, res = _run(sim_db, ['510300'], [('510300', 30000)])

        assert '510300' not in res['config'].get('fee_estimated_symbols', [])


# ═══════════════════════════════════════════════════════════
#  4. Determinism + the position anchor, end to end
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSessionIdCollision:
    """Two simulations in the same second must BOTH succeed. Now a real assertion.

    History worth keeping: session_id used to be minted as
    ``sim_{now:%Y%m%d_%H%M%S}`` against a UNIQUE column, so two runs started in
    the same second collided and the second died on an uncaught IntegrityError —
    no session row, no explanation. It bit double-clicks, batch/parameter sweeps
    (exactly what a quant-vs-LLM comparison does), and two users on a shared
    host, since the id carried no uid either.

    This was pinned with ``xfail(strict=True)`` precisely so it could not rot
    into a permanently-yellow test: the moment the defect was fixed
    (pt_ca7e1be82b904c48, ids now minted via ``tofu_trading.run_ids``) the strict
    marker turned the suite RED and forced this class to be revisited. That is
    the marker doing its job, and it is why the assertion below is live rather
    than still deferred.
    """

    def test_two_simulations_in_one_second_both_succeed(self, sim_db, monkeypatch):
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        ids = []
        for _ in range(2):
            _stub_llm(monkeypatch, [('600519', 30000)])
            _cfg, res = _run(sim_db, ['600519'], [('600519', 30000)])
            ids.append(res['session_id'])
        assert len(set(ids)) == 2, f'session ids collided: {ids}'


@pytest.mark.unit
class TestSimulatorDeterminism:
    def _run_isolated(self, trading_connection_factory, monkeypatch):
        """Run once against a private DB (see TestSessionIdCollision for why)."""
        db = trading_connection_factory(isolated=True)
        from tofu_trading.trading.historical_data import _ensure_sim_tables
        _ensure_sim_tables(db)
        _seed_prices(db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        _cfg, res = _run(db, ['600519'], [('600519', 30000)])
        _assert_traded(res)
        db.close()
        return res

    def test_two_runs_agree(self, trading_connection_factory, monkeypatch):
        outcomes = []
        for _ in range(2):
            m = self._run_isolated(
                trading_connection_factory, monkeypatch)['metrics']
            outcomes.append((m['final_value'], m['total_fees'], m['total_trades']))
        assert outcomes[0] == outcomes[1], f'simulator not reproducible: {outcomes}'

    def test_annualisation_not_inflated(self, sim_db, monkeypatch):
        """A ~5-month run must not report a wildly extrapolated annual figure.

        The old formula fed the SAMPLE COUNT into a 252-day exponent, so a
        21-sample run was annualised as if it spanned 21 trading days.
        """
        _seed_prices(sim_db, '600519', 0.30, seed=11)
        _stub_llm(monkeypatch, [('600519', 30000)])
        _cfg, res = _run(sim_db, ['600519'], [('600519', 30000)])

        m = res['metrics']
        _assert_traded(res)
        assert m['simulation_days'] > 120, (
            f"simulation_days={m['simulation_days']} — not a calendar span")
        total, ann = m['total_return_pct'], m['annualized_return_pct']
        assert abs(ann) < max(abs(total) * 4 + 5, 20), (
            f'annualised {ann:.2f}% vs total {total:.2f}% — extrapolation blown up')

    def test_position_cap_follows_equity(self, sim_db, monkeypatch):
        """The cap must be a fraction of CURRENT equity, never of initial capital.

        A single buy at full equity cannot distinguish the two anchors — both
        yield 20% of 100k. So this scenario first buys a DECLINING asset and
        lets it drag the portfolio down ~19% (80790 from 100000), then requests
        an oversized buy in a SECOND, flat asset. Under the fix the cap is 20%
        of the depleted 80790 (~16158); under the buggy initial_capital anchor
        it stays at 20000. Measured: the clamped buy lands at 16157.94.
        """
        for code, decay in (('600519', 0.95), ('510300', 1.0)):
            px = 10.0
            day = dt.date(2024, 1, 1)
            n = 0
            while n < 150:
                prev = px
                px *= decay
                if day.weekday() < 5:
                    # `open` is required: fills happen at the NEXT bar's open.
                    # Set it to the prior close so the decline stays exactly as
                    # designed and the cap arithmetic below is unaffected.
                    sim_db.execute(
                        'INSERT OR REPLACE INTO trading_sim_prices'
                        ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                        (code, day.strftime('%Y-%m-%d'), round(px, 4),
                         round(prev, 4), round(px, 4)))
                    n += 1
                day += dt.timedelta(days=1)
        sim_db.commit()

        def scripted(monkeypatch):
            import lib.llm_dispatch as LD
            import lib.llm_dispatch.api as LDA
            state = {'n': 0}

            def fake(*args, **kwargs):
                state['n'] += 1
                if state['n'] == 1:
                    return ('<decisions>[{"action":"buy","symbol":"600519",'
                            '"amount":25000,"confidence":95,"reason":"x"}]</decisions>', {})
                if state['n'] == 10:
                    return ('<decisions>[{"action":"buy","symbol":"510300",'
                            '"amount":90000,"confidence":95,"reason":"x"}]</decisions>', {})
                return ('<decisions>[]</decisions>', {})

            monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
            monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)

        scripted(monkeypatch)
        _cfg, res = _run(sim_db, ['600519', '510300'], [], max_position_pct=20,
                         **_FORCE_HOLD)

        buys = [(t['symbol'], t['amount']) for t in res['trade_log']
                if t['action'] == 'buy']
        assert len(buys) >= 2, f'expected two scripted buys, got {buys}'
        second = buys[1][1]
        # Anchored to the INVARIANT, not to a measured constant: the cap must be
        # 20% of equity at the time of the second buy, and equity by then is
        # BELOW initial (the first holding has been declining). A hardcoded
        # figure here breaks whenever anything legitimately changes the holding
        # path — the adaptive stop did exactly that, moving it 16157.94 ->
        # 16949.59 without the anchor being any less correct.
        assert second < INITIAL * 0.20 - 1, (
            f'second buy {second:.2f} is at/above 20% of INITIAL capital '
            f'({INITIAL * 0.20:.2f}) — the cap still follows initial_capital')
        # And it must be a real fraction of a DEPLETED account, not an arbitrary
        # small number: 20% of something between half and all of initial.
        assert INITIAL * 0.10 < second < INITIAL * 0.20, (
            f'second buy {second:.2f} outside the plausible 20%-of-depleted band')
