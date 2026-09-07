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

import asyncio
import json
import threading
from datetime import datetime

from flask import Response, jsonify, request

from tofu_trading.storage import (
    DOMAIN_TRADING,
    async_fetchall,
    async_fetchone,
    get_thread_db,
)
from tofu_trading.storage import _pool_get, _pool_put
from lib.log import get_logger
from lib.api_response import api_conflict, api_internal_error, api_not_found, api_ok
from lib.request_parser import async_parse_body

from tofu_trading.identity import DEFAULT_OWNER_ID, current_user_id

logger = get_logger(__name__)

from tofu_trading.web.v1.brain import api_v1_trading_brain_bp as trading_brain_bp  # noqa: E402
# (alias kept for back-compat with `from tofu_trading.web.handlers.trading_brain import trading_brain_bp` callers)


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
    """Initialize cycle_count from DB so it survives server restarts.

    Runs at startup (register_all → init_brain), where there is NO Flask
    app/request context, so it must use the thread-local ``get_thread_db``
    accessor rather than the request-scoped ``get_db`` (which needs ``g``).
    """
    try:
        db = get_thread_db(
            DOMAIN_TRADING, owner_user_id=DEFAULT_OWNER_ID)
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


@trading_brain_bp.route('/api/v1/trading/brain/state', methods=['GET'])
async def brain_state():
    """Get brain state including recent cycles and stats."""
    state = dict(_brain_state)

    # Recent cycles
    cycles = await async_fetchall(
        'SELECT cycle_id, cycle_number, confidence_score, market_outlook, '
        'status, created_at FROM trading_autopilot_cycles '
        'ORDER BY cycle_number DESC, created_at DESC LIMIT 5',
        domain=DOMAIN_TRADING,
    )
    state['recent_cycles'] = [dict(c) for c in cycles]

    # Pending trades
    pending = await async_fetchone(
        "SELECT COUNT(*) as cnt FROM trading_trade_queue WHERE status='pending' AND user_id=?",
        (current_user_id(),),
        domain=DOMAIN_TRADING,
    )
    state['pending_trades'] = pending['cnt'] if pending else 0

    # Recommendation stats
    stats = await async_fetchone('''
        SELECT
          COUNT(*) as total,
          SUM(CASE WHEN status='correct' THEN 1 ELSE 0 END) as correct,
          SUM(CASE WHEN status='incorrect' THEN 1 ELSE 0 END) as incorrect,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
        FROM trading_autopilot_recommendations WHERE user_id=?
    ''', (current_user_id(),), domain=DOMAIN_TRADING)
    state['recommendation_stats'] = dict(stats) if stats else {}

    return jsonify(state)


@trading_brain_bp.route('/api/v1/trading/brain/analyze', methods=['POST'])
async def brain_analyze():
    """Trigger a brain analysis (sync). Returns full analysis result."""
    if _brain_state.get('running'):
        return api_conflict('Brain is already running an analysis')

    data = await async_parse_body()
    trigger = data.get('trigger', 'manual')

    # Gather news
    from tofu_trading.trading.news_gathering import gather_news_cached
    news = await asyncio.to_thread(gather_news_cached)

    _brain_state['running'] = True
    try:
        from tofu_trading.trading.brain.pipeline import run_brain_analysis

        _brain_state['cycle_count'] += 1
        scan_new_candidates = data.get('scan_candidates', True)
        cycle_number = _brain_state['cycle_count']
        # Resolve the owner HERE (request thread); _run() executes in a worker
        # thread where request context is gone.
        uid = current_user_id()

        def _run():
            # Sync DB-helper takes a borrowed connection; use the strict
            # checkout->use->return cycle (never get_thread_db on the loop).
            db = _pool_get(owner_user_id=uid)
            try:
                return run_brain_analysis(
                    db, trigger=trigger, news_items=news,
                    cycle_number=cycle_number, uid=uid,
                    scan_new_candidates=scan_new_candidates,
                )
            finally:
                _pool_put(db)

        result = await asyncio.to_thread(_run)

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
        return api_internal_error('internal_error')


@trading_brain_bp.route('/api/v1/trading/brain/stream', methods=['POST'])
async def brain_stream():
    """SSE streaming brain analysis — primary endpoint for AI操盘 tab."""
    from lib.llm_dispatch import dispatch_stream
    from tofu_trading.trading.brain.pipeline import _extract_and_queue_trades_from_result, build_brain_streaming_body
    from tofu_trading.trading_autopilot.cycle import _apply_strategy_updates, _store_cycle_result
    from tofu_trading.trading_autopilot.reasoning import parse_autopilot_result

    data = await async_parse_body()
    trigger = data.get('trigger', 'manual')

    from tofu_trading.trading.news_gathering import gather_news_cached
    news = await asyncio.to_thread(gather_news_cached)

    _brain_state['cycle_count'] += 1
    cycle_number = _brain_state['cycle_count']

    scan_new_candidates = data.get('scan_candidates', True)
    # Resolve the owner HERE (request thread) -- _build() runs in a worker.
    uid = current_user_id()

    def _build():
        # Sync DB-helper takes a borrowed connection; use the strict
        # checkout->use->return cycle (never get_thread_db on the loop).
        db = _pool_get(owner_user_id=uid)
        try:
            return build_brain_streaming_body(
                db, trigger=trigger, news_items=news,
                cycle_number=cycle_number, uid=uid,
                scan_new_candidates=scan_new_candidates,
            )
        finally:
            _pool_put(db)

    body, context = await asyncio.to_thread(_build)

    def generate():
        # Padding for SSE proxy buffering
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'

        import queue
        q = queue.Queue()
        from tofu_trading.run_ids import mint_run_id
        cycle_id = mint_run_id('brain', uid=current_user_id())

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

        t = threading.Thread(
            target=_worker,
            daemon=True,
            name=f'trading-brain-stream-{cycle_id}',
        )
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
                # Parse structured result first — always send to frontend
                structured = None
                try:
                    structured = parse_autopilot_result(val)
                except Exception as e:
                    logger.warning('[Brain] Result parsing failed: %s', e, exc_info=True)

                # Build done event with structured data (before storage)
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

                # Try to persist to DB (non-fatal — UI still gets data)
                try:
                    _db = get_thread_db(
                        DOMAIN_TRADING, owner_user_id=uid)
                    _store_cycle_result(
                        _db, cycle_id, cycle_number, val,
                        structured, uid, context.get('kpi_evaluations', {}),
                        context.get('correlations', []),
                    )
                    if structured and structured.get('strategy_updates'):
                        _apply_strategy_updates(_db, structured['strategy_updates'], uid)
                    _extract_and_queue_trades_from_result(_db, structured, cycle_id, uid)

                    _brain_state['last_cycle'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    _brain_state['last_cycle_id'] = cycle_id
                except Exception as e:
                    logger.error('[Brain] Result storage failed: %s', e, exc_info=True)
                    done_evt['storage_error'] = str(e)

                yield f"data: {json.dumps(done_evt, ensure_ascii=False)}\n\n"
                break

        t.join(timeout=2)

    return Response(
        generate(), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'},
    )


@trading_brain_bp.route('/api/v1/trading/brain/cycles', methods=['GET'])
async def brain_cycles():
    """List recent brain analysis cycles."""
    limit = request.args.get('limit', 20, type=int)
    rows = await async_fetchall(
        'SELECT * FROM trading_autopilot_cycles ORDER BY cycle_number DESC, created_at DESC LIMIT ?',
        (limit,),
        domain=DOMAIN_TRADING,
    )

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


@trading_brain_bp.route('/api/v1/trading/brain/cycles/<cycle_id>', methods=['GET'])
async def brain_cycle_detail(cycle_id):
    """Get detail for a specific brain cycle."""
    row = await async_fetchone(
        'SELECT * FROM trading_autopilot_cycles WHERE cycle_id=?', (cycle_id,),
        domain=DOMAIN_TRADING,
    )
    if not row:
        try:
            row = await async_fetchone(
                'SELECT * FROM trading_autopilot_cycles WHERE id=?', (int(cycle_id),),
                domain=DOMAIN_TRADING,
            )
        except (ValueError, TypeError) as _e:
            logger.debug('[Brain] cycle_id int parse failed: %s', _e)
    if not row:
        return api_not_found('Cycle not found')

    d = dict(row)
    for key in ('structured_result', 'kpi_evaluations', 'correlations'):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else ({} if key != 'correlations' else [])
        except Exception as e:
            logger.warning('[Brain] JSON parse failed for %s: %s', key, e, exc_info=True)

    recs = await async_fetchall(
        'SELECT * FROM trading_autopilot_recommendations WHERE cycle_id=? AND user_id=? ORDER BY confidence DESC',
        (cycle_id, current_user_id()),
        domain=DOMAIN_TRADING,
    )
    d['recommendations'] = [dict(r) for r in recs]

    return jsonify({'cycle': d})


@trading_brain_bp.route('/api/v1/trading/brain/auto/toggle', methods=['POST'])
async def brain_auto_toggle():
    """Toggle automatic brain analysis (scheduled mode)."""
    data = await async_parse_body()
    enabled = data.get('enabled', False)
    _brain_state['auto_enabled'] = enabled

    # Also sync with autopilot scheduler
    try:
        from tofu_trading.trading_autopilot import set_autopilot_enabled
        await asyncio.to_thread(set_autopilot_enabled, enabled)
    except Exception as e:
        logger.warning('[Brain] Autopilot sync failed: %s', e, exc_info=True)

    return api_ok({'auto_enabled': enabled})
