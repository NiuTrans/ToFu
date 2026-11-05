"""tests/test_trading_fee_single_source.py — guards for the fee resolution layer.

WHAT THIS PROTECTS (and why each guard exists)
----------------------------------------------
1. **Zero network inside the loop.** Fee resolution must remain independent of
   vendor availability. Every test blocks socket connections and DNS, and the
   guard records attempts so a provider cannot hide I/O by catching the error.

2. **Bit-identical determinism.** Same inputs → same bytes, twice. Without this
   a backtest is not reproducible and therefore not evidence.

3. **The ¥5 floor makes rates size-dependent.** Measured on ``600519`` buys:
   ¥500 → 1.000%, ¥50,000 → 0.025% — a 40x spread from the floor alone. Any
   acceptance number stated as a flat percentage is wrong by construction.

4. **Bonds must not be priced as open-end funds.** ``110022`` classifies as
   ``bond`` and previously fell through to the fund path, returning a 1.5%
   redemption fee — a 15x error, delivered silently.

5. **Misclassification must be visible.** Anything not fully determined is
   reported by ``estimated_symbols()`` so a run can label its own numbers.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import socket
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _NetworkUsed(AssertionError):
    """Raised when guarded code attempts a connection or DNS lookup."""


@contextmanager
def _network_guard(monkeypatch):
    """Reject I/O immediately and detect attempts swallowed by provider code."""
    attempts = 0

    def _boom(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise _NetworkUsed('network access attempted on a path that must be pure')

    with monkeypatch.context() as scoped:
        scoped.setattr(socket.socket, 'connect', _boom)
        scoped.setattr(socket.socket, 'connect_ex', _boom)
        scoped.setattr(socket, 'create_connection', _boom)
        scoped.setattr(socket, 'getaddrinfo', _boom)
        yield _boom
        if attempts:
            raise _NetworkUsed(f'{attempts} network attempt(s) swallowed by guarded code')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    with _network_guard(monkeypatch) as guard:
        yield guard


@pytest.mark.unit
@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'create_connection', 'getaddrinfo'])
def test_network_guard_detects_a_swallowed_connection_attempt(monkeypatch, operation):
    with pytest.raises(_NetworkUsed):
        with _network_guard(monkeypatch):
            try:
                if operation == 'getaddrinfo':
                    socket.getaddrinfo('test.invalid', 443)
                elif operation == 'create_connection':
                    socket.create_connection(('test.invalid', 443))
                else:
                    with socket.socket() as connection:
                        getattr(connection, operation)(('test.invalid', 443))
            except _NetworkUsed:
                pass  # Provider wrappers may catch Exception and return defaults.


# ═══════════════════════════════════════════════════════════
#  1. Purity — the whole point of the layer
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestZeroNetwork:
    def test_compute_fee_never_touches_network(self, no_network):
        """The per-trade maths must be pure for every asset type."""
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        for symbol in ('600519', '510300', '110022', '161725', '003003'):
            sched = default_schedule_for(symbol)
            for action in ('buy', 'sell'):
                r = compute_fee(sched, 20000.0, action, holding_days=3)
                assert r['fee_amount'] >= 0

    def test_loop_path_never_invokes_the_io_method(self, monkeypatch):
        """Even a cached I/O entry point must not run in the decision loop."""
        from tofu_trading.trading.fee_book import FeeBook

        forbidden = Mock(side_effect=AssertionError('loop path invoked _from_network'))
        monkeypatch.setattr(FeeBook, '_from_network', forbidden)

        book = FeeBook()
        book.prewarm(['600519', '510300'])
        book.fee_for('600519', 20000.0, 'buy')
        book.fee_for('003003', 20000.0, 'sell', holding_days=5)   # never prewarmed
        book.fee_for('161725', 20000.0, 'buy')                    # never prewarmed
        assert book.schedule_for('110022').asset_type == 'bond'
        forbidden.assert_not_called()

    def test_fee_book_lookup_never_touches_network(self, no_network):
        """Loop-path lookups stay pure even for symbols never prewarmed."""
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()
        r = book.fee_for('161725', 20000.0, 'sell', holding_days=5)
        r2 = book.fee_for('003003', 20000.0, 'buy')

        assert r['fee_amount'] >= 0
        assert r2['fee_amount'] >= 0
        assert book.schedule_for('600519').asset_type == 'stock'

    def test_prewarm_without_network_flag_stays_pure(self, no_network):
        """Default construction must not reach the vendor even during prewarm."""
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()
        resolved = book.prewarm(['600519', '510300', '003003'])
        assert set(resolved) == {'600519', '510300', '003003'}

# ═══════════════════════════════════════════════════════════
#  2. Determinism
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDeterminism:
    def test_same_inputs_bit_identical(self):
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('600519')
        a = compute_fee(sched, 23456.78, 'sell', holding_days=11)
        b = compute_fee(sched, 23456.78, 'sell', holding_days=11)
        assert repr(a) == repr(b)
        assert a['fee_amount'].hex() == b['fee_amount'].hex()

    def test_book_repeated_lookup_stable(self):
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()
        first = [book.fee_for(s, 20000.0, 'sell', 3)['fee_amount'].hex()
                 for s in ('600519', '510300', '110022', '003003')]
        second = [book.fee_for(s, 20000.0, 'sell', 3)['fee_amount'].hex()
                  for s in ('600519', '510300', '110022', '003003')]
        assert first == second

    def test_schedule_is_immutable(self):
        """Frozen: a schedule mutated mid-run would reintroduce non-determinism."""
        from dataclasses import FrozenInstanceError

        from tofu_trading.trading.fee_book import default_schedule_for

        sched = default_schedule_for('600519')
        with pytest.raises(FrozenInstanceError):
            sched.commission_rate = 0.99


# ═══════════════════════════════════════════════════════════
#  3. Minimum commission — rates are NOT constants
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMinimumCommission:
    def test_effective_rate_falls_as_size_rises(self):
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('600519')
        rates = [compute_fee(sched, amt, 'buy')['fee_rate']
                 for amt in (500, 1000, 3000, 10000, 50000)]
        assert rates == sorted(rates, reverse=True), 'floor must make small trades pricier'
        # ¥5 floor on ¥500 = 1%, plus the 0.001% transfer fee.
        assert rates[0] == pytest.approx(0.01001, rel=1e-6)
        # At ¥50,000 the floor no longer binds: 0.025% commission + transfer fee.
        assert rates[-1] == pytest.approx(0.00026, rel=1e-6)

    def test_floor_binds_below_breakeven(self):
        """Below ¥20,000 the ¥5 floor dominates — that is the whole point."""
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('600519')
        assert compute_fee(sched, 10000, 'buy')['commission'] == pytest.approx(5.0)
        assert compute_fee(sched, 50000, 'buy')['commission'] == pytest.approx(12.5)

    def test_flat_rate_assumption_is_wrong(self):
        """A flat 0.025% under-states an ¥8,000 trade — pinned as a real gap."""
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('600519')
        actual = compute_fee(sched, 8000, 'buy')['fee_rate']
        assert actual > 0.00025 * 2


# ═══════════════════════════════════════════════════════════
#  4. Per-type correctness
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPerAssetType:
    def test_stock_sell_carries_stamp_tax(self):
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('600519')
        assert compute_fee(sched, 100000, 'sell')['stamp_tax'] == pytest.approx(50.0)
        assert compute_fee(sched, 100000, 'buy')['stamp_tax'] == 0.0

    def test_etf_has_no_stamp_tax(self):
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('510300')
        assert sched.asset_type == 'etf'
        assert compute_fee(sched, 100000, 'sell')['stamp_tax'] == 0.0

    def test_bond_is_not_priced_as_a_fund(self):
        """110022 previously returned a 1.5% redemption fee — a 15x error.

        The assertion anchors on WHICH BRANCH ran, not on the resulting rate.
        A rate-only assertion is vacuous here: the bond default carries no
        sell_fee_rules, so a bond wrongly routed to the fund path iterates an
        empty tier list and still returns 0 — passing a 'rate is low' check
        while being on the wrong branch entirely. Commission > 0 is only
        reachable from the exchange-traded branch.
        """
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('110022')
        assert sched.asset_type == 'bond'
        r = compute_fee(sched, 10000, 'sell', holding_days=3)
        assert r['commission'] > 0, 'bond must be priced on the exchange-traded branch'
        assert r['stamp_tax'] == 0.0, 'bonds are stamp-tax exempt'
        assert r['fee_rate'] < 0.001, 'bond must not pay a fund redemption fee'

    def test_fund_redemption_is_tiered(self):
        from tofu_trading.trading.fee_book import compute_fee, default_schedule_for

        sched = default_schedule_for('003003')
        assert sched.asset_type == 'fund'
        # Tier semantics: holding strictly fewer than `days` costs `rate`.
        assert compute_fee(sched, 10000, 'sell', 3)['fee_rate'] == pytest.approx(0.015)
        assert compute_fee(sched, 10000, 'sell', 20)['fee_rate'] == pytest.approx(0.005)
        assert compute_fee(sched, 10000, 'sell', 100)['fee_rate'] == pytest.approx(0.0025)
        assert compute_fee(sched, 10000, 'sell', 400)['fee_rate'] == pytest.approx(0.0)
        assert compute_fee(sched, 10000, 'sell', 800)['fee_rate'] == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════
#  5. Confidence — no silent wrong rates
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestConfidence:
    def test_exchange_traded_types_are_exact(self):
        from tofu_trading.trading.fee_book import default_schedule_for

        for symbol in ('600519', '510300', '110022'):
            assert default_schedule_for(symbol).confidence == 'exact'

    def test_open_end_fund_default_is_flagged(self):
        from tofu_trading.trading.fee_book import default_schedule_for

        sched = default_schedule_for('003003')
        assert sched.confidence == 'default'
        assert 'NOT fetched' in sched.source

    def test_estimated_symbols_surfaces_unresolved(self):
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()
        book.prewarm(['600519', '510300', '003003'])
        assert book.estimated_symbols() == ['003003']

    def test_provenance_records_every_symbol(self):
        from tofu_trading.trading.fee_book import FeeBook

        book = FeeBook()
        book.prewarm(['600519', '003003'])
        prov = book.provenance()
        assert set(prov) == {'600519', '003003'}
        assert all(p['source'] for p in prov.values())
