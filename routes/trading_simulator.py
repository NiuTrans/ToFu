"""routes/trading_simulator.py — API endpoints for LLM-driven historical simulation.

Endpoints:
  POST /api/trading/sim/fetch-data       — Start data fetch (returns task_id)
  GET  /api/trading/sim/fetch-progress/<id> — Poll fetch progress (returns new events)
  POST /api/trading/sim/run              — Start LLM simulation (returns task_id)
  GET  /api/trading/sim/run-progress/<id> — Poll simulation progress (returns new events)
  GET  /api/trading/sim/sessions         — List all simulation sessions
  GET  /api/trading/sim/session/<id>     — Get session details + metrics
  GET  /api/trading/sim/journal/<id>     — Get decision journal
  GET  /api/trading/sim/coverage         — Check data coverage for a period

★ FIX (2026-03-29): Both fetch-data AND sim-run use POLLING mode.
  SSE events through VS Code tunnel proxy are silently buffered.
  POLLING mode is also refresh-safe: server stores ALL events in-memory
  for 1 hour, so a browser refresh can resume from cursor=0 and replay
  all events without losing progress.
"""

import threading

from flask import jsonify, request

from lib.database import DOMAIN_TRADING, get_thread_db
from lib.log import get_logger
from lib.api_response import api_bad_request
from lib.request_parser import parse_body
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

from routes.api_v1.trading.simulator import api_v1_trading_simulator_bp as trading_simulator_bp  # noqa: E402
# (alias kept for back-compat with `from routes.trading_simulator import trading_simulator_bp` callers)


# ═══════════════════════════════════════════════════════════
#  Unified task storage via TaskRuntime
#  Used by BOTH fetch-data and sim-run (kind distinguishes them).
# ═══════════════════════════════════════════════════════════

_runtime = TaskRuntime(
    'trading-sim', ttl=3600,
    push_channel='trading-sim',
    error_source='routes.trading_simulator',
)


def _create_task(task_type: str) -> str:
    """Create a new task and return its ID. ``task_type`` stored in meta."""
    task = _runtime.create(meta={'type': task_type})
    return task['id']


def _append_event(task_id: str, evt: dict):
    """Thread-safe: append an event to a task's event list."""
    _runtime.append_event(task_id, evt)


def _finish_task(task_id: str, result=None, error=None, *,
                 error_context: str = '', error_source: str = ''):
    """Thread-safe: mark task as done with result or error.

    ``error`` may be:
      * ``None`` — success
      * ``BaseException`` — wrapped via ``error_envelope.from_exception``
      * ``str`` — wrapped via ``error_envelope.make_envelope('generic', ...)``
      * ``dict`` (already an envelope) — stored as-is
    """
    _runtime.finish(task_id, result=result, error=error,
                    error_context=error_context or 'trading-sim')


def _get_task_progress(task_id: str, cursor: int = 0) -> dict:
    """Get new events since cursor (legacy response shape preserved).

    Maps TaskRuntime's standard shape to the legacy keys that the
    frontend (static/js/trading/simulator.js) expects:
      - ``cursor``      (not ``next_cursor``)
      - ``error: 'Task not found'``  on missing task (string)
      - top-level ``result`` / ``error`` (envelope) when done
    """
    poll_resp = _runtime.poll(task_id, cursor=cursor)
    if not poll_resp['ok']:
        # Match the legacy {'error': 'Task not found'} string shape
        return {'error': 'Task not found', 'done': True,
                'events': [], 'cursor': 0}

    task = _runtime.get(task_id)
    task_type = (task['meta'].get('type', 'unknown')
                 if task else 'unknown')
    resp = {
        'events': poll_resp['events'],
        'cursor': poll_resp['next_cursor'],
        'done': poll_resp['done'],
        'task_type': task_type,
    }
    if poll_resp['done']:
        if poll_resp.get('error'):
            resp['error'] = poll_resp['error']
        elif poll_resp.get('result'):
            resp['result'] = poll_resp['result']
    return resp


# ═══════════════════════════════════════════════════════════
#  Data Fetching — POLLING mode (proxy-safe + refresh-safe)
# ═══════════════════════════════════════════════════════════

@trading_simulator_bp.route('/api/v1/trading/sim/fetch-data', methods=['POST'])
def sim_fetch_data():
    """Start historical data fetch in background.

    Returns immediately with {task_id}.  Frontend polls
    /sim/fetch-progress/<task_id>?cursor=0 for new events.

    Request JSON:
        {
            "symbols": ["510300", "159915"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
            "skip_intel": false
        }
    """
    data = parse_body(force=True)
    symbols = data.get('symbols', [])
    start_date = data.get('start_date', '')
    end_date = data.get('end_date', '')
    skip_intel = data.get('skip_intel', False)
    # Register custom asset names from frontend
    symbol_names = data.get('symbol_names', {})
    if symbol_names:
        from lib.trading.historical_data import register_asset_name
        from lib.trading.llm_simulator import register_sim_asset_name
        for code, name in symbol_names.items():
            register_asset_name(code, name)
            register_sim_asset_name(code, name)

    if not start_date or not end_date:
        return api_bad_request('start_date, end_date are required')
    # symbols may be empty — open-universe mode, AI discovers on its own

    task_id = _create_task('fetch')

    def on_progress(phase, done, total, msg=''):
        """Thread-safe progress callback — appends to task event list."""
        _append_event(task_id, {
            'phase': phase,
            'done': done,
            'total': total,
            'message': msg,
        })

    def _run_fetch():
        """Background thread: runs the full fetch, stores result in task."""
        try:
            db = get_thread_db(DOMAIN_TRADING)
            from lib.trading.historical_data import run_full_historical_fetch
            result = run_full_historical_fetch(
                db, symbols, start_date, end_date,
                on_progress=on_progress,
                skip_intel=skip_intel,
            )
            _finish_task(task_id, result=result)
        except Exception as e:
            logger.error('[SimRoute] Data fetch failed: %s', e, exc_info=True)
            _finish_task(task_id, error=e,
                         error_context='trading-sim:fetch',
                         error_source='routes.trading_simulator:fetch')

    thread = threading.Thread(target=_run_fetch, daemon=True)
    thread.start()

    _stale = _runtime.cleanup_stale()
    if _stale:
        logger.info('[SimRoute] Cleaned up %d expired tasks', _stale)

    return jsonify({'task_id': task_id, 'status': 'started'})


@trading_simulator_bp.route('/api/v1/trading/sim/fetch-progress/<task_id>', methods=['GET'])
def sim_fetch_progress(task_id):
    """Poll for fetch progress events.

    Query params:
        cursor: Event index to start from (default 0).
                Client sends cursor=0 on first poll (or after refresh).

    Returns:
        {
            "events": [...new events since cursor...],
            "cursor": <new cursor value>,
            "done": false,
            "result": null,
            "error": null
        }
    """
    cursor = int(request.args.get('cursor', 0))
    resp = _get_task_progress(task_id, cursor)

    status_code = 404 if (resp.get('error') == 'Task not found') else 200
    return jsonify(resp), status_code


# ═══════════════════════════════════════════════════════════
#  Run Simulation — POLLING mode (proxy-safe + refresh-safe)
#
#  ★ FIX: Converted from SSE to polling.
#  Events are stored in _tasks[task_id] just like fetch-data.
#  Frontend polls /sim/run-progress/<id>?cursor=N every 1.5s.
#  On browser refresh, frontend resumes from cursor=0, replaying
#  all events to rebuild the timeline and equity chart.
# ═══════════════════════════════════════════════════════════

@trading_simulator_bp.route('/api/v1/trading/sim/run', methods=['POST'])
def sim_run():
    """Start LLM-driven historical simulation in background.

    Returns immediately with {task_id}.  Frontend polls
    /sim/run-progress/<task_id>?cursor=0 for new events.

    Request JSON:
        {
            "symbols": ["510300", "159915", "512880"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
            "initial_capital": 100000,
            "step_days": 5,
            ...
        }
    """
    data = parse_body(force=True)
    symbols = data.get('symbols', [])
    start_date = data.get('start_date', '')
    end_date = data.get('end_date', '')

    if not start_date or not end_date:
        return api_bad_request('start_date, end_date are required')
    # symbols may be empty — open-universe mode, AI discovers on its own

    task_id = _create_task('sim')

    def on_event(event_type, event_data):
        """Thread-safe callback from simulator — stores each event."""
        event_data['_type'] = event_type
        _append_event(task_id, event_data)

    def _run_sim():
        """Background thread: runs the full simulation, stores result."""
        try:
            db = get_thread_db(DOMAIN_TRADING)
            from lib.trading.llm_simulator import SimulatorConfig, run_simulation

            config = SimulatorConfig(
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                initial_capital=data.get('initial_capital', 100000),
                step_days=data.get('step_days', 5),
                max_position_pct=data.get('max_position_pct', 30),
                max_positions=data.get('max_positions', 5),
                stop_loss_pct=data.get('stop_loss_pct', 5),
                take_profit_pct=data.get('take_profit_pct', 15),
                buy_fee_rate=data.get('buy_fee_rate', 0.0015),
                sell_fee_rate=data.get('sell_fee_rate', 0.005),
                min_confidence=data.get('min_confidence', 50),
                t_plus_1=data.get('t_plus_1', True),
                benchmark_index=data.get('benchmark_index', '1.000300'),
                strategy=data.get('strategy', data.get('risk_level', 'balanced')),
            )

            result = run_simulation(db, config, on_event=on_event)

            # Build the final result payload
            sim_result = {
                'session_id': result.get('session_id'),
                'status': result.get('status'),
                'metrics': result.get('metrics', {}),
                'benchmark': result.get('benchmark', {}),
                'total_fees': result.get('total_fees', 0),
                'trade_count': len(result.get('trade_log', [])),
            }
            _finish_task(task_id, result=sim_result)

        except Exception as e:
            logger.error('[SimRoute] Simulation failed: %s', e, exc_info=True)
            _finish_task(task_id, error=e,
                         error_context='trading-sim:run',
                         error_source='routes.trading_simulator:run')

    thread = threading.Thread(target=_run_sim, daemon=True)
    thread.start()

    _stale = _runtime.cleanup_stale()
    if _stale:
        logger.info('[SimRoute] Cleaned up %d expired tasks', _stale)

    return jsonify({'task_id': task_id, 'status': 'started'})


@trading_simulator_bp.route('/api/v1/trading/sim/run-progress/<task_id>', methods=['GET'])
def sim_run_progress(task_id):
    """Poll for simulation progress events.

    Query params:
        cursor: Event index to start from (default 0).

    Returns same format as fetch-progress.
    """
    cursor = int(request.args.get('cursor', 0))
    resp = _get_task_progress(task_id, cursor)

    status_code = 404 if (resp.get('error') == 'Task not found') else 200
    return jsonify(resp), status_code


# ═══════════════════════════════════════════════════════════
#  Session Management
# ═══════════════════════════════════════════════════════════

@trading_simulator_bp.route('/api/v1/trading/sim/sessions', methods=['GET'])
def sim_list_sessions():
    db = get_thread_db(DOMAIN_TRADING)
    limit = int(request.args.get('limit', 20))
    from lib.trading.llm_simulator import list_sim_sessions
    sessions = list_sim_sessions(db, limit=limit)
    return jsonify({'sessions': sessions})


@trading_simulator_bp.route('/api/v1/trading/sim/session/<session_id>', methods=['GET'])
def sim_get_session(session_id):
    db = get_thread_db(DOMAIN_TRADING)
    from lib.trading.llm_simulator import get_sim_stats
    stats = get_sim_stats(db, session_id)
    if 'error' in stats:
        return jsonify(stats), 404
    return jsonify(stats)


@trading_simulator_bp.route('/api/v1/trading/sim/journal/<session_id>', methods=['GET'])
def sim_get_journal(session_id):
    """Get decision journal for a simulation.

    Query params:
        limit: Max rows (default 100).
        type: Optional entry_type filter.  Use 'step_summary' to get
              per-step aggregate data with portfolio_value and actions.
    """
    db = get_thread_db(DOMAIN_TRADING)
    limit = int(request.args.get('limit', 100))
    entry_type = request.args.get('type', '')
    from lib.trading.llm_simulator import get_sim_journal
    journal = get_sim_journal(db, session_id, limit=limit,
                              entry_type=entry_type)
    return jsonify({'journal': journal})


# ═══════════════════════════════════════════════════════════
#  Asset Search — Stocks + ETFs + Funds
# ═══════════════════════════════════════════════════════════

@trading_simulator_bp.route('/api/v1/trading/sim/search', methods=['GET'])
def sim_search_assets():
    """Universal asset search — finds stocks, ETFs, and funds.

    Query params:
        q: Search keyword (code, name, or pinyin abbreviation).

    Returns:
        {results: [{code, name, type, market}, ...]}
    """
    q = request.args.get('q', '').strip()
    if not q or len(q) < 1:
        return jsonify({'results': []})
    try:
        from lib.trading.info import search_asset_universal
        results = search_asset_universal(q)
        return jsonify({'results': results})
    except Exception as e:
        logger.warning('[SimRoute] Asset search failed for q=%s: %s', q, e)
        return jsonify({'results': [], 'error': str(e)})


# ═══════════════════════════════════════════════════════════
#  Data Coverage Check
# ═══════════════════════════════════════════════════════════

@trading_simulator_bp.route('/api/v1/trading/sim/strategies', methods=['GET'])
def sim_strategy_analytics():
    """Get strategy analytics for the Strategy Lab display.

    Returns:
        {
            strategies: [{id, name, type, logic, ...}],
            performance: {strategy_id: {win_rate, avg_return, total_uses, ...}},
            aggregate: {total_strategies, active_count, avg_win_rate, ...},
            type_labels: {buy_signal: '📈 买入信号', ...}
        }
    """
    db = get_thread_db(DOMAIN_TRADING)
    from lib.trading.llm_simulator import _STRATEGY_TYPE_LABELS, _load_strategy_analytics
    analytics = _load_strategy_analytics(db)
    analytics['type_labels'] = _STRATEGY_TYPE_LABELS
    return jsonify(analytics)


@trading_simulator_bp.route('/api/v1/trading/sim/coverage', methods=['GET'])
def sim_data_coverage():
    db = get_thread_db(DOMAIN_TRADING)
    symbols = request.args.get('symbols', '').split(',')
    symbols = [s.strip() for s in symbols if s.strip()]
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    if not symbols or not start_date or not end_date:
        return api_bad_request('symbols, start_date, end_date are required')
    from lib.trading.historical_data import get_data_coverage_report
    report = get_data_coverage_report(db, symbols, start_date, end_date)
    return jsonify(report)
