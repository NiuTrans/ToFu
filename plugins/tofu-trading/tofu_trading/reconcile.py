"""tofu_trading/reconcile.py — target-vs-actual drift reconciliation.

Why this module replaces the old "daily briefing"
-------------------------------------------------
The abandoned module stored *commands*: ``trading_daily_briefing`` keyed on
``date``, one row per day, meaning "here is what to do today". That model breaks
the moment the user does not act:

  * skip today  -> yesterday's command rots in the table, still saying "buy"
  * skip 3 days -> three mutually contradictory commands, none re-priced
  * follow half -> nothing notices, so the next command double-counts

This module stores a *target* instead and computes the difference on demand.
The action list is derived from "where you actually are right now" plus today's
prices, so it is correct whenever you look at it and there is no command
backlog to go stale. Missing a day is a no-op by construction, not by a
catch-up mechanism.

The three gates (owner-mandated)
--------------------------------
A raw weight difference is NOT a tradeable instruction. Three filters stand
between drift and an action, and an action must clear all three:

  1. DEADBAND    — weights wander every day; acting on a 0.3% wobble churns the
                   portfolio and the fees eat the return. Require the drift to
                   exceed a relative threshold AND a minimum absolute amount.
  2. MIN TICKET  — a 87-yuan order is not worth a commission (A-share
                   commission has a 5-yuan floor, so tiny orders pay a huge
                   effective rate).
  3. NO IN-FLIGHT— A-share buys settle T+1 and fund subscriptions confirm T+1
                   or later. Shares you bought today cannot be sold today, and
                   re-recommending a symbol that already has unsettled shares
                   double-counts the cash.

Lot sizes are applied after the gates: A-share round lots are 100 shares, so an
un-rounded "buy 37 shares" is not an executable instruction. ETFs trade in 100s
too; open-end funds are bought by amount and need no rounding.

Everything in this module is a PURE function over plain dicts — no DB, no
network, no clock. That is deliberate: the gate logic is the part that decides
whether the user loses money to churn, so it must be testable without fixtures.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'ReconcileParams',
    'compute_drift',
    'plan_actions',
    'lot_size_for',
    'round_to_lot',
    'DEFAULT_PARAMS',
]


class ReconcileParams:
    """Tunables for the three gates. Defaults are the owner's stated values."""

    __slots__ = ('deadband_pct', 'min_ticket', 'min_abs_drift', 'cash_buffer')

    def __init__(self, deadband_pct=5.0, min_ticket=1000.0,
                 min_abs_drift=500.0, cash_buffer=0.0):
        """
        Args:
            deadband_pct:  Relative drift (percentage points of portfolio
                           weight) below which no action is proposed.
            min_ticket:    Minimum order amount in yuan.
            min_abs_drift: Absolute drift in yuan below which no action is
                           proposed, regardless of percentage. Guards the case
                           where a tiny portfolio makes 5% a trivial sum.
            cash_buffer:   Cash to hold back from buys.
        """
        self.deadband_pct = float(deadband_pct)
        self.min_ticket = float(min_ticket)
        self.min_abs_drift = float(min_abs_drift)
        self.cash_buffer = float(cash_buffer)


DEFAULT_PARAMS = ReconcileParams()


# ═══════════════════════════════════════════════════════════
#  Lot sizes
# ═══════════════════════════════════════════════════════════

def lot_size_for(code: str) -> int:
    """Round-lot size for a symbol. 1 means "no rounding needed".

    Open-end funds (``classify_asset_code`` -> 'fund') are purchased by amount,
    so shares need not be whole lots. Exchange-traded instruments (stocks,
    ETFs) trade in 100-share lots on the Shanghai and Shenzhen exchanges.
    """
    try:
        # Import the LEAF module, not the package: tofu_trading.trading's
        # __init__ pulls in the whole data layer (lib._pkg_utils, HTTP client,
        # …). Going through it would make a pure arithmetic helper fail
        # whenever any of that is unavailable, and the except branch below
        # would then silently mis-lot every open-end fund as 100.
        from tofu_trading.trading._common import classify_asset_code
        kind = classify_asset_code(code)
    except Exception as e:
        # Unknown classification: assume exchange-traded, which is the
        # RESTRICTIVE choice — proposing a whole lot is always executable,
        # while proposing 37 shares of a stock is not. Logged at warning
        # because a silent fallback here would round fund orders wrongly.
        logger.warning('[Reconcile] classify_asset_code unavailable for %s '
                       '(%s); assuming exchange-traded 100-share lot', code, e)
        return 100
    return 1 if kind == 'fund' else 100


def round_to_lot(shares: float, lot: int) -> float:
    """Round DOWN to a whole lot. Never rounds up.

    Rounding down matters: rounding a buy up spends cash the user may not have,
    and rounding a sell up sells shares they do not own.
    """
    if lot <= 1:
        return round(float(shares), 2)
    return float(int(float(shares) // lot) * lot)


# ═══════════════════════════════════════════════════════════
#  Drift
# ═══════════════════════════════════════════════════════════

def compute_drift(targets, positions, prices, cash=0.0):
    """Compare the target portfolio against actual holdings.

    Args:
        targets:   [{symbol, target_weight}] — weights in percent, need not
                   sum to 100 (the remainder is treated as intended cash).
        positions: [{symbol, shares, pending_shares?, settle_date?}]
        prices:    {symbol: price}
        cash:      Investable cash.

    Returns:
        List of drift dicts, one per symbol appearing in either side::

            {symbol, target_weight, actual_weight, drift_pct, drift_amount,
             price, shares, pending_shares, market_value, total_value}

        ``drift_amount`` is positive when the target wants MORE than is held.
        Symbols with no price are returned with ``price=0`` and zero drift —
        a missing price must never be read as "sell everything".
    """
    pos_by_sym = {}
    for p in positions or []:
        sym = p.get('symbol')
        if sym:
            pos_by_sym[sym] = p

    # Total portfolio value uses settled + pending shares: pending shares are
    # already paid for, so excluding them would understate the portfolio and
    # inflate every remaining weight.
    market_values = {}
    for sym, p in pos_by_sym.items():
        price = float(prices.get(sym) or 0)
        shares = float(p.get('shares') or 0) + float(p.get('pending_shares') or 0)
        market_values[sym] = price * shares

    total_value = sum(market_values.values()) + float(cash or 0)
    if total_value <= 0:
        logger.debug('[Reconcile] total portfolio value is 0 — no drift computable')
        return []

    symbols = set(pos_by_sym) | {t.get('symbol') for t in (targets or []) if t.get('symbol')}
    tgt_by_sym = {t['symbol']: float(t.get('target_weight') or 0)
                  for t in (targets or []) if t.get('symbol')}

    out = []
    for sym in sorted(symbols):
        p = pos_by_sym.get(sym, {})
        price = float(prices.get(sym) or 0)
        shares = float(p.get('shares') or 0)
        pending = float(p.get('pending_shares') or 0)
        mv = market_values.get(sym, 0.0)
        actual_w = mv / total_value * 100
        target_w = tgt_by_sym.get(sym, 0.0)

        if price <= 0:
            # No price: report the position but claim zero drift. Acting on an
            # unknown price is how a data outage turns into a liquidation.
            out.append({
                'symbol': sym, 'target_weight': target_w,
                'actual_weight': actual_w, 'drift_pct': 0.0,
                'drift_amount': 0.0, 'price': 0.0, 'shares': shares,
                'pending_shares': pending, 'market_value': mv,
                'total_value': total_value, 'price_missing': True,
            })
            continue

        drift_pct = target_w - actual_w
        out.append({
            'symbol': sym, 'target_weight': target_w,
            'actual_weight': actual_w, 'drift_pct': drift_pct,
            'drift_amount': drift_pct / 100 * total_value,
            'price': price, 'shares': shares, 'pending_shares': pending,
            'market_value': mv, 'total_value': total_value,
            'price_missing': False,
        })
    return out


# ═══════════════════════════════════════════════════════════
#  Gates -> actions
# ═══════════════════════════════════════════════════════════

def plan_actions(drifts, params=None, cash=0.0, today=None):
    """Turn drift into an executable action list, applying the three gates.

    Args:
        drifts: Output of :func:`compute_drift`.
        params: :class:`ReconcileParams`; defaults to DEFAULT_PARAMS.
        cash:   Investable cash, used to cap buys.
        today:  ``'YYYY-MM-DD'``; a position whose ``settle_date`` is on or
                after this is considered in-flight. Required for the in-flight
                gate to do anything — pass it explicitly rather than reading a
                clock here, so the gate is testable.

    Returns:
        ``(actions, skipped)``. Each action::

            {symbol, side, shares, amount, reason, lot}

        Each skipped entry carries ``gate`` naming which gate rejected it, so
        the UI can explain "why is there nothing to do today" instead of
        showing an empty screen.
    """
    p = params or DEFAULT_PARAMS
    actions, skipped = [], []
    budget = max(0.0, float(cash or 0) - p.cash_buffer)

    # Sell before buy: sells free cash that buys can then use, and a plan that
    # buys first can propose more than the user can fund.
    ordered = sorted(drifts or [], key=lambda d: d.get('drift_amount', 0))

    for d in ordered:
        sym = d['symbol']

        if d.get('price_missing'):
            skipped.append({**d, 'gate': 'price_missing',
                            'note': '无可用价格，跳过（不猜价）'})
            continue

        # ── Gate 3: no in-flight shares ──
        # Checked FIRST: an unsettled position makes the symbol untradeable
        # regardless of how large the drift is, so the other gates are moot.
        pending = float(d.get('pending_shares') or 0)
        if pending > 0:
            skipped.append({**d, 'gate': 'in_flight',
                            'note': f'有 {pending:g} 份在途未确认，等待交收'})
            continue
        settle = d.get('settle_date') or ''
        if settle and today and settle >= today:
            skipped.append({**d, 'gate': 'in_flight',
                            'note': f'交收日 {settle} 未到'})
            continue

        drift_pct = float(d.get('drift_pct') or 0)
        drift_amt = float(d.get('drift_amount') or 0)

        # ── Gate 1: deadband ──
        if abs(drift_pct) < p.deadband_pct or abs(drift_amt) < p.min_abs_drift:
            skipped.append({**d, 'gate': 'deadband',
                            'note': (f'偏离 {drift_pct:+.2f}% / ¥{drift_amt:+.0f}，'
                                     f'未超过免交易带 {p.deadband_pct:g}%'
                                     f' / ¥{p.min_abs_drift:g}')})
            continue

        side = 'buy' if drift_amt > 0 else 'sell'
        want_amount = abs(drift_amt)
        if side == 'buy':
            want_amount = min(want_amount, budget)

        # ── Gate 2: minimum ticket ──
        if want_amount < p.min_ticket:
            skipped.append({**d, 'gate': 'min_ticket',
                            'note': (f'金额 ¥{want_amount:.0f} 低于最小票 '
                                     f'¥{p.min_ticket:g}')})
            continue

        # ── Lot rounding (after the gates, so it cannot resurrect a
        #     gate-rejected action) ──
        price = float(d['price'])
        lot = lot_size_for(sym)
        raw_shares = want_amount / price if price > 0 else 0
        if side == 'sell':
            # Never propose selling more than is actually held.
            raw_shares = min(raw_shares, float(d.get('shares') or 0))
        shares = round_to_lot(raw_shares, lot)

        if shares <= 0:
            skipped.append({**d, 'gate': 'below_one_lot',
                            'note': (f'不足一手（{lot} 份 ≈ '
                                     f'¥{lot * price:.0f}）')})
            continue

        amount = round(shares * price, 2)
        # Re-check the ticket AFTER rounding: rounding down can drop the amount
        # back under the floor, and shipping it anyway would defeat the gate.
        if amount < p.min_ticket:
            skipped.append({**d, 'gate': 'min_ticket_after_lot',
                            'note': (f'取整到 {shares:g} 份后金额 ¥{amount:.0f} '
                                     f'低于最小票 ¥{p.min_ticket:g}')})
            continue

        if side == 'buy':
            budget -= amount

        actions.append({
            'symbol': sym, 'side': side, 'shares': shares, 'amount': amount,
            'lot': lot, 'price': price,
            'target_weight': d.get('target_weight'),
            'actual_weight': d.get('actual_weight'),
            'drift_pct': drift_pct,
            'reason': (f'目标 {d.get("target_weight", 0):.1f}% vs 实际 '
                       f'{d.get("actual_weight", 0):.1f}%'
                       f'（偏离 {drift_pct:+.2f}%）'),
        })

    return actions, skipped
