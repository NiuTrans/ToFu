"""tests/test_reconcile.py — the three gates, lot rounding, and skip-a-day invariance.

The headline property is ``test_skipping_days_changes_nothing``: the whole
reason for replacing the daily-briefing model is that a user who ignores the
system for a week should see a correct plan when they come back, not a backlog
of stale commands. That is a property of the DESIGN (stateless recomputation),
so it is asserted directly rather than inferred.

Every gate has a NEUTER partner: a test that fails if the gate is removed. A
gate with no NEUTER is indistinguishable from a gate that never fires.
"""

import importlib.util
import logging
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, '..')


def _load_reconcile():
    """Import reconcile.py without dragging in the host-dependent package."""
    # Respect the real host lib when it is importable (e.g. host on
    # PYTHONPATH): pytest imports every test module at collection time, so an
    # unconditional stub here would shadow the real package for OTHER suites
    # sharing the process (measured: ModuleNotFoundError 'lib.database').
    try:
        import lib.log  # noqa: F401
    except ImportError:
        if 'lib' not in sys.modules:
            lib = types.ModuleType('lib'); lib.__path__ = []
            log = types.ModuleType('lib.log')
            log.get_logger = lambda n: logging.getLogger(n)
            sys.modules['lib'] = lib
            sys.modules['lib.log'] = log
    if 'tt_rec' in sys.modules:
        return sys.modules['tt_rec']
    spec = importlib.util.spec_from_file_location(
        'tt_rec', os.path.join(_ROOT, 'tofu_trading', 'reconcile.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules['tt_rec'] = m
    spec.loader.exec_module(m)
    return m


R = _load_reconcile()


def _load_common():
    """Load trading/_common.py by path.

    A plain ``from tofu_trading.trading._common import ...`` cannot work here:
    _load_reconcile() installs a stub ``lib`` module, which shadows the host's
    real ``lib`` and makes the trading package __init__ fail on
    ``lib._pkg_utils``. Loading the leaf module directly sidesteps the package
    __init__ entirely, so the REAL classifier is exercised rather than a
    reimplementation of it.
    """
    if 'tt_common' in sys.modules:
        return sys.modules['tt_common']
    spec = importlib.util.spec_from_file_location(
        'tt_common', os.path.join(_ROOT, 'tofu_trading', 'trading', '_common.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules['tt_common'] = m
    spec.loader.exec_module(m)
    return m


# Publish the leaf-loaded _common under the exact name reconcile.lot_size_for
# imports, so that helper exercises the REAL classifier here rather than its
# "assume 100" fallback. Without this the fund case silently tests the fallback
# and would still pass if classification were broken.
# (Cross-checked in the real runtime, where the normal import works: 003003 -> 1.)
_common_mod = _load_common()
for _name, _path in (('tofu_trading', os.path.join(_ROOT, 'tofu_trading')),
                     ('tofu_trading.trading',
                      os.path.join(_ROOT, 'tofu_trading', 'trading'))):
    if _name not in sys.modules:
        _pkg = types.ModuleType(_name)
        _pkg.__path__ = [_path]
        sys.modules[_name] = _pkg
sys.modules['tofu_trading.trading._common'] = _common_mod


# A 100k portfolio: 600519 is 30% underweight, everything else on target.
PRICES = {'600519': 100.0, '510300': 4.0, '000001': 10.0}


def _params(**kw):
    base = dict(deadband_pct=5.0, min_ticket=1000.0, min_abs_drift=500.0)
    base.update(kw)
    return R.ReconcileParams(**base)


# ── drift maths ────────────────────────────────────────────────────

@pytest.mark.unit
def test_drift_is_positive_when_underweight():
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 50.0}],
        positions=[{'symbol': '600519', 'shares': 200}],   # 20k of 100k = 20%
        prices=PRICES, cash=80000.0)
    d = next(x for x in drifts if x['symbol'] == '600519')
    assert d['actual_weight'] == pytest.approx(20.0)
    assert d['drift_pct'] == pytest.approx(30.0)
    assert d['drift_amount'] == pytest.approx(30000.0)


@pytest.mark.unit
def test_pending_shares_count_toward_portfolio_value():
    """Excluding paid-for pending shares would inflate every other weight."""
    with_pending = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 50.0}],
        positions=[{'symbol': '600519', 'shares': 100, 'pending_shares': 100}],
        prices=PRICES, cash=80000.0)
    d = next(x for x in with_pending if x['symbol'] == '600519')
    assert d['actual_weight'] == pytest.approx(20.0), \
        'pending shares must be included in market value'


@pytest.mark.unit
def test_missing_price_never_produces_an_action():
    """A data outage must not read as 'sell everything'."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 0.0}],
        positions=[{'symbol': '600519', 'shares': 200}],
        prices={}, cash=100000.0)
    d = next(x for x in drifts if x['symbol'] == '600519')
    assert d['price_missing'] is True
    assert d['drift_amount'] == 0.0
    actions, skipped = R.plan_actions(drifts, _params(), cash=100000.0)
    assert actions == []
    assert any(s['gate'] == 'price_missing' for s in skipped)


# ── Gate 1: deadband ───────────────────────────────────────────────

@pytest.mark.unit
def test_gate_deadband_suppresses_small_wobble():
    """A 3% drift must not generate churn.

    Sized so the order WOULD clear the lot and ticket floors (¥3,000 = 30
    shares... no: 3% of 100k = ¥3,000, and at ¥4/share that is 750 shares = 7
    lots). Using 510300 keeps the lot floor cheap so the deadband is provably
    the gate doing the work -- see the NEUTER below.
    """
    drifts = R.compute_drift(
        targets=[{'symbol': '510300', 'target_weight': 23.0}],
        positions=[{'symbol': '510300', 'shares': 5000}],   # 20k of 100k = 20%
        prices=PRICES, cash=80000.0)
    actions, skipped = R.plan_actions(drifts, _params(), cash=80000.0)
    assert actions == []
    assert any(s['gate'] == 'deadband' for s in skipped)


@pytest.mark.unit
def test_neuter_deadband_would_let_the_wobble_through():
    """NEUTER: with the deadband at 0 the same wobble becomes an action.

    Proves the previous test is held up by the DEADBAND specifically. The
    amount (¥3,000 at ¥4/share = 750 shares = 7 whole lots) clears both the
    lot floor and the ¥1,000 ticket, so nothing else can be doing the
    suppressing.
    """
    drifts = R.compute_drift(
        targets=[{'symbol': '510300', 'target_weight': 23.0}],
        positions=[{'symbol': '510300', 'shares': 5000}],
        prices=PRICES, cash=80000.0)
    actions, skipped = R.plan_actions(
        drifts, _params(deadband_pct=0.0, min_abs_drift=0.0), cash=80000.0)
    assert actions, (
        f'deadband gate is what suppressed this, as intended; '
        f'instead got skips: {[s["gate"] for s in skipped]}')


# ── Gate 2: minimum ticket ─────────────────────────────────────────

@pytest.mark.unit
def test_gate_min_ticket_suppresses_tiny_order():
    """Large % drift on a small portfolio still yields a trivial order."""
    drifts = R.compute_drift(
        targets=[{'symbol': '510300', 'target_weight': 100.0}],
        positions=[{'symbol': '510300', 'shares': 100}],   # 400 of 1000
        prices=PRICES, cash=600.0)
    actions, skipped = R.plan_actions(
        drifts, _params(min_abs_drift=0.0), cash=600.0)
    assert actions == []
    assert any(s['gate'] in ('min_ticket', 'min_ticket_after_lot')
               for s in skipped)


@pytest.mark.unit
def test_neuter_min_ticket_would_let_tiny_order_through():
    drifts = R.compute_drift(
        targets=[{'symbol': '510300', 'target_weight': 100.0}],
        positions=[{'symbol': '510300', 'shares': 100}],
        prices=PRICES, cash=600.0)
    actions, _ = R.plan_actions(
        drifts, _params(min_ticket=0.0, min_abs_drift=0.0), cash=600.0)
    assert actions, 'min-ticket gate is what suppressed this'


# ── Gate 3: no in-flight shares ────────────────────────────────────

@pytest.mark.unit
def test_gate_in_flight_blocks_symbol_with_pending_shares():
    """T+1: shares bought today cannot be traded today."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 90.0}],
        positions=[{'symbol': '600519', 'shares': 100, 'pending_shares': 100}],
        prices=PRICES, cash=80000.0)
    actions, skipped = R.plan_actions(drifts, _params(), cash=80000.0)
    assert actions == []
    assert any(s['gate'] == 'in_flight' for s in skipped)


@pytest.mark.unit
def test_gate_in_flight_respects_settle_date():
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 90.0}],
        positions=[{'symbol': '600519', 'shares': 200}],
        prices=PRICES, cash=80000.0)
    for d in drifts:
        d['settle_date'] = '2026-07-27'          # tomorrow
    actions, skipped = R.plan_actions(
        drifts, _params(), cash=80000.0, today='2026-07-26')
    assert actions == []
    assert any(s['gate'] == 'in_flight' for s in skipped)

    # Once settled, the same drift becomes actionable.
    actions2, _ = R.plan_actions(
        drifts, _params(), cash=80000.0, today='2026-07-28')
    assert actions2, 'after settlement the symbol must be tradeable again'


@pytest.mark.unit
def test_neuter_in_flight_would_double_count():
    """NEUTER: without the gate, a pending symbol gets re-recommended."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 90.0}],
        positions=[{'symbol': '600519', 'shares': 100, 'pending_shares': 100}],
        prices=PRICES, cash=80000.0)
    for d in drifts:
        d['pending_shares'] = 0                  # simulate the gate being blind
    actions, _ = R.plan_actions(drifts, _params(), cash=80000.0)
    assert actions, 'in-flight gate is what suppressed this'


# ── Lot rounding ───────────────────────────────────────────────────

@pytest.mark.unit
def test_stock_actions_are_whole_lots():
    """'buy 37 shares' is not an executable A-share instruction."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 100.0}],
        positions=[], prices=PRICES, cash=53700.0)
    actions, _ = R.plan_actions(drifts, _params(), cash=53700.0)
    assert actions
    a = actions[0]
    assert a['shares'] % 100 == 0, f'not a round lot: {a["shares"]}'
    assert a['amount'] <= 53700.0, 'must not exceed available cash'


@pytest.mark.unit
def test_lot_size_matches_instrument_type():
    """Only OPEN-END funds escape lot rounding.

    Note 11xxxx is an exchange-traded BOND, not an open-end fund -- it trades
    in lots like a stock. Picking such a code as the 'fund' example is an easy
    mistake and would assert the wrong contract, so the real classifier's
    verdict is asserted alongside.
    """
    classify_asset_code = _load_common().classify_asset_code

    assert classify_asset_code('000001') == 'stock'
    assert classify_asset_code('510300') == 'etf'
    assert classify_asset_code('110022') == 'bond'
    assert classify_asset_code('003003') == 'fund'

    assert R.lot_size_for('000001') == 100     # stock (SZ main board)
    assert R.lot_size_for('510300') == 100     # ETF
    assert R.lot_size_for('110022') == 100     # exchange-traded bond
    assert R.lot_size_for('003003') == 1       # open-end fund


@pytest.mark.unit
def test_round_to_lot_never_rounds_up():
    """Rounding up spends cash the user does not have."""
    assert R.round_to_lot(199, 100) == 100
    assert R.round_to_lot(99, 100) == 0
    assert R.round_to_lot(250.7, 100) == 200


@pytest.mark.unit
def test_sell_never_exceeds_shares_held():
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 0.0}],
        positions=[{'symbol': '600519', 'shares': 300}],
        prices=PRICES, cash=0.0)
    actions, _ = R.plan_actions(drifts, _params(), cash=0.0)
    assert actions
    assert actions[0]['side'] == 'sell'
    assert actions[0]['shares'] <= 300


# ── The design property ────────────────────────────────────────────

@pytest.mark.unit
def test_skipping_days_changes_nothing():
    """★ The whole point of the redesign.

    The old model stored one command row per day, so ignoring the system left a
    backlog of contradictory, stale-priced instructions. Here the plan is a
    pure function of (target, actual, price) -- so 'day 1' and 'day 5 having
    done nothing' produce byte-identical output. There is no backlog because
    nothing is ever stored as a pending command.
    """
    targets = [{'symbol': '600519', 'target_weight': 50.0}]
    positions = [{'symbol': '600519', 'shares': 200}]

    day1, _ = R.plan_actions(
        R.compute_drift(targets, positions, PRICES, cash=80000.0),
        _params(), cash=80000.0, today='2026-07-26')
    day5, _ = R.plan_actions(
        R.compute_drift(targets, positions, PRICES, cash=80000.0),
        _params(), cash=80000.0, today='2026-07-30')
    assert day1 == day5


@pytest.mark.unit
def test_partial_follow_shrinks_the_remaining_action():
    """Following half the advice must leave only the remainder, re-priced."""
    targets = [{'symbol': '600519', 'target_weight': 50.0}]

    before, _ = R.plan_actions(
        R.compute_drift(targets, [{'symbol': '600519', 'shares': 200}],
                        PRICES, cash=80000.0),
        _params(), cash=80000.0)
    after, _ = R.plan_actions(
        R.compute_drift(targets, [{'symbol': '600519', 'shares': 350}],
                        PRICES, cash=65000.0),
        _params(), cash=65000.0)

    assert before and after
    assert after[0]['amount'] < before[0]['amount'], \
        'acting on part of the advice must shrink what remains'


@pytest.mark.unit
def test_sells_are_planned_before_buys():
    """A buy-first plan can propose more than the user can fund."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 60.0},
                 {'symbol': '000001', 'target_weight': 0.0}],
        positions=[{'symbol': '600519', 'shares': 100},
                   {'symbol': '000001', 'shares': 5000}],
        prices=PRICES, cash=0.0)
    actions, _ = R.plan_actions(drifts, _params(), cash=0.0)
    sides = [a['side'] for a in actions]
    if 'sell' in sides and 'buy' in sides:
        assert sides.index('sell') < sides.index('buy')


@pytest.mark.unit
def test_every_skip_names_its_gate():
    """The UI must be able to explain an empty plan, not just show nothing."""
    drifts = R.compute_drift(
        targets=[{'symbol': '600519', 'target_weight': 21.0}],
        positions=[{'symbol': '600519', 'shares': 200}],
        prices=PRICES, cash=80000.0)
    _, skipped = R.plan_actions(drifts, _params(), cash=80000.0)
    assert skipped
    for s in skipped:
        assert s.get('gate'), 'skip with no gate name is unexplainable'
        assert s.get('note'), 'skip with no human-readable note'
