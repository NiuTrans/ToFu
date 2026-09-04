"""tofu_trading/trading/fee_book.py — deterministic, zero-network fee resolution.

WHY THIS EXISTS
---------------
``info.estimate_trade_fee`` already implements the CORRECT per-asset-type fee
maths (A-share commission + stamp tax, ETF commission-only, fund tiered
redemption). But it is **not usable inside a backtest loop**: on the ``fund``
path it scrapes ``fundf10.eastmoney.com`` for the subscription rate. Measured on
this host: ``110022`` took **1317.8 ms** for a single call, while ``600519`` /
``510300`` took 0.1 ms / 0.0 ms.

Wiring that directly into a decision loop would do two unacceptable things:

  1. **Destroy determinism.** The same historical inputs would produce different
     results depending on whether the network answered — and a backtest whose
     output depends on the weather is not evidence of anything.
  2. Add seconds-to-minutes of latency per run (hundreds of decision points).

So the seam is split in two:

  * :func:`compute_fee` — a PURE function of ``(schedule, amount, action,
    holding_days)``. No I/O of any kind. This is what the engines call, once per
    simulated trade.
  * :func:`FeeBook.prewarm` — the ONLY place network access may happen, called
    once before a run. Failures here degrade to a classifier-derived default and
    are recorded as such (never silently).

THE SILENT-WRONG-RATE RULE
--------------------------
A misclassified code must never quietly return a plausible-looking rate.
Measured: ``110022`` (an exchange-traded bond) classifies as ``bond``, falls
through to the fund path, and comes back with a **1.5% redemption fee** — a 15x
error delivered with no warning. Every schedule therefore carries
:attr:`FeeSchedule.confidence` and :attr:`FeeSchedule.source`; anything not
``confidence='exact'`` is surfaced by :func:`FeeBook.estimated_symbols` so the
caller can label the run's numbers as estimates.

MINIMUM COMMISSION IS WHY RATES ARE NOT CONSTANTS
-------------------------------------------------
The ¥5 commission floor makes the effective rate a function of trade size, not
a fixed number. Measured on ``600519`` buys: ¥500 → 1.000%, ¥1,000 → 0.500%,
¥3,000 → 0.167%, ¥10,000 → 0.050%, ¥50,000 → 0.025%. Any acceptance target
expressed as a flat percentage is therefore wrong by construction; derive it
from :func:`compute_fee` at the position size actually used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger

from ._common import classify_asset_code

logger = get_logger(__name__)

__all__ = [
    'FeeSchedule',
    'FeeBook',
    'compute_fee',
    'default_schedule_for',
    'DEFAULT_SCHEDULES',
]


# ═══════════════════════════════════════════════════════════
#  Schedule
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FeeSchedule:
    """An immutable, fully-resolved fee schedule for ONE symbol.

    Frozen because the engines hold these across a whole run: a schedule that
    could be mutated mid-run would reintroduce exactly the non-determinism this
    module exists to remove.

    Attributes:
        symbol:          6-digit asset code.
        asset_type:      'stock' | 'etf' | 'bond' | 'fund'.
        commission_rate: Broker commission, both sides (stock/etf/bond).
        min_commission:  Commission floor in CNY (0 disables the floor).
        stamp_tax_rate:  Stamp tax, SELL side only (stocks only since 2008).
        transfer_fee_rate: Transfer fee, both sides (Shanghai; approximated).
        buy_fee_rate:    Subscription rate — fund path only.
        sell_fee_rules:  Tiered redemption, fund path only. Each entry is
                         ``{'days': int, 'rate': float}``, meaning "holding
                         strictly fewer than ``days`` costs ``rate``".
        confidence:      'exact'    — arithmetic is fully determined
                                      (exchange-traded: published tax + the
                                      configured commission).
                         'fetched'  — scraped from the vendor for this symbol.
                         'default'  — class-level fallback; the real number for
                                      THIS symbol was never obtained.
        source:          Human-readable provenance, for logs and the run record.
    """

    symbol: str
    asset_type: str
    commission_rate: float = 0.0
    min_commission: float = 0.0
    stamp_tax_rate: float = 0.0
    transfer_fee_rate: float = 0.0
    buy_fee_rate: float = 0.0
    sell_fee_rules: tuple[tuple[int, float], ...] = ()
    confidence: str = 'default'
    source: str = ''

    def to_dict(self) -> dict[str, Any]:
        """Serialise for persistence / run records."""
        return {
            'symbol': self.symbol,
            'asset_type': self.asset_type,
            'commission_rate': self.commission_rate,
            'min_commission': self.min_commission,
            'stamp_tax_rate': self.stamp_tax_rate,
            'transfer_fee_rate': self.transfer_fee_rate,
            'buy_fee_rate': self.buy_fee_rate,
            'sell_fee_rules': [{'days': d, 'rate': r} for d, r in self.sell_fee_rules],
            'confidence': self.confidence,
            'source': self.source,
        }


# ── Class-level defaults ────────────────────────────────────
#
# These mirror info.fetch_trading_fee_info's non-network branches. They are the
# published market structure, not guesses:
#   - commission ~万2.5 with a ¥5 floor (the broker-negotiable part)
#   - stamp tax 0.05%, SELL side, STOCKS ONLY (ETFs and bonds are exempt)
#   - transfer fee 0.001% (Shanghai; applied to all here, as info.py does)
#
# The fund entry is the only one that is a genuine placeholder: real
# subscription rates vary per product and can only be fetched. It is therefore
# the only default carrying confidence='default'.
DEFAULT_SCHEDULES: dict[str, dict[str, Any]] = {
    'stock': {
        'commission_rate': 0.00025,
        'min_commission': 5.0,
        'stamp_tax_rate': 0.0005,
        'transfer_fee_rate': 0.00001,
        'confidence': 'exact',
        'source': 'A-share published structure: commission 0.025% (min ¥5) + stamp tax 0.05% sell-side',
    },
    'etf': {
        'commission_rate': 0.00025,
        'min_commission': 0.1,
        'stamp_tax_rate': 0.0,
        'transfer_fee_rate': 0.0,
        'confidence': 'exact',
        'source': 'ETF published structure: commission 0.025%, no stamp tax',
    },
    'bond': {
        # Exchange-traded bonds: commission only, no stamp tax. Critically NOT
        # the fund path — routing a bond there returned a 1.5% redemption fee.
        'commission_rate': 0.00025,
        'min_commission': 0.1,
        'stamp_tax_rate': 0.0,
        'transfer_fee_rate': 0.0,
        'confidence': 'exact',
        'source': 'Exchange-traded bond: commission only, no stamp tax',
    },
    'fund': {
        'buy_fee_rate': 0.0015,
        'sell_fee_rules': ((7, 0.015), (30, 0.005), (365, 0.0025), (730, 0.0)),
        'confidence': 'default',
        'source': 'open-end fund class default (per-product rate NOT fetched)',
    },
}


def default_schedule_for(symbol: str) -> FeeSchedule:
    """Build the class-level default schedule for ``symbol``. Never touches I/O.

    Args:
        symbol: 6-digit asset code.

    Returns:
        A :class:`FeeSchedule`. Exchange-traded types come back
        ``confidence='exact'``; open-end funds come back ``'default'`` because
        the per-product subscription rate genuinely requires a fetch.
    """
    asset_type = classify_asset_code(symbol)
    spec = dict(DEFAULT_SCHEDULES.get(asset_type, DEFAULT_SCHEDULES['fund']))
    return FeeSchedule(
        symbol=symbol,
        asset_type=asset_type,
        commission_rate=spec.get('commission_rate', 0.0),
        min_commission=spec.get('min_commission', 0.0),
        stamp_tax_rate=spec.get('stamp_tax_rate', 0.0),
        transfer_fee_rate=spec.get('transfer_fee_rate', 0.0),
        buy_fee_rate=spec.get('buy_fee_rate', 0.0),
        sell_fee_rules=tuple(spec.get('sell_fee_rules', ())),
        confidence=spec.get('confidence', 'default'),
        source=spec.get('source', ''),
    )


# ═══════════════════════════════════════════════════════════
#  The pure computation
# ═══════════════════════════════════════════════════════════

def compute_fee(
    schedule: FeeSchedule,
    amount: float,
    action: str = 'buy',
    holding_days: int = 0,
) -> dict[str, Any]:
    """Compute the fee for one trade. PURE — no I/O, no clock, no globals.

    Determinism is the contract: identical arguments MUST produce a
    bit-identical result, because backtest reproducibility depends on it.

    Args:
        schedule:     Resolved :class:`FeeSchedule` for the symbol.
        amount:       Gross trade value in CNY (before fees).
        action:       ``'buy'`` or ``'sell'``.
        holding_days: Days held — only consulted on the fund sell path.

    Returns:
        Dict with ``fee_amount`` / ``fee_rate`` (effective, = fee/amount) /
        ``net_amount`` plus the per-component breakdown and the schedule's
        ``confidence``.
    """
    if amount <= 0:
        return {
            'fee_amount': 0.0, 'fee_rate': 0.0, 'net_amount': 0.0,
            'commission': 0.0, 'stamp_tax': 0.0, 'transfer_fee': 0.0,
            'asset_type': schedule.asset_type, 'confidence': schedule.confidence,
        }

    is_sell = (action == 'sell')

    if schedule.asset_type in ('stock', 'etf', 'bond'):
        # Exchange-traded: commission (with floor) + sell-side stamp tax.
        # The floor is why the effective rate is size-dependent.
        commission = max(amount * schedule.commission_rate, schedule.min_commission)
        stamp_tax = amount * schedule.stamp_tax_rate if is_sell else 0.0
        transfer_fee = amount * schedule.transfer_fee_rate
        fee = commission + stamp_tax + transfer_fee
    else:
        # Open-end fund: subscription on buy, tiered redemption on sell.
        commission = stamp_tax = transfer_fee = 0.0
        if is_sell:
            rate = 0.0
            for days, tier_rate in sorted(schedule.sell_fee_rules):
                if holding_days < days:
                    rate = tier_rate
                    break
            fee = amount * rate
        else:
            fee = amount * schedule.buy_fee_rate

    return {
        'fee_amount': fee,
        'fee_rate': fee / amount,
        'net_amount': amount - fee,
        'commission': commission,
        'stamp_tax': stamp_tax,
        'transfer_fee': transfer_fee,
        'asset_type': schedule.asset_type,
        'confidence': schedule.confidence,
    }


# ═══════════════════════════════════════════════════════════
#  The book
# ═══════════════════════════════════════════════════════════

class FeeBook:
    """Per-run schedule store. Resolution happens ONCE, up front.

    Lifecycle, and the reason for it:

      1. :meth:`prewarm` — the ONLY method permitted to do I/O. Call it before
         the decision loop starts.
      2. :meth:`schedule_for` / :meth:`fee_for` — pure lookups + pure maths.
         Safe to call from inside a loop; a symbol first seen mid-run resolves
         to its class-level default rather than reaching for the network.

    ``allow_network`` defaults to **False** so that the dangerous behaviour is
    opt-in: a caller that forgets to think about it gets the deterministic path.
    """

    def __init__(self, *, allow_network: bool = False, zero_cost: bool = False):
        self._schedules: dict[str, FeeSchedule] = {}
        self._allow_network = allow_network
        self._zero_cost = zero_cost

    @property
    def zero_cost(self) -> bool:
        """True when every fee is forced to zero (transaction-cost-impact studies).

        This exists so a cost-free control run is expressed ONCE, here, rather
        than by zeroing individual rate keys at each call site. Zeroing keys was
        how the old config did it, and it silently missed any fee component the
        caller forgot to list.
        """
        return self._zero_cost

    # ── Resolution (I/O allowed here and nowhere else) ──

    def prewarm(self, symbols, db=None, client=None) -> dict[str, FeeSchedule]:
        """Resolve schedules for ``symbols``. The only I/O entry point.

        Resolution order per symbol, first hit wins:
          1. ``trading_fee_rules`` row (if ``db`` given) → ``confidence='fetched'``
          2. Vendor fetch (only when ``allow_network=True`` AND the class default
             is not already exact — i.e. open-end funds only)
          3. Class-level default from :func:`default_schedule_for`

        Every failure is logged with the symbol and the fallback taken; nothing
        degrades silently.

        Args:
            symbols: Iterable of 6-digit codes.
            db:      Optional DB connection for the ``trading_fee_rules`` cache.
            client:  Optional ``TradingClient`` for the vendor fetch.

        Returns:
            The resolved ``{symbol: FeeSchedule}`` for the requested symbols.
        """
        resolved: dict[str, FeeSchedule] = {}
        for symbol in symbols:
            if not symbol or symbol in self._schedules:
                if symbol in self._schedules:
                    resolved[symbol] = self._schedules[symbol]
                continue

            sched = None
            if db is not None:
                sched = self._from_db(db, symbol)

            if sched is None:
                fallback = default_schedule_for(symbol)
                # Only the fund path has anything a fetch could improve; for
                # exchange-traded types the default IS the published structure.
                if self._allow_network and fallback.confidence != 'exact':
                    sched = self._from_network(symbol, client=client)
                if sched is None:
                    sched = fallback
                    if fallback.confidence != 'exact':
                        logger.info(
                            '[FeeBook] %s: using class default (%s) — per-symbol rate not resolved',
                            symbol, fallback.asset_type)

            self._schedules[symbol] = sched
            resolved[symbol] = sched

        logger.info('[FeeBook] Prewarmed %d symbol(s); %d estimated',
                    len(resolved), sum(1 for s in resolved.values() if s.confidence != 'exact'))
        return resolved

    def _from_db(self, db, symbol: str) -> FeeSchedule | None:
        """Load a cached schedule from ``trading_fee_rules``. Returns None on miss."""
        try:
            row = db.execute(
                'SELECT * FROM trading_fee_rules WHERE symbol=?', (symbol,)
            ).fetchone()
        except Exception as e:
            logger.warning('[FeeBook] trading_fee_rules lookup failed for %s: %s', symbol, e)
            return None
        if not row:
            return None

        row = dict(row)
        try:
            rules = json.loads(row.get('sell_fee_rules') or '[]')
            sell_rules = tuple(sorted((int(r['days']), float(r['rate'])) for r in rules))
        except Exception as e:
            logger.warning('[FeeBook] %s: malformed sell_fee_rules in DB (%s) — falling back', symbol, e)
            return None

        base = default_schedule_for(symbol)
        return FeeSchedule(
            symbol=symbol,
            asset_type=base.asset_type,
            commission_rate=base.commission_rate,
            min_commission=base.min_commission,
            stamp_tax_rate=base.stamp_tax_rate,
            transfer_fee_rate=base.transfer_fee_rate,
            buy_fee_rate=float(row.get('buy_fee_rate') or base.buy_fee_rate),
            sell_fee_rules=sell_rules or base.sell_fee_rules,
            confidence='exact' if base.confidence == 'exact' else 'fetched',
            source=f"trading_fee_rules ({row.get('data_source') or 'db'})",
        )

    def _from_network(self, symbol: str, client=None) -> FeeSchedule | None:
        """Fetch a per-product schedule from the vendor. Returns None on failure."""
        try:
            from .info import fetch_trading_fee_info
            raw = fetch_trading_fee_info(symbol, client=client)
        except Exception as e:
            logger.warning('[FeeBook] %s: vendor fee fetch failed (%s) — will use default',
                           symbol, e)
            return None

        base = default_schedule_for(symbol)
        try:
            rules = tuple(sorted((int(r['days']), float(r['rate']))
                                 for r in raw.get('sell_fee_rules', [])))
        except Exception as e:
            logger.warning('[FeeBook] %s: malformed vendor sell_fee_rules (%s)', symbol, e)
            rules = base.sell_fee_rules

        return FeeSchedule(
            symbol=symbol,
            asset_type=base.asset_type,
            commission_rate=base.commission_rate,
            min_commission=base.min_commission,
            stamp_tax_rate=base.stamp_tax_rate,
            transfer_fee_rate=base.transfer_fee_rate,
            buy_fee_rate=float(raw.get('buy_fee_rate', base.buy_fee_rate)),
            sell_fee_rules=rules,
            confidence='fetched',
            source='vendor fetch (prewarm)',
        )

    # ── Lookup + maths (pure, loop-safe) ──

    def schedule_for(self, symbol: str) -> FeeSchedule:
        """Return the schedule for ``symbol``. Never does I/O.

        A symbol not seen by :meth:`prewarm` resolves to its class-level default
        and is memoised, so repeated lookups stay bit-identical within a run.
        """
        sched = self._schedules.get(symbol)
        if sched is None:
            sched = default_schedule_for(symbol)
            self._schedules[symbol] = sched
            logger.debug('[FeeBook] %s not prewarmed — resolved to %s default',
                         symbol, sched.asset_type)
        return sched

    def fee_for(self, symbol: str, amount: float, action: str = 'buy',
                holding_days: int = 0) -> dict[str, Any]:
        """Compute the fee for one trade on ``symbol``. Pure; loop-safe."""
        if self._zero_cost:
            return {
                'fee_amount': 0.0, 'fee_rate': 0.0, 'net_amount': amount,
                'commission': 0.0, 'stamp_tax': 0.0, 'transfer_fee': 0.0,
                'asset_type': self.schedule_for(symbol).asset_type,
                'confidence': 'exact',
            }
        return compute_fee(self.schedule_for(symbol), amount, action, holding_days)

    def estimated_symbols(self) -> list[str]:
        """Symbols whose schedule is NOT exact — results using them are estimates."""
        return sorted(s for s, sched in self._schedules.items()
                      if sched.confidence not in ('exact', 'fetched'))

    def provenance(self) -> dict[str, dict[str, Any]]:
        """Full per-symbol provenance, for embedding in a run record."""
        return {s: sched.to_dict() for s, sched in sorted(self._schedules.items())}
