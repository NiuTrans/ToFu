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

from lib.database import DOMAIN_TRADING, get_db
from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import parse_body

logger = get_logger(__name__)
from routes.api_v1.trading.autopilot import api_v1_trading_autopilot_bp as trading_autopilot_bp  # noqa: E402
# (alias kept for back-compat with `from routes.trading_autopilot import trading_autopilot_bp` callers)


# ═══════════════════════════════════════════════════════════
#  State & Analysis — delegate to Brain
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/state', methods=['GET'])
def autopilot_state():
    """Delegate to brain state for unified view."""
    from .trading_brain import brain_state
    return brain_state()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/toggle', methods=['POST'])
def autopilot_toggle():
    """Toggle autopilot scheduler — syncs with brain auto toggle."""
    from .trading_brain import brain_auto_toggle
    return brain_auto_toggle()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/run', methods=['POST'])
def autopilot_run_now():
    """Delegate to brain analyze."""
    from .trading_brain import brain_analyze
    return brain_analyze()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/stream', methods=['POST'])
def autopilot_stream():
    """Delegate to brain stream."""
    from .trading_brain import brain_stream
    return brain_stream()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles', methods=['GET'])
def autopilot_cycles_list():
    """Delegate to brain cycles."""
    from .trading_brain import brain_cycles
    return brain_cycles()


@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles/<cycle_id>', methods=['GET'])
def autopilot_cycle_detail(cycle_id):
    """Delegate to brain cycle detail."""
    from .trading_brain import brain_cycle_detail
    return brain_cycle_detail(cycle_id)


# ═══════════════════════════════════════════════════════════
#  Recommendations — unique to autopilot (accept/reject workflow)
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/cycles/<cycle_id>/recommendations', methods=['GET'])
def autopilot_cycle_recommendations(cycle_id):
    """Return recommendations for a specific cycle."""
    db = get_db(DOMAIN_TRADING)
    rows = db.execute(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? ORDER BY confidence DESC',
        (cycle_id,)
    ).fetchall()
    return jsonify({'recommendations': [dict(r) for r in rows]})


@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations', methods=['GET'])
def autopilot_recommendations():
    db = get_db(DOMAIN_TRADING)
    status = request.args.get('status', '')
    if status:
        rows = db.execute(
            'SELECT * FROM trading_autopilot_recommendations WHERE status=? ORDER BY created_at DESC LIMIT 100',
            (status,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM trading_autopilot_recommendations ORDER BY created_at DESC LIMIT 100'
        ).fetchall()
    return jsonify({'recommendations': [dict(r) for r in rows]})


@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations/<int:rid>/accept', methods=['POST'])
def autopilot_accept_recommendation(rid):
    db = get_db(DOMAIN_TRADING)
    rec = db.execute('SELECT * FROM trading_autopilot_recommendations WHERE id=?', (rid,)).fetchone()
    if not rec:
        return api_not_found('Recommendation not found')
    rec = dict(rec)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    batch_id = f"autopilot_{now.replace(' ', '_').replace(':', '')}"
    db.execute('''
        INSERT INTO trading_trade_queue (batch_id, symbol, asset_name, action, amount, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (batch_id, rec['symbol'], rec['asset_name'], rec['action'],
          rec['amount'], f"[Autopilot] {rec['reason']}", 'pending', now))
    db.execute('UPDATE trading_autopilot_recommendations SET status=? WHERE id=?', ('accepted', rid))
    db.commit()
    return api_ok()
@trading_autopilot_bp.route('/api/v1/trading/autopilot/recommendations/<int:rid>/reject', methods=['POST'])
def autopilot_reject_recommendation(rid):
    db = get_db(DOMAIN_TRADING)
    db.execute('UPDATE trading_autopilot_recommendations SET status=? WHERE id=?', ('rejected', rid))
    db.commit()
    return api_ok()
# ═══════════════════════════════════════════════════════════
#  Outcome Tracking & KPI — unique analytics endpoints
# ═══════════════════════════════════════════════════════════

@trading_autopilot_bp.route('/api/v1/trading/autopilot/evaluate', methods=['POST'])
@trading_autopilot_bp.route('/api/v1/trading/autopilot/track', methods=['POST'])
def autopilot_evaluate_outcomes():
    """Evaluate/track recommendation outcomes. Both paths do the same thing."""
    from lib.trading_autopilot import track_recommendation_outcomes
    db = get_db(DOMAIN_TRADING)
    data = parse_body()
    days = data.get('days_after', 7)
    outcomes = track_recommendation_outcomes(db, days_after=days)
    return api_ok({'outcomes': outcomes, 'count': len(outcomes)})
@trading_autopilot_bp.route('/api/v1/trading/autopilot/kpi', methods=['POST'])
@trading_autopilot_bp.route('/api/v1/trading/autopilot/kpi-evaluate', methods=['POST'])
def autopilot_kpi_evaluate():
    from lib.trading_autopilot import pre_backtest_evaluate
    db = get_db(DOMAIN_TRADING)
    data = parse_body()
    codes = data.get('symbols', [])
    lookback = data.get('lookback_days', 90)
    if not codes:
        holdings = db.execute('SELECT symbol FROM trading_holdings').fetchall()
        codes = [h['symbol'] for h in holdings]
    if not codes:
        return api_bad_request('No asset codes to evaluate')
    kpi = pre_backtest_evaluate(db, codes, lookback_days=lookback)
    return api_ok({'kpi': kpi})
@trading_autopilot_bp.route('/api/v1/trading/autopilot/strategy-evolution', methods=['POST'])
def autopilot_strategy_evolution():
    from lib.trading_autopilot import evolve_strategies
    db = get_db(DOMAIN_TRADING)
    ctx, items = evolve_strategies(db)
    return api_ok({'evolution_context': ctx, 'items': items})
# ═══════════════════════════════════════════════════════════
#  Background worker (started from server.py)
# ═══════════════════════════════════════════════════════════

def start_autopilot_worker():
    """Start the autopilot background scheduler thread."""
    def _worker():
        from lib.trading_autopilot import autopilot_scheduler_tick
        time.sleep(60)
        while True:
            try:
                autopilot_scheduler_tick(db_path=None)  # uses PG via get_thread_db
            except Exception as e:
                logger.error('[Autopilot Worker] %s', e, exc_info=True)
            time.sleep(300)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t
