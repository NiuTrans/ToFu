"""routes/trading_autopilot.py — Autopilot state, toggle, cycles, recommendations, KPI."""

import json
import threading
import time
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_TRADING, get_db, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)
trading_autopilot_bp = Blueprint('trading_autopilot', __name__)

# ── Shared autopilot state ──
_autopilot_state = {'running': False, 'cycle_count': 0, 'last_cycle': None,
                    'last_cycle_id': None, 'error': None}


def get_autopilot_state():
    return _autopilot_state


@trading_autopilot_bp.route('/api/trading/autopilot/state', methods=['GET'])
def autopilot_state():
    """Full autopilot state including recent cycles."""
    from lib.trading_autopilot import get_autopilot_state as _ap_state
    state = _ap_state()
    state.update(_autopilot_state)

    db = get_db(DOMAIN_TRADING)
    cycles = db.execute(
        'SELECT * FROM trading_autopilot_cycles ORDER BY cycle_number DESC, created_at DESC LIMIT 10'
    ).fetchall()
    state['recent_cycles'] = [dict(c) for c in cycles]
    stats = db.execute('''
        SELECT
          COUNT(*) as total,
          SUM(CASE WHEN status='correct' THEN 1 ELSE 0 END) as correct,
          SUM(CASE WHEN status='incorrect' THEN 1 ELSE 0 END) as incorrect,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
        FROM trading_autopilot_recommendations
    ''').fetchone()
    state['recommendation_stats'] = dict(stats) if stats else {}
    return jsonify(state)


@trading_autopilot_bp.route('/api/trading/autopilot/toggle', methods=['POST'])
def autopilot_toggle():
    from lib.trading_autopilot import get_autopilot_state as _ap_state
    from lib.trading_autopilot import set_autopilot_enabled
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    set_autopilot_enabled(enabled)
    return jsonify({'ok': True, 'state': _ap_state()})


@trading_autopilot_bp.route('/api/trading/autopilot/run', methods=['POST'])
def autopilot_run_now():
    """Trigger an immediate autopilot cycle."""
    from lib.trading_autopilot import run_autopilot_cycle
    if _autopilot_state.get('running'):
        return jsonify({'error': 'Autopilot is already running'}), 409

    db = get_db(DOMAIN_TRADING)
    from .trading_decision import _gather_news_cached
    news = _gather_news_cached()
    _autopilot_state['running'] = True
    try:
        _autopilot_state['cycle_count'] = _autopilot_state.get('cycle_count', 0) + 1
        result = run_autopilot_cycle(db, news_items=news,
                                     cycle_number=_autopilot_state['cycle_count'])
        _autopilot_state['last_cycle'] = result['timestamp']
        _autopilot_state['last_cycle_id'] = result['cycle_id']
        _autopilot_state['running'] = False
        return jsonify({'ok': True, 'result': {
            'cycle_id': result['cycle_id'],
            'timestamp': result['timestamp'],
            'structured_result': result['structured_result'],
            'kpi_evaluations': result['kpi_evaluations'],
        }})
    except Exception as e:
        _autopilot_state['running'] = False
        _autopilot_state['error'] = str(e)
        logger.error('[Autopilot] Sync analysis failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@trading_autopilot_bp.route('/api/trading/autopilot/stream', methods=['POST'])
def autopilot_stream():
    """SSE streaming autopilot analysis — the primary endpoint for the UI."""
    from lib.llm_dispatch import dispatch_stream
    from lib.trading_autopilot import (
        _apply_strategy_updates,
        _store_cycle_result,
        build_autopilot_streaming_body,
        parse_autopilot_result,
    )

    db = get_db(DOMAIN_TRADING)
    from .trading_decision import _gather_news_cached
    news = _gather_news_cached()
    _autopilot_state['cycle_count'] = _autopilot_state.get('cycle_count', 0) + 1
    cycle_number = _autopilot_state['cycle_count']

    body, context = build_autopilot_streaming_body(db, news_items=news,
                                                   cycle_number=cycle_number)

    def generate():
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'
        import queue
        q = queue.Queue()
        cycle_id = f"autopilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        def _worker():
            try:
                joined_parts = []
                def _on_content(t):
                    joined_parts.append(t)
                    q.put(('content', t))

                dispatch_stream(body,
                                on_thinking=lambda t: q.put(('thinking', t)),
                                on_content=_on_content,
                                capability='thinking',
                                log_prefix=f'[Autopilot-{cycle_id}]')
                q.put(('done', ''.join(joined_parts)))
            except Exception as e:
                logger.error('[Autopilot] LLM streaming call failed: %s', e, exc_info=True)
                q.put(('error', str(e)))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        # Emit pre-computed context while LLM generates
        if context.get('correlations'):
            yield f"data: {json.dumps({'correlations': context['correlations']})}\n\n"
        if context.get('kpi_evaluations'):
            yield f"data: {json.dumps({'kpi_evaluations': context['kpi_evaluations']})}\n\n"

        while True:
            try:
                kind, val = q.get(timeout=300)
            except Exception as e:
                logger.warning('[Autopilot] Queue get timed out after 300s: %s', e, exc_info=True)
                yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
                break
            if kind == 'error':
                yield f"data: {json.dumps({'error': val})}\n\n"
                break
            elif kind == 'thinking':
                yield f"data: {json.dumps({'thinking': val})}\n\n"
            elif kind == 'content':
                yield f"data: {json.dumps({'content': val})}\n\n"
            elif kind == 'done':
                try:
                    _db = get_thread_db(DOMAIN_TRADING)
                    structured = parse_autopilot_result(val)
                    _store_cycle_result(
                        _db, cycle_id, cycle_number, val,
                        structured, context.get('kpi_evaluations', {}),
                        context.get('correlations', [])
                    )
                    if structured and structured.get('strategy_updates'):
                        _apply_strategy_updates(_db, structured['strategy_updates'])
                    _autopilot_state['last_cycle'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    _autopilot_state['last_cycle_id'] = cycle_id
                    done_evt = {
                        'done': True,
                        'cycle_id': cycle_id,
                        'kpi_evaluations': context.get('kpi_evaluations', {}),
                        'correlations': context.get('correlations', []),
                        'context_summary': {
                            'intel_count': context.get('intel_count', 0),
                            'holdings_count': context.get('holdings_count', 0),
                            'cash': context.get('cash', 0),
                        },
                    }
                    if structured:
                        done_evt['recommendations'] = structured.get('position_recommendations', structured.get('recommendations', []))
                        done_evt['risk_factors'] = structured.get('risk_factors', [])
                        done_evt['strategy_updates'] = structured.get('strategy_updates', [])
                        done_evt['market_outlook'] = structured.get('market_outlook', '')
                        done_evt['confidence'] = structured.get('confidence_score', structured.get('confidence', 0))
                        done_evt['next_review'] = structured.get('next_review', '')
                    yield f"data: {json.dumps(done_evt, ensure_ascii=False)}\n\n"
                except Exception as e:
                    logger.error('[Autopilot] Result parsing/storage failed for cycle %s: %s', cycle_id, e, exc_info=True)
                    yield f"data: {json.dumps({'done': True, 'cycle_id': cycle_id, 'error': str(e)})}\n\n"
                break
        t.join(timeout=2)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


@trading_autopilot_bp.route('/api/trading/autopilot/cycles', methods=['GET'])
def autopilot_cycles_list():
    db = get_db(DOMAIN_TRADING)
    limit = request.args.get('limit', 20, type=int)
    rows = db.execute(
        'SELECT * FROM trading_autopilot_cycles ORDER BY cycle_number DESC, created_at DESC LIMIT ?', (limit,)
    ).fetchall()
    cycles = []
    for r in rows:
        d = dict(r)
        for key in ('structured_result', 'kpi_evaluations', 'correlations'):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else ({} if key != 'correlations' else [])
            except Exception as e:
                logger.warning('Failed to parse autopilot cycle JSON field %s: %s', key, e, exc_info=True)
        cycles.append(d)
    return jsonify({'cycles': cycles})


@trading_autopilot_bp.route('/api/trading/autopilot/cycles/<cycle_id>', methods=['GET'])
def autopilot_cycle_detail(cycle_id):
    db = get_db(DOMAIN_TRADING)
    # Try by cycle_id first, then by integer id
    row = db.execute('SELECT * FROM trading_autopilot_cycles WHERE cycle_id=?',
                     (cycle_id,)).fetchone()
    if not row:
        try:
            row = db.execute('SELECT * FROM trading_autopilot_cycles WHERE id=?',
                             (int(cycle_id),)).fetchone()
        except (ValueError, TypeError) as e:
            logger.debug('[Autopilot] cycle_id %r int conversion failed: %s', cycle_id, e, exc_info=True)
    if not row:
        return jsonify({'error': 'Cycle not found'}), 404
    d = dict(row)
    for key in ('structured_result', 'kpi_evaluations', 'correlations'):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else ({} if key != 'correlations' else [])
        except Exception as e:
            logger.warning('Failed to parse autopilot cycle detail JSON field %s: %s', key, e, exc_info=True)
    recs = db.execute(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? ORDER BY confidence DESC',
        (cycle_id,)
    ).fetchall()
    d['recommendations'] = [dict(r) for r in recs]
    return jsonify({'cycle': d})


@trading_autopilot_bp.route('/api/trading/autopilot/cycles/<cycle_id>/recommendations', methods=['GET'])
def autopilot_cycle_recommendations(cycle_id):
    """Return recommendations for a specific cycle."""
    db = get_db(DOMAIN_TRADING)
    rows = db.execute(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? ORDER BY confidence DESC',
        (cycle_id,)
    ).fetchall()
    return jsonify({'recommendations': [dict(r) for r in rows]})


@trading_autopilot_bp.route('/api/trading/autopilot/recommendations', methods=['GET'])
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


@trading_autopilot_bp.route('/api/trading/autopilot/recommendations/<int:rid>/accept', methods=['POST'])
def autopilot_accept_recommendation(rid):
    db = get_db(DOMAIN_TRADING)
    rec = db.execute('SELECT * FROM trading_autopilot_recommendations WHERE id=?', (rid,)).fetchone()
    if not rec:
        return jsonify({'error': 'Recommendation not found'}), 404
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
    return jsonify({'ok': True})


@trading_autopilot_bp.route('/api/trading/autopilot/recommendations/<int:rid>/reject', methods=['POST'])
def autopilot_reject_recommendation(rid):
    db = get_db(DOMAIN_TRADING)
    db.execute('UPDATE trading_autopilot_recommendations SET status=? WHERE id=?', ('rejected', rid))
    db.commit()
    return jsonify({'ok': True})


@trading_autopilot_bp.route('/api/trading/autopilot/evaluate', methods=['POST'])
def autopilot_evaluate_outcomes():
    from lib.trading_autopilot import track_recommendation_outcomes
    db = get_db(DOMAIN_TRADING)
    data = request.get_json(silent=True) or {}
    days = data.get('days_after', 7)
    outcomes = track_recommendation_outcomes(db, days_after=days)
    return jsonify({'ok': True, 'outcomes': outcomes, 'count': len(outcomes)})


@trading_autopilot_bp.route('/api/trading/autopilot/track', methods=['POST'])
def autopilot_track_outcomes():
    from lib.trading_autopilot import track_recommendation_outcomes
    db = get_db(DOMAIN_TRADING)
    data = request.get_json(silent=True) or {}
    days = data.get('days_after', 7)
    outcomes = track_recommendation_outcomes(db, days_after=days)
    return jsonify({'ok': True, 'outcomes': outcomes, 'count': len(outcomes)})


@trading_autopilot_bp.route('/api/trading/autopilot/kpi', methods=['POST'])
@trading_autopilot_bp.route('/api/trading/autopilot/kpi-evaluate', methods=['POST'])
def autopilot_kpi_evaluate():
    from lib.trading_autopilot import pre_backtest_evaluate
    db = get_db(DOMAIN_TRADING)
    data = request.get_json(silent=True) or {}
    codes = data.get('symbols', [])
    lookback = data.get('lookback_days', 90)
    if not codes:
        holdings = db.execute('SELECT symbol FROM trading_holdings').fetchall()
        codes = [h['symbol'] for h in holdings]
    if not codes:
        return jsonify({'error': 'No asset codes to evaluate'}), 400
    kpi = pre_backtest_evaluate(db, codes, lookback_days=lookback)
    return jsonify({'ok': True, 'kpi': kpi})


@trading_autopilot_bp.route('/api/trading/autopilot/strategy-evolution', methods=['POST'])
def autopilot_strategy_evolution():
    from lib.trading_autopilot import evolve_strategies
    db = get_db(DOMAIN_TRADING)
    ctx, items = evolve_strategies(db)
    return jsonify({'ok': True, 'evolution_context': ctx, 'items': items})


# ── Background worker (started from server.py) ──

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
