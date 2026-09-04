"""tests/test_trading_fee_wiring.py — the engines MUST bill through fee_book.

WHY THIS EXISTS
---------------
The correct fee maths existed for a long time in info.py while both engines
quietly billed their own hardcoded 0.15%/0.5%. Nothing failed, because nothing
asserted that the engines USE the shared source — the maths being right somewhere
in the tree is not the same as it being right on the path that charges money.

These guards therefore assert the WIRING, not the arithmetic (arithmetic is
covered by test_trading_fee_single_source.py):

  1. No engine may carry a hardcoded fee literal or a per-instance rate scalar.
  2. `short_sell_penalty` must stay deleted. It was a second implementation of
     the fund tiered-redemption fee applied to every asset type: on a fund held
     3 days it double-charged (1.5% + 1.5% = 3.0%), and on a stock held 3 days
     it charged 1.5% where the truth is 0.076% — a 20x overcharge on a fee that
     does not exist in A-shares.
  3. A stock and an ETF must be billed differently by the engine (stamp tax is
     sell-side and stock-only), which is impossible with a single scalar rate.
  4. Identical inputs must produce byte-identical runs.
  5. The prompt the model reads must be generated from the SAME FeeBook the
     ledger bills from. The prompt used to claim 0.1% round-trip while the
     ledger took 0.65%.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ENGINE_SOURCES = (
    'tofu_trading/trading/llm_simulator.py',
    'tofu_trading/trading_backtest_engine/engine.py',
    'tofu_trading/trading_backtest_engine/intel_backtest.py',
    'tofu_trading/trading_backtest_engine/strategies.py',
    'tofu_trading/trading_backtest_engine/config.py',
    'tofu_trading/web/handlers/trading_simulator.py',
)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_comments(text: str) -> str:
    """Drop Python line comments before scanning source text.

    A comment must never be able to satisfy a guard, nor to violate one. The
    fee literals are DISCUSSED at length in the comments that explain why they
    were removed, so a naive scan would flag exactly the files that document
    the fix.
    """
    out = []
    for line in text.splitlines():
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
        out.append(line[:cut])
    return '\n'.join(out)


def _gbm(vol: float, n: int = 252, seed: int = 1, s0: float = 10.0):
    """Zero-drift geometric Brownian motion — no alpha, so cost is the only signal."""
    import datetime as dt

    random.seed(seed)
    sigma = vol / math.sqrt(252)
    px = s0
    out = []
    d = dt.date(2024, 1, 1)
    for _ in range(n):
        px *= math.exp(random.gauss(-0.5 * sigma ** 2, sigma))
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        out.append({'date': d.strftime('%Y-%m-%d'), 'nav': round(px, 4)})
        d += dt.timedelta(days=1)
    return out


# ═══════════════════════════════════════════════════════════
#  1. No hardcoded rates anywhere on the billing path
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoHardcodedRates:
    def test_no_fee_literals_in_engine_sources(self):
        offenders = []
        for rel in _ENGINE_SOURCES:
            path = os.path.join(_repo_root(), rel)
            code = _strip_comments(open(path, encoding='utf-8').read())
            for lit in ('0.0015', '0.005', '0.015'):
                if lit in code:
                    offenders.append(f'{rel}: {lit}')
        assert not offenders, (
            'hardcoded fee literals on the billing path: ' + '; '.join(offenders))

    def test_no_per_instance_rate_scalars(self):
        """A single scalar cannot express the floor, the stamp tax, or the tiers."""
        offenders = []
        for rel in _ENGINE_SOURCES:
            path = os.path.join(_repo_root(), rel)
            code = _strip_comments(open(path, encoding='utf-8').read())
            for attr in ('self.buy_fee_rate', 'self.sell_fee_rate',
                         'cfg.buy_fee_rate', 'cfg.sell_fee_rate',
                         'config.buy_fee_rate', 'config.sell_fee_rate'):
                if attr in code:
                    offenders.append(f'{rel}: {attr}')
        assert not offenders, 'rate scalars still in use: ' + '; '.join(offenders)

    def test_short_sell_penalty_stays_deleted(self):
        for rel in _ENGINE_SOURCES:
            path = os.path.join(_repo_root(), rel)
            code = _strip_comments(open(path, encoding='utf-8').read())
            assert 'short_sell_penalty' not in code, (
                f'{rel}: short_sell_penalty is a duplicate of the fund tier table '
                '(double-charges funds, 20x overcharges stocks)')

    def test_engines_import_fee_book(self):
        for rel in ('tofu_trading/trading/llm_simulator.py',
                    'tofu_trading/trading_backtest_engine/engine.py',
                    'tofu_trading/trading_backtest_engine/intel_backtest.py'):
            code = open(os.path.join(_repo_root(), rel), encoding='utf-8').read()
            assert 'FeeBook' in code, f'{rel} does not reference FeeBook'


# ═══════════════════════════════════════════════════════════
#  2. The engine really bills per asset type
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEngineBillsPerAssetType:
    def test_stock_costs_more_than_etf_on_the_same_path(self):
        """Stamp tax is sell-side and stock-only — unreachable with one scalar.

        Same price series, same strategy: the ONLY difference is the code, so
        any fee gap proves the engine resolved the asset type.
        """
        from tofu_trading.trading_backtest_engine.engine import BacktestEngine

        series = _gbm(0.20, seed=11)
        fees = {}
        for code in ('510300', '600519'):
            eng = BacktestEngine({'strategy': 'buy_and_hold', 'initial_capital': 100000})
            result = eng.run({code: list(series)})
            fees[code] = result.get('summary', {}).get('total_fees', 0)

        assert fees['510300'] > 0 and fees['600519'] > 0
        assert fees['600519'] != fees['510300'], (
            'stock and ETF billed identically — asset type was not resolved')

    def test_fund_costs_more_than_etf(self):
        """Open-end funds carry a subscription fee ETFs do not."""
        from tofu_trading.trading_backtest_engine.engine import BacktestEngine

        series = _gbm(0.20, seed=11)
        fees = {}
        for code in ('510300', '003003'):
            eng = BacktestEngine({'strategy': 'buy_and_hold', 'initial_capital': 100000})
            result = eng.run({code: list(series)})
            fees[code] = result.get('summary', {}).get('total_fees', 0)

        assert fees['003003'] > fees['510300'] * 2

    def test_etf_cost_is_in_the_right_ballpark(self):
        """Buy-and-hold pays ONE commission: ~0.025% of deployed capital.

        Pins the absolute magnitude, not just relative ordering — the old
        0.15% buy rate would land 6x above this ceiling.
        """
        from tofu_trading.trading_backtest_engine.engine import BacktestEngine

        eng = BacktestEngine({'strategy': 'buy_and_hold', 'initial_capital': 100000})
        result = eng.run({'510300': _gbm(0.18, seed=7)})
        fees = result.get('summary', {}).get('total_fees', 0)
        assert 15 < fees < 40, f'ETF buy-and-hold fees {fees:.2f} outside sane band'

    def test_zero_cost_switch_removes_all_fees(self):
        from tofu_trading.trading_backtest_engine.engine import BacktestEngine

        eng = BacktestEngine({'strategy': 'buy_and_hold', 'initial_capital': 100000,
                              'zero_cost': True})
        result = eng.run({'510300': _gbm(0.18, seed=7)})
        assert result.get('summary', {}).get('total_fees', -1) == 0


# ═══════════════════════════════════════════════════════════
#  3. Determinism through the real engine
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEngineDeterminism:
    def test_same_inputs_byte_identical_result(self):
        from tofu_trading.trading_backtest_engine.engine import BacktestEngine

        series = _gbm(0.18, seed=99)
        digests = []
        for _ in range(2):
            eng = BacktestEngine({'strategy': 'signal_driven', 'initial_capital': 100000})
            result = eng.run({'510300': list(series)})
            blob = json.dumps(result, sort_keys=True, default=str)
            digests.append(hashlib.sha256(blob.encode()).hexdigest())
        assert digests[0] == digests[1], 'backtest is not reproducible'


# ═══════════════════════════════════════════════════════════
#  4. Prompt and ledger cannot drift apart
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPromptMatchesLedger:
    def test_fee_description_derives_from_the_book(self):
        from tofu_trading.trading.fee_book import FeeBook
        from tofu_trading.trading.llm_simulator import _describe_fees

        book = FeeBook()
        text = _describe_fees(book, ['600519', '510300'], 30000)
        # The stock round-trip the ledger will actually charge.
        buy = book.fee_for('600519', 30000, 'buy')['fee_rate']
        sell = book.fee_for('600519', 30000, 'sell', holding_days=30)['fee_rate']
        assert f'{(buy + sell) * 100:.4f}%' in text
        assert '印花税' in text
        assert '最低' in text, 'the commission floor must be disclosed to the model'

    def test_system_prompt_has_no_competing_fee_schedule(self):
        """The system prompt stated 0.1% round-trip while the ledger took 0.65%."""
        code = _strip_comments(open(
            os.path.join(_repo_root(), 'tofu_trading/trading/llm_simulator.py'),
            encoding='utf-8').read())
        assert '万2.5' not in code, 'system prompt still hardcodes a fee schedule'
        assert '0.05%印花税' not in code, 'system prompt still hardcodes a stamp-tax rate'


# ═══════════════════════════════════════════════════════════
#  5. Accounting reconciliation + position anchor
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNetworkFeePathIsFencedOff:
    """The old network-fetching fee path must stay out of the decision loops.

    fee_book exists because ``info.fetch_trading_fee_info`` scrapes the vendor on
    the fund branch (measured 1068 ms for 003003 vs 0.0 ms for exchange-traded
    codes) and swallows failures, so a backtest calling it becomes both slow and
    non-deterministic. Building fee_book does not by itself CLOSE that door — the
    old function is still importable, and the next person to need a fee rate will
    reach for the obvious name unless the door is nailed shut.
    """

    _LOOP_MODULES = (
        'tofu_trading/trading/llm_simulator.py',
        'tofu_trading/trading_backtest_engine/engine.py',
        'tofu_trading/trading_backtest_engine/intel_backtest.py',
        'tofu_trading/trading_backtest_engine/strategies.py',
    )

    def test_no_loop_module_imports_the_network_fee_helpers(self):
        banned = ('fetch_trading_fee_info', 'estimate_trade_fee',
                  'calc_buy_fee', 'calc_sell_fee')
        offenders = []
        for rel in self._LOOP_MODULES:
            path = os.path.join(_repo_root(), rel)
            code = _strip_comments(open(path, encoding='utf-8').read())
            for name in banned:
                if name in code:
                    offenders.append(f'{rel}: {name}')
        assert not offenders, (
            'decision-loop modules must bill through fee_book, not the '
            'network-fetching helpers: ' + '; '.join(offenders))

    def test_the_warning_is_actually_on_the_function(self):
        """The docstring warning is part of the fence — assert it exists.

        A future reader who only reads the signature is exactly the person this
        protects, so the prohibition has to be where they will look.
        """
        from tofu_trading.trading.info import fetch_trading_fee_info

        doc = fetch_trading_fee_info.__doc__ or ''
        assert 'NOT FOR USE INSIDE A BACKTEST' in doc
        assert 'fee_book' in doc, 'the warning must name the correct alternative'

    def test_fee_book_is_the_documented_alternative_and_is_pure(self):
        """The fence is only credible if the sanctioned path is genuinely pure."""
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()

        def forbidden(self, *args, **kwargs):
            raise AssertionError('fee_book reached for the network on a loop path')

        original = FeeBook._from_network
        FeeBook._from_network = forbidden
        try:
            book.prewarm(['600519', '510300', '003003'])
            for sym in ('600519', '510300', '003003'):
                assert book.fee_for(sym, 20000, 'sell', holding_days=5)['fee_amount'] >= 0
        finally:
            FeeBook._from_network = original


@pytest.mark.unit
class TestAccountingFixes:
    def test_annualisation_uses_calendar_span(self):
        """A true 1-year +10% must report ~+10%, not +61.67%."""
        import datetime as dt

        from tofu_trading.trading.llm_simulator import _compute_metrics

        d = dt.date(2024, 1, 1)
        n = 50
        vals = []
        for i in range(n):
            vals.append({
                'date': (d + dt.timedelta(days=i * 7)).strftime('%Y-%m-%d'),
                'value': 100000 * (1 + 0.10 * i / (n - 1)),
            })
        m = _compute_metrics(vals, 100000, 0, 10, 6)
        assert m['total_return_pct'] == pytest.approx(10.0, abs=0.01)
        assert m['annualized_return_pct'] == pytest.approx(10.4, abs=1.5), (
            f"annualised {m['annualized_return_pct']} — sample count used as day count?")

    def test_annualisation_symmetric_on_losses(self):
        """A true -10% must not be reported as -41%."""
        import datetime as dt

        from tofu_trading.trading.llm_simulator import _compute_metrics

        d = dt.date(2024, 1, 1)
        n = 50
        vals = [{'date': (d + dt.timedelta(days=i * 7)).strftime('%Y-%m-%d'),
                 'value': 100000 * (1 - 0.10 * i / (n - 1))} for i in range(n)]
        m = _compute_metrics(vals, 100000, 0, 10, 4)
        assert m['annualized_return_pct'] > -13, (
            f"annualised {m['annualized_return_pct']} — loss inflated")

    def test_position_cap_follows_current_equity(self):
        """Anchored to initial_capital, a halved portfolio kept the same cap."""
        code = _strip_comments(open(
            os.path.join(_repo_root(), 'tofu_trading/trading/llm_simulator.py'),
            encoding='utf-8').read())
        assert 'max_amount = config.initial_capital * config.max_position_pct' not in code
        assert 'max_amount = portfolio_value * config.max_position_pct' in code


# ═══════════════════════════════════════════════════════════
#  6. Removed learning modules degrade LOUDLY
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRemovedModulesAreLoud:
    def test_adaptive_engine_absence_is_announced(self, caplog):
        import logging

        from tofu_trading.trading_backtest_engine import intel_backtest

        intel_backtest._ADAPTIVE_ENGINE_STATE.clear()
        with caplog.at_level(logging.WARNING):
            available = intel_backtest._adaptive_engine_available()

        if not available:
            assert any('adaptive_decision_engine' in r.message for r in caplog.records), (
                'a removed decision engine degraded silently')

    def test_availability_is_resolved_once(self):
        """Announced once per process, not once per simulated day."""
        from tofu_trading.trading_backtest_engine import intel_backtest

        intel_backtest._ADAPTIVE_ENGINE_STATE.clear()
        first = intel_backtest._adaptive_engine_available()
        assert 'available' in intel_backtest._ADAPTIVE_ENGINE_STATE
        assert intel_backtest._adaptive_engine_available() == first
