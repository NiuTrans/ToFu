"""routes/trading_autopilot.py — Autopilot recommendation management & outcome tracking.

Decision analysis endpoints have been consolidated into ``routes/trading_brain.py``
(the Brain is the unified decision center). This module retains:

  - GET  /api/trading/autopilot/state       → delegates to brain state
  - POST /api/trading/autopilot/toggle      → toggles autopilot scheduler
  - POST /api/trading/autopilot/run         → delegates to brain analyze
  - POST /api/trading/autopilot/stream      → delegates to brain stream
  - GET  /api/trading/autopilot/cycles      → delegates to brain cycles
  - GET  /api/trading/autopilot/cycles/<id> → delegates to brain cycle detail
  - GET  /api/trading/autopilot/cycles/<id>/recommendations — cycle recs
  - GET  /api/trading/autopilot/recommendations — list recommendations
  - POST /api/trading/autopilot/recommendations/<id>/accept — accept rec
  - POST /api/trading/autopilot/recommendations/<id>/reject — reject rec
  - POST /api/trading/autopilot/evaluate    — evaluate outcomes (alias: /track)
  - POST /api/trading/autopilot/kpi         — KPI evaluation
  - POST /api/trading/autopilot/strategy-evolution — strategy evolution
"""

import threading
import time
from datetime import datetime

from flask import jsonify, request

from tofu_trading.storage import (
    DOMAIN_TRADING,
    async_execute,
    async_fetchall,
    async_fetchone,
)
from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import async_parse_body

from tofu_trading.identity import current_user_id

logger = get_logger(__name__)
from tofu_trading.web.v1.autopilot import api_v1_trading_autopilot_bp as trading_autopilot_bp  # noqa: E402
# (alias kept for back-compat with `from tofu_trading.web.handlers.trading_autopilot import trading_autopilot_bp` callers)


# ═══════════════════════════════════════════════════════════
#  State & Analysis — delegate to Brain
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/state', methods=['GET'])
async def autopilot_state():
    """Delegate to brain state for unified view."""
    from .trading_brain import brain_state
    return await brain_state()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/toggle', methods=['POST'])
async def autopilot_toggle():
    """Toggle autopilot scheduler — syncs with brain auto toggle."""
    from .trading_brain import brain_auto_toggle
    return await brain_auto_toggle()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/run', methods=['POST'])
async def autopilot_run_now():
    """Delegate to brain analyze."""
    from .trading_brain import brain_analyze
    return await brain_analyze()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/stream', methods=['POST'])
async def autopilot_stream():
    """Delegate to brain stream."""
    from .trading_brain import brain_stream
    return await brain_stream()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles', methods=['GET'])
async def autopilot_cycles_list():
    """Delegate to brain cycles."""
    from .trading_brain import brain_cycles
    return await brain_cycles()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles/<cycle_id>', methods=['GET'])
async def autopilot_cycle_detail(cycle_id):
    """Delegate to brain cycle detail."""
    from .trading_brain import brain_cycle_detail
    return await brain_cycle_detail(cycle_id)


# ═══════════════════════════════════════════════════════════
#  Recommendations — unique to autopilot (accept/reject workflow)
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles/<cycle_id>/recommendations', methods=['GET'])
async def autopilot_cycle_recommendations(cycle_id):
    """Return recommendations for a specific cycle."""
    rows = await async_fetchall(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? AND user_id=? ORDER BY confidence DESC',
        (cycle_id, current_user_id()), domain=DOMAIN_TRADING
    )
    return jsonify({'recommendations': [dict(r) for r in rows]})


@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations', methods=['GET'])
async def autopilot_recommendations():
    uid = current_user_id()
    status = request.args.get('status', '')
    if status:
        rows = await async_fetchall(
            'SELECT * FROM trading_autopilot_recommendations WHERE status=? AND user_id=? ORDER BY created_at DESC LIMIT 100',
            (status, uid), domain=DOMAIN_TRADING
        )
    else:
        rows = await async_fetchall(
            'SELECT * FROM trading_autopilot_recommendations WHERE user_id=? ORDER BY created_at DESC LIMIT 100',
            (uid,), domain=DOMAIN_TRADING
        )
    return jsonify({'recommendations': [dict(r) for r in rows]})


@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations/<int:rid>/accept', methods=['POST'])
async def autopilot_accept_recommendation(rid):
    uid = current_user_id()
    rec = await async_fetchone(
        'SELECT * FROM trading_autopilot_recommendations WHERE id=? AND user_id=?',
        (rid, uid), domain=DOMAIN_TRADING)
    if not rec:
        return api_not_found('Recommendation not found')
    rec = dict(rec)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    batch_id = f"autopilot_{now.replace(' ', '_').replace(':', '')}"
    await async_execute('''
        INSERT INTO trading_trade_queue (user_id, batch_id, symbol, asset_name, action, amount, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (uid, batch_id, rec['symbol'], rec['asset_name'], rec['action'],
          rec['amount'], f"[Autopilot] {rec['reason']}", 'pending', now), domain=DOMAIN_TRADING)
    await async_execute('UPDATE trading_autopilot_recommendations SET status=? WHERE id=? AND user_id=?',
                        ('accepted', rid, uid), domain=DOMAIN_TRADING)
    return api_ok()
@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations/<int:rid>/reject', methods=['POST'])
async def autopilot_reject_recommendation(rid):
    await async_execute('UPDATE trading_autopilot_recommendations SET status=? WHERE id=? AND user_id=?',
                        ('rejected', rid, current_user_id()), domain=DOMAIN_TRADING)
    return api_ok()
# ═══════════════════════════════════════════════════════════
#  Outcome Tracking & KPI — unique analytics endpoints
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/evaluate', methods=['POST'])
@trading_autopilot_bp.route('/api/v1/trading/autopilot/track', methods=['POST'])
async def autopilot_evaluate_outcomes():
    """Evaluate/track recommendation outcomes. Both paths do the same thing."""
    import asyncio
    from tofu_trading.trading_autopilot import track_recommendation_outcomes
    data = await async_parse_body()
    days = data.get('days_after', 7)
    # Resolve owner in the request thread; _run() has no request context.
    uid = current_user_id()

    def _run():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            return track_recommendation_outcomes(db, days_after=days, uid=uid)
        finally:
            _pool_put(db)

    outcomes = await asyncio.to_thread(_run)
    return api_ok({'outcomes': outcomes, 'count': len(outcomes)})
@trading_autopilot_bp.route('/api/v1/trading/autopilot/kpi', methods=['POST'])
@trading_autopilot_bp.route('/api/v1/trading/autopilot/kpi-evaluate', methods=['POST'])
async def autopilot_kpi_evaluate():
    import asyncio
    from tofu_trading.trading_autopilot import pre_backtest_evaluate
    data = await async_parse_body()
    codes = data.get('symbols', [])
    lookback = data.get('lookback_days', 90)
    uid = current_user_id()
    if not codes:
        holdings = await async_fetchall('SELECT symbol FROM trading_holdings WHERE user_id=?',
                                        (uid,), domain=DOMAIN_TRADING)
        codes = [h['symbol'] for h in holdings]
    if not codes:
        return api_bad_request('No asset codes to evaluate')

    def _run():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            return pre_backtest_evaluate(db, codes, lookback_days=lookback)
        finally:
            _pool_put(db)

    kpi = await asyncio.to_thread(_run)
    return api_ok({'kpi': kpi})
@trading_autopilot_bp.route('/api/v1/trading/autopilot/strategy-evolution', methods=['POST'])
async def autopilot_strategy_evolution():
    import asyncio
    from tofu_trading.trading_autopilot import evolve_strategies
    uid = current_user_id()

    def _run():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            return evolve_strategies(db)
        finally:
            _pool_put(db)

    ctx, items = await asyncio.to_thread(_run)
    return api_ok({'evolution_context': ctx, 'items': items})
# ═══════════════════════════════════════════════════════════
#  Background worker (started from server.py)
# ═══════════════════════════════════════════════════════════

def start_autopilot_worker():
    """Start the autopilot background scheduler thread."""
    def _worker():
        from tofu_trading.gate import wait_until_enabled
        from tofu_trading.trading_autopilot import autopilot_scheduler_tick
        time.sleep(60)
        while True:
            # run_autopilot_cycle drives smart_chat and can place simulated
            # trades, so it must not tick while the feature is switched off.
            wait_until_enabled(time.sleep, 60)
            try:
                autopilot_scheduler_tick(db_path=None)  # uses PG via get_thread_db
            except Exception as e:
                logger.error('[Autopilot Worker] %s', e, exc_info=True)
            time.sleep(300)

    t = threading.Thread(
        target=_worker, daemon=True, name='trading-autopilot-scheduler')
    t.start()
    return t
