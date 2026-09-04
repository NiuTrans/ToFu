"""tofu_trading/web/handlers/trading_reconcile.py — target/position/action REST.

The reconcile surface. Three groups:

  * ``/reconcile/target``   — the desired portfolio. AI proposes, owner
    approves (owner decision #3): an unapproved row is stored but MUST NOT
    influence the plan, so approval is an explicit endpoint, not a side effect
    of writing the row.
  * ``/reconcile/position`` — where the user actually is, as THEY confirmed
    it, including the in-flight fields (``pending_shares`` / ``settle_date``)
    the third gate reads.
  * ``/reconcile/plan``     — the derived action list, recomputed on every
    call. Persisting the plan is a RECORD of what was advised, never a queue
    to be replayed: the next call recomputes from scratch, so a user who skips
    days can never accumulate stale commands.
  * ``/reconcile/action/<...>/status`` — closes the adoption loop. The old
    ``trading_recommendations.adopted`` column was never written by any code
    path, so the system could not tell whether its advice was followed. This
    endpoint is the one that makes "were the suggestions any good?" answerable
    at all.

Price provenance: intraday fund estimates are NOT obtainable (both fundgz
domains measured dead — see docs/REDESIGN.md §5), so every price carries a
``price_basis`` and the payload carries ``is_estimate``. The UI must render
"估算（基于昨日净值）" rather than implying a live quote.
"""

import asyncio
from datetime import datetime

from flask import jsonify, request

from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import async_parse_body
from tofu_trading.storage import (
    DOMAIN_TRADING,
    async_execute,
    async_fetchall,
    async_fetchone,
)

from tofu_trading.identity import current_user_id
from tofu_trading.reconcile import ReconcileParams, compute_drift, plan_actions

logger = get_logger(__name__)

from tofu_trading.web.v1.reconcile import api_v1_trading_reconcile_bp as trading_reconcile_bp  # noqa: E402

_VALID_STATUS = ('pending', 'done', 'skipped', 'expired')


# ═══════════════════════════════════════════════════════════
#  Target portfolio
# ═══════════════════════════════════════════════════════════

@trading_reconcile_bp.route('/api/v1/trading/reconcile/target', methods=['GET'])
async def reconcile_target_list():
    """List this user's target weights (approved and pending alike).

    Both are returned so the UI can show "AI 提议，待你批准" — but only the
    approved ones reach the planner.
    """
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT * FROM trading_target WHERE user_id=? ORDER BY target_weight DESC',
        (uid,), domain=DOMAIN_TRADING)
    targets = [dict(r) for r in rows]
    approved_sum = sum(t['target_weight'] for t in targets if t.get('approved'))
    return jsonify({
        'targets': targets,
        'approved_weight_sum': round(approved_sum, 2),
        # Weights need not sum to 100 — the remainder is intended cash. Sent
        # explicitly so the UI states it rather than treating it as an error.
        'implied_cash_weight': round(max(0.0, 100.0 - approved_sum), 2),
    })


@trading_reconcile_bp.route('/api/v1/trading/reconcile/target', methods=['POST'])
async def reconcile_target_upsert():
    """Create or update one target weight.

    Writing a target does NOT approve it: a proposal must be ratified through
    the approve endpoint. Defaulting to approved would let an AI proposal move
    real money with no human step, which is exactly what owner decision #3
    rules out.
    """
    uid = current_user_id()
    data = await async_parse_body()
    symbol = (data.get('symbol') or '').strip()
    if not symbol:
        return api_bad_request('symbol required')
    try:
        weight = float(data.get('target_weight') or 0)
    except (TypeError, ValueError):
        return api_bad_request('target_weight must be numeric')
    if weight < 0 or weight > 100:
        return api_bad_request('target_weight must be within 0..100')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    await async_execute(
        'INSERT OR REPLACE INTO trading_target '
        '(user_id, symbol, asset_name, target_weight, rationale, proposed_by, '
        ' approved, valid_from, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (uid, symbol, data.get('asset_name', ''), weight,
         data.get('rationale', ''), data.get('proposed_by', 'ai'),
         0, data.get('valid_from', now[:10]), now),
        domain=DOMAIN_TRADING)
    logger.info('[Reconcile] target upserted user=%s %s -> %.2f%% (unapproved)',
                uid, symbol, weight)
    return api_ok({'symbol': symbol, 'target_weight': weight, 'approved': 0})


@trading_reconcile_bp.route('/api/v1/trading/reconcile/target/<symbol>/approve',
                            methods=['POST'])
async def reconcile_target_approve(symbol):
    """Owner ratifies a proposed target. Only after this does it drive a plan."""
    uid = current_user_id()
    row = await async_fetchone(
        'SELECT * FROM trading_target WHERE user_id=? AND symbol=?',
        (uid, symbol), domain=DOMAIN_TRADING)
    if not row:
        return api_not_found('target not found')
    await async_execute(
        'UPDATE trading_target SET approved=1, updated_at=? '
        'WHERE user_id=? AND symbol=?',
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), uid, symbol),
        domain=DOMAIN_TRADING)
    logger.info('[Reconcile] target approved user=%s %s', uid, symbol)
    return api_ok({'symbol': symbol, 'approved': 1})


@trading_reconcile_bp.route('/api/v1/trading/reconcile/target/<symbol>',
                            methods=['DELETE'])
async def reconcile_target_delete(symbol):
    uid = current_user_id()
    await async_execute(
        'DELETE FROM trading_target WHERE user_id=? AND symbol=?',
        (uid, symbol), domain=DOMAIN_TRADING)
    return api_ok({'symbol': symbol})


# ═══════════════════════════════════════════════════════════
#  Real positions
# ═══════════════════════════════════════════════════════════

@trading_reconcile_bp.route('/api/v1/trading/reconcile/position', methods=['GET'])
async def reconcile_position_list():
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT * FROM trading_position WHERE user_id=? ORDER BY symbol',
        (uid,), domain=DOMAIN_TRADING)
    return jsonify({'positions': [dict(r) for r in rows]})


@trading_reconcile_bp.route('/api/v1/trading/reconcile/position', methods=['POST'])
async def reconcile_position_upsert():
    """Record what the user actually holds, including unsettled shares."""
    uid = current_user_id()
    data = await async_parse_body()
    symbol = (data.get('symbol') or '').strip()
    if not symbol:
        return api_bad_request('symbol required')
    try:
        shares = float(data.get('shares') or 0)
        pending = float(data.get('pending_shares') or 0)
        cost = float(data.get('cost') or 0)
    except (TypeError, ValueError):
        return api_bad_request('shares / pending_shares / cost must be numeric')
    if shares < 0 or pending < 0:
        return api_bad_request('share counts cannot be negative')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    await async_execute(
        'INSERT OR REPLACE INTO trading_position '
        '(user_id, symbol, asset_name, shares, cost, pending_shares, '
        ' settle_date, as_of, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (uid, symbol, data.get('asset_name', ''), shares, cost, pending,
         data.get('settle_date', ''), data.get('as_of', now[:10]), now),
        domain=DOMAIN_TRADING)
    return api_ok({'symbol': symbol, 'shares': shares,
                   'pending_shares': pending})


@trading_reconcile_bp.route('/api/v1/trading/reconcile/position/<symbol>',
                            methods=['DELETE'])
async def reconcile_position_delete(symbol):
    uid = current_user_id()
    await async_execute(
        'DELETE FROM trading_position WHERE user_id=? AND symbol=?',
        (uid, symbol), domain=DOMAIN_TRADING)
    return api_ok({'symbol': symbol})


# ═══════════════════════════════════════════════════════════
#  The plan
# ═══════════════════════════════════════════════════════════

def _resolve_prices(symbols):
    """Latest price per symbol + how fresh it is. Runs off the event loop.

    Returns ``(prices, basis)`` where basis[symbol] is 'close' (a real prior
    close) or 'cost' (no market data at all — fell back to the user's cost).
    Intraday estimates are deliberately absent: both fundgz endpoints are dead
    from this deployment, so claiming a live figure would be a lie.
    """
    prices, basis = {}, {}
    from tofu_trading.trading import get_latest_price
    for sym in symbols:
        try:
            val, as_of = get_latest_price(sym)
        except Exception as e:
            logger.warning('[Reconcile] price lookup failed for %s: %s', sym, e)
            val, as_of = None, ''
        if val:
            prices[sym] = float(val)
            basis[sym] = {'source': 'close', 'as_of': as_of or ''}
        else:
            basis[sym] = {'source': 'missing', 'as_of': ''}
    return prices, basis


@trading_reconcile_bp.route('/api/v1/trading/reconcile/plan', methods=['GET'])
async def reconcile_plan():
    """Recompute the action plan from target vs actual. Stateless.

    Nothing is read from a "today's commands" table because none exists. Two
    calls a week apart with unchanged holdings return the same plan, re-priced;
    a user who acted on half sees only the remainder.

    ``persist=1`` records the plan into trading_action so the adoption loop has
    rows to update. That is a RECORD, not a queue — the next call recomputes
    regardless of what is stored.
    """
    uid = current_user_id()
    today = datetime.now().strftime('%Y-%m-%d')

    trows = await async_fetchall(
        'SELECT * FROM trading_target WHERE user_id=? AND approved=1',
        (uid,), domain=DOMAIN_TRADING)
    prows = await async_fetchall(
        'SELECT * FROM trading_position WHERE user_id=?',
        (uid,), domain=DOMAIN_TRADING)
    cfg = await async_fetchone(
        "SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'",
        (uid,), domain=DOMAIN_TRADING)

    targets = [dict(r) for r in trows]
    positions = [dict(r) for r in prows]
    cash = float(cfg['value']) if cfg else 0.0

    symbols = {t['symbol'] for t in targets} | {p['symbol'] for p in positions}
    prices, basis = await asyncio.to_thread(_resolve_prices, sorted(symbols))

    drifts = compute_drift(targets, positions, prices, cash=cash)
    # Carry settle_date onto the drift rows so the in-flight gate can see it.
    settle_by_sym = {p['symbol']: p.get('settle_date', '') for p in positions}
    for d in drifts:
        d['settle_date'] = settle_by_sym.get(d['symbol'], '')

    params = ReconcileParams(
        deadband_pct=float(request.args.get('deadband', 5.0)),
        min_ticket=float(request.args.get('min_ticket', 1000.0)),
        min_abs_drift=float(request.args.get('min_abs_drift', 500.0)),
    )
    actions, skipped = plan_actions(drifts, params, cash=cash, today=today)

    if request.args.get('persist') in ('1', 'true', 'yes'):
        await _persist_plan(uid, today, actions)

    return jsonify({
        'plan_date': today,
        'actions': actions,
        'skipped': skipped,
        'cash': cash,
        'price_basis': basis,
        # Intraday NAV is unavailable (docs/REDESIGN.md §5) — the UI must say
        # so rather than presenting these as live prices.
        'is_estimate': True,
        'estimate_note': '估算（基于上一交易日收盘/净值），非实时',
        'gates': {
            'deadband_pct': params.deadband_pct,
            'min_ticket': params.min_ticket,
            'min_abs_drift': params.min_abs_drift,
        },
    })


async def _persist_plan(uid, plan_date, actions):
    """Upsert today's advised actions, preserving any already-acted status.

    Recomputation must never resurrect a completed action: if the user already
    marked one done, re-running the plan keeps that verdict instead of
    resetting it to pending.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for a in actions:
        existing = await async_fetchone(
            'SELECT status FROM trading_action '
            'WHERE user_id=? AND plan_date=? AND symbol=?',
            (uid, plan_date, a['symbol']), domain=DOMAIN_TRADING)
        if existing and existing['status'] != 'pending':
            continue
        await async_execute(
            'INSERT OR REPLACE INTO trading_action '
            '(user_id, plan_date, symbol, side, shares, amount, price, '
            ' drift_pct, reason, status, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (uid, plan_date, a['symbol'], a['side'], a['shares'], a['amount'],
             a['price'], a.get('drift_pct', 0), a.get('reason', ''),
             'pending', now),
            domain=DOMAIN_TRADING)


# ═══════════════════════════════════════════════════════════
#  Adoption loop
# ═══════════════════════════════════════════════════════════

@trading_reconcile_bp.route('/api/v1/trading/reconcile/action', methods=['GET'])
async def reconcile_action_list():
    uid = current_user_id()
    status = request.args.get('status', '')
    if status:
        rows = await async_fetchall(
            'SELECT * FROM trading_action WHERE user_id=? AND status=? '
            'ORDER BY plan_date DESC, symbol', (uid, status),
            domain=DOMAIN_TRADING)
    else:
        rows = await async_fetchall(
            'SELECT * FROM trading_action WHERE user_id=? '
            'ORDER BY plan_date DESC, symbol LIMIT 200', (uid,),
            domain=DOMAIN_TRADING)
    return jsonify({'actions': [dict(r) for r in rows]})


@trading_reconcile_bp.route(
    '/api/v1/trading/reconcile/action/<plan_date>/<symbol>/status',
    methods=['POST'])
async def reconcile_action_set_status(plan_date, symbol):
    """★ Close the adoption loop: record what the user actually did.

    This is the endpoint whose absence made the old module unable to answer
    "was the advice any good?" — the legacy ``adopted`` column existed but no
    code ever wrote it. Recording ``actual_price``/``actual_shares`` (not just
    a boolean) is what later allows advice quality to be measured against what
    was really executed rather than what was proposed.
    """
    uid = current_user_id()
    data = await async_parse_body()
    status = (data.get('status') or '').strip()
    if status not in _VALID_STATUS:
        return api_bad_request(f'status must be one of {_VALID_STATUS}')

    row = await async_fetchone(
        'SELECT * FROM trading_action '
        'WHERE user_id=? AND plan_date=? AND symbol=?',
        (uid, plan_date, symbol), domain=DOMAIN_TRADING)
    if not row:
        return api_not_found('action not found')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        actual_price = float(data.get('actual_price') or 0)
        actual_shares = float(data.get('actual_shares') or 0)
    except (TypeError, ValueError):
        return api_bad_request('actual_price / actual_shares must be numeric')

    await async_execute(
        'UPDATE trading_action SET status=?, acted_at=?, actual_price=?, '
        'actual_shares=? WHERE user_id=? AND plan_date=? AND symbol=?',
        (status, now if status != 'pending' else '', actual_price,
         actual_shares, uid, plan_date, symbol),
        domain=DOMAIN_TRADING)
    logger.info('[Reconcile] action %s/%s -> %s (user=%s)',
                plan_date, symbol, status, uid)
    return api_ok({'plan_date': plan_date, 'symbol': symbol, 'status': status})


@trading_reconcile_bp.route('/api/v1/trading/reconcile/adoption', methods=['GET'])
async def reconcile_adoption_stats():
    """How much of the advice actually gets followed.

    Only meaningful because the status endpoint above writes for real. Reported
    as counts, not a quality score: whether the advice was *good* needs price
    outcomes over time, which this deliberately does not pretend to know yet.
    """
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT status, COUNT(*) AS n FROM trading_action WHERE user_id=? '
        'GROUP BY status', (uid,), domain=DOMAIN_TRADING)
    counts = {r['status']: r['n'] for r in rows}
    total = sum(counts.values())
    done = counts.get('done', 0)
    return jsonify({
        'counts': counts,
        'total': total,
        'follow_through_rate': round(done / total * 100, 1) if total else None,
    })
