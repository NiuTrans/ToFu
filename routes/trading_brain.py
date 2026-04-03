"""routes/trading_brain.py — Unified Brain API (decision + screening + strategy + autopilot).

Consolidates ALL decision-making endpoints into ONE route:
  - Brain analysis (stream / sync)
  - Screening (now part of brain pipeline)
  - Strategy CRUD (managed by brain)
  - Autopilot state (brain's running mode)
  - Trade queue (brain's output)
  - KPI evaluation
  - Outcome tracking

The existing trading_decision_bp, trading_screening_bp, trading_strategy_bp,
and trading_autopilot_bp routes remain active for backward compatibility.
This module adds the unified /api/trading/brain/* endpoints.
"""

import json
import threading
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_TRADING, get_db, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

trading_brain_bp = Blueprint('trading_brain', __name__)


# ── Shared brain state ──
_brain_state = {
    'running': False,
    'cycle_count': 0,
    'last_cycle': None,
    'last_cycle_id': None,
    'error': None,
    'auto_enabled': False,
}


def _init_cycle_count():
    """Initialize cycle_count from DB so it survives server restarts."""
    try:
        db = get_db(DOMAIN_TRADING)
        row = db.execute(
            'SELECT MAX(cycle_number) as max_num FROM trading_autopilot_cycles'
        ).fetchone()
        if row and row['max_num']:
            _brain_state['cycle_count'] = row['max_num']
            logger.info('[Brain] Initialized cycle_count=%d from DB', row['max_num'])
    except Exception as e:
        logger.warning('[Brain] Failed to init cycle_count from DB: %s', e)


def get_brain_state():
    """Expose brain state for other modules."""
    return _brain_state


def init_brain():
    """Called once at app startup to restore state from DB."""
    _init_cycle_count()


@trading_brain_bp.route('/api/trading/brain/state', methods=['GET'])
def brain_state():
    """Get brain state including recent cycles and stats."""
    db = get_db(DOMAIN_TRADING)
    state = dict(_brain_state)

    # Recent cycles
    cycles = db.execute(
        'SELECT cycle_id, cycle_number, confidence_score, market_outlook, '
        'status, created_at FROM trading_autopilot_cycles '
        'ORDER BY cycle_number DESC, created_at DESC LIMIT 5'
    ).fetchall()
    state['recent_cycles'] = [dict(c) for c in cycles]

    # Pending trades
    pending = db.execute(
        "SELECT COUNT(*) as cnt FROM trading_trade_queue WHERE status='pending'"
    ).fetchone()
    state['pending_trades'] = pending['cnt'] if pending else 0

    # Recommendation stats
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


@trading_brain_bp.route('/api/trading/brain/analyze', methods=['POST'])
def brain_analyze():
    """Trigger a brain analysis (sync). Returns full analysis result."""
    if _brain_state.get('running'):
        return jsonify({'error': 'Brain is already running an analysis'}), 409

    data = request.get_json(silent=True) or {}
    trigger = data.get('trigger', 'manual')

    db = get_db(DOMAIN_TRADING)

    # Gather news
    from .trading_decision import _gather_news_cached
    news = _gather_news_cached()

    _brain_state['running'] = True
    try:
        from lib.trading.brain.pipeline import run_brain_analysis

        _brain_state['cycle_count'] += 1
        result = run_brain_analysis(
            db, trigger=trigger, news_items=news,
            cycle_number=_brain_state['cycle_count'],
            scan_new_candidates=data.get('scan_candidates', True),
        )

        _brain_state['last_cycle'] = result['timestamp']
        _brain_state['last_cycle_id'] = result['cycle_id']
        _brain_state['running'] = False

        return jsonify({
            'ok': True,
            'cycle_id': result['cycle_id'],
            'timestamp': result['timestamp'],
            'structured_result': result['structured_result'],
            'kpi_evaluations': result['kpi_evaluations'],
            'new_candidates': result.get('new_candidates', []),
            'alerts': result.get('alerts', []),
        })
    except Exception as e:
        _brain_state['running'] = False
        _brain_state['error'] = str(e)
        logger.error('[Brain] Analysis failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@trading_brain_bp.route('/api/trading/brain/stream', methods=['POST'])
def brain_stream():
    """SSE streaming brain analysis — primary endpoint for AI操盘 tab."""
    from lib.llm_dispatch import dispatch_stream
    from lib.trading.brain.pipeline import _extract_and_queue_trades_from_result, build_brain_streaming_body
    from lib.trading_autopilot.cycle import _apply_strategy_updates, _store_cycle_result
    from lib.trading_autopilot.reasoning import parse_autopilot_result

    data = request.get_json(silent=True) or {}
    trigger = data.get('trigger', 'manual')

    db = get_db(DOMAIN_TRADING)
    from .trading_decision import _gather_news_cached
    news = _gather_news_cached()

    _brain_state['cycle_count'] += 1
    cycle_number = _brain_state['cycle_count']

    body, context = build_brain_streaming_body(
        db, trigger=trigger, news_items=news,
        cycle_number=cycle_number,
        scan_new_candidates=data.get('scan_candidates', True),
    )

    def generate():
        # Padding for SSE proxy buffering
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'

        import queue
        q = queue.Queue()
        cycle_id = f"brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        def _worker():
            try:
                joined_parts = []

                def _on_content(t):
                    joined_parts.append(t)
                    q.put(('content', t))

                dispatch_stream(
                    body,
                    on_thinking=lambda t: q.put(('thinking', t)),
                    on_content=_on_content,
                    capability='thinking',
                    log_prefix=f'[Brain-{cycle_id}]',
                )
                q.put(('done', ''.join(joined_parts)))
            except Exception as e:
                logger.error('[Brain] Streaming failed: %s', e, exc_info=True)
                q.put(('error', str(e)))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        # Emit pre-computed context
        if context.get('kpi_evaluations'):
            yield f"data: {json.dumps({'kpi_evaluations': context['kpi_evaluations']})}\n\n"
        if context.get('new_candidates'):
            yield f"data: {json.dumps({'new_candidates': context['new_candidates']})}\n\n"
        if context.get('alerts'):
            yield f"data: {json.dumps({'alerts': context['alerts']})}\n\n"

        while True:
            try:
                kind, val = q.get(timeout=300)
            except Exception as e:
                logger.warning('[Brain] Queue timeout: %s', e, exc_info=True)
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
                        context.get('correlations', []),
                    )
                    if structured and structured.get('strategy_updates'):
                        _apply_strategy_updates(_db, structured['strategy_updates'])
                    _extract_and_queue_trades_from_result(_db, structured, cycle_id)

                    _brain_state['last_cycle'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    _brain_state['last_cycle_id'] = cycle_id

                    done_evt = {
                        'done': True,
                        'cycle_id': cycle_id,
                        'kpi_evaluations': context.get('kpi_evaluations', {}),
                        'new_candidates': context.get('new_candidates', []),
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
                    logger.error('[Brain] Result storage failed: %s', e, exc_info=True)
                    yield f"data: {json.dumps({'done': True, 'cycle_id': cycle_id, 'error': str(e)})}\n\n"
                break

        t.join(timeout=2)

    return Response(
        generate(), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'},
    )


@trading_brain_bp.route('/api/trading/brain/cycles', methods=['GET'])
def brain_cycles():
    """List recent brain analysis cycles."""
    db = get_db(DOMAIN_TRADING)
    limit = request.args.get('limit', 20, type=int)
    rows = db.execute(
        'SELECT * FROM trading_autopilot_cycles ORDER BY cycle_number DESC, created_at DESC LIMIT ?',
        (limit,)
    ).fetchall()

    cycles = []
    for r in rows:
        d = dict(r)
        for key in ('structured_result', 'kpi_evaluations', 'correlations'):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else ({} if key != 'correlations' else [])
            except Exception as e:
                logger.warning('[Brain] JSON parse failed for %s: %s', key, e, exc_info=True)
        cycles.append(d)

    return jsonify({'cycles': cycles})


@trading_brain_bp.route('/api/trading/brain/cycles/<cycle_id>', methods=['GET'])
def brain_cycle_detail(cycle_id):
    """Get detail for a specific brain cycle."""
    db = get_db(DOMAIN_TRADING)
    row = db.execute(
        'SELECT * FROM trading_autopilot_cycles WHERE cycle_id=?', (cycle_id,)
    ).fetchone()
    if not row:
        try:
            row = db.execute(
                'SELECT * FROM trading_autopilot_cycles WHERE id=?', (int(cycle_id),)
            ).fetchone()
        except (ValueError, TypeError) as _e:
            logger.debug('[Brain] cycle_id int parse failed: %s', _e)
    if not row:
        return jsonify({'error': 'Cycle not found'}), 404

    d = dict(row)
    for key in ('structured_result', 'kpi_evaluations', 'correlations'):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else ({} if key != 'correlations' else [])
        except Exception as e:
            logger.warning('[Brain] JSON parse failed for %s: %s', key, e, exc_info=True)

    recs = db.execute(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? ORDER BY confidence DESC',
        (cycle_id,)
    ).fetchall()
    d['recommendations'] = [dict(r) for r in recs]

    return jsonify({'cycle': d})


@trading_brain_bp.route('/api/trading/brain/auto/toggle', methods=['POST'])
def brain_auto_toggle():
    """Toggle automatic brain analysis (scheduled mode)."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    _brain_state['auto_enabled'] = enabled

    # Also sync with autopilot scheduler
    try:
        from lib.trading_autopilot import set_autopilot_enabled
        set_autopilot_enabled(enabled)
    except Exception as e:
        logger.warning('[Brain] Autopilot sync failed: %s', e, exc_info=True)

    return jsonify({'ok': True, 'auto_enabled': enabled})
