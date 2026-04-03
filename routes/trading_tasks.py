"""routes/trading_tasks.py — Background task API for trading long-running operations.

Provides:
  POST /api/trading/tasks/submit          — Submit a new background task
  GET  /api/trading/tasks/<id>/poll       — Poll for incremental output
  GET  /api/trading/tasks/<id>/result     — Get final result
  POST /api/trading/tasks/<id>/cancel     — Cancel a running task
  GET  /api/trading/tasks/active          — List active tasks
"""

import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from lib.database import DOMAIN_TRADING, get_db, get_thread_db
from lib.log import get_logger
from lib.trading_tasks import cancel_task, get_task, list_active_tasks, poll_task, submit_task

logger = get_logger(__name__)

trading_tasks_bp = Blueprint('trading_tasks', __name__)


# ══════════════════════════════════════════
#  Submit Task — universal entry point
# ══════════════════════════════════════════

@trading_tasks_bp.route('/api/trading/tasks/submit', methods=['POST'])
def tasks_submit():
    """Submit a long-running trading task.

    Body: { "type": "decision"|"autopilot"|"intel_backtest",
            ...extra params depending on type... }
    Returns: { "task_id": "...", "status": "running" }
    """
    data = request.get_json(silent=True) or {}
    task_type = data.get('type', '')

    params = data.get('params', {})

    if task_type == 'decision':
        return _submit_decision(params)
    elif task_type == 'autopilot':
        return _submit_autopilot(params)
    elif task_type == 'intel_backtest':
        return _submit_intel_backtest(params)
    else:
        return jsonify({'error': f'Unknown task type: {task_type}'}), 400


# ══════════════════════════════════════════
#  Poll / Result / Cancel / List
# ══════════════════════════════════════════

@trading_tasks_bp.route('/api/trading/tasks/<task_id>/poll', methods=['GET'])
def tasks_poll(task_id):
    """Poll for new output chunks.

    Query params:
      cursor (int): last known chunk index (default 0)

    Returns: { task_id, status, cursor, chunks: [{type, text}] }
    """
    cursor = request.args.get('cursor', 0, type=int)
    result = poll_task(task_id, cursor)
    if result is None:
        # Return a synthetic "done" response instead of 404.
        # This ensures old cached JS (which doesn't handle 404)
        # will still stop polling via its onDone callback.
        logger.info('[poll] Task %s not found — sending synthetic done to stop client polling', task_id)
        return jsonify({
            'task_id': task_id,
            'status':  'done',
            'cursor':  0,
            'chunks':  [{'type': 'content', 'text': '⚠️ 任务已过期或不存在，请重新提交。'}],
        })
    return jsonify(result)


@trading_tasks_bp.route('/api/trading/tasks/<task_id>/result', methods=['GET'])
def tasks_result(task_id):
    """Get final result of a completed task."""
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({
        'task_id': task.task_id,
        'task_type': task.task_type,
        'status': task.status,
        'result': task.result,
        'thinking': task.thinking,
        'error': task.error,
    })


@trading_tasks_bp.route('/api/trading/tasks/<task_id>/cancel', methods=['POST'])
def tasks_cancel(task_id):
    """Cancel a running task."""
    ok = cancel_task(task_id)
    if ok:
        return jsonify({'ok': True, 'task_id': task_id})
    return jsonify({'error': 'Task not found or not running'}), 404


@trading_tasks_bp.route('/api/trading/tasks/active', methods=['GET'])
def tasks_active():
    """List all active (and recently completed) tasks."""
    task_type = request.args.get('type')
    tasks = list_active_tasks(task_type)
    return jsonify({'tasks': tasks})


# ══════════════════════════════════════════
#  Task Runners
# ══════════════════════════════════════════

def _submit_decision(data):
    """Submit an AI decision/recommendation task."""
    # Pre-gather all DB data in the request thread (Flask context available)
    from lib.llm_dispatch import dispatch_stream
    from lib.trading import calc_sell_fee, fetch_trading_fees
    from routes.trading_decision import _auto_save_strategies, _extract_and_queue_trades, _gather_news_cached
    from routes.trading_intel import _get_holdings_ctx, _get_strategies_ctx

    db = get_db(DOMAIN_TRADING)
    holdings_ctx = _get_holdings_ctx(db)
    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
    cash = float(cfg['value']) if cfg else 0
    strategies_ctx = _get_strategies_ctx(db)

    group_id = data.get('strategy_group_id')
    group_ctx = ""
    if group_id:
        grp = db.execute("SELECT * FROM trading_strategy_groups WHERE id=?", (group_id,)).fetchone()
        if grp:
            grp = dict(grp)
            group_ctx = f"\n## 使用策略组: {grp['name']}\n描述: {grp['description']}\n风险级别: {grp.get('risk_level', 'medium')}\n"
            try:
                sids = json.loads(grp.get('strategy_ids', '[]'))
            except (json.JSONDecodeError, TypeError):
                logger.warning('[FundTasks] corrupt strategy_ids JSON in group %s', grp.get('id'), exc_info=True)
                sids = []
            if sids:
                ph = ','.join('?' * len(sids))
                gstrats = [dict(r) for r in db.execute(f'SELECT * FROM trading_strategies WHERE id IN ({ph})', sids).fetchall()]
                group_ctx += "组内策略:\n" + "\n".join([f"- {s['name']}: {s['logic']}" for s in gstrats])

    news_items = _gather_news_cached()
    intel_rows = db.execute(
        "SELECT * FROM trading_intel_cache WHERE expires_at > ? ORDER BY relevance_score DESC LIMIT 20",
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
    ).fetchall()
    intel_ctx = ''
    if intel_rows:
        intel_ctx = "\n## 情报分析缓存（已分析的市场情报）\n"
        for r in intel_rows:
            r = dict(r)
            intel_ctx += f"- [{r['category']}] {r['title']}: {r['summary']}\n"

    fee_ctx = "\n## 费率信息\n"
    for h_row in db.execute('SELECT * FROM trading_holdings').fetchall():
        h = dict(h_row)
        fees = fetch_trading_fees(h['symbol'])
        sell_info = calc_sell_fee(h)
        fee_ctx += f"- {h['symbol']}: 申购费{fees['buy_fee_rate']*100:.2f}% | 管理费{fees['management_fee']*100:.2f}%/年 | 当前赎回费{sell_info['fee_rate']*100:.2f}%（持有{sell_info['holding_days']}天）\n"

    news_text = "\n".join([f"- [{n['title']}] {n['snippet']}" for n in news_items[:20]])

    prompt = f"""你是一位资深的投资交易顾问，集市场分析师与交易执行顾问于一身。请一次性完成以下全部内容：

## 市场动态（实时新闻）
{news_text if news_text.strip() else "（暂未获取到实时新闻）"}
{intel_ctx}

## 用户持仓
{holdings_ctx if holdings_ctx else "用户暂无持仓。"}

## 可支配资金
¥{cash:,.2f}
{fee_ctx}

## 用户策略
{strategies_ctx if strategies_ctx else "暂无自定义策略。"}
{group_ctx}

## 要求
请按以下结构输出完整的决策报告：

### 一、市场速览
简要分析当前市场环境、关键指标、政策动向（3-5个要点）。

### 二、持仓诊断
逐一分析每只持仓标的的状态、风险、盈亏表现。注意赎回费率对卖出时机的影响。

### 三、操作建议
结合策略组和费率信息，给出具体的买入/卖出/调仓建议。**必须考虑赎回费率**——如果某只标的赎回费较高，需评估是否值得承担费用。

### 四、可执行交易清单
请在下方以 JSON 格式输出具体可执行的交易指令：
<trades>
[{{"action":"buy/sell/rebalance","symbol":"标的代码","asset_name":"标的名称","amount":金额,"shares":份额,"reason":"一句话理由"}}]
</trades>

如果不需要任何操作，输出空数组 <trades>[]</trades>。

请深度思考后给出专业、有依据的建议。使用 Markdown 格式。"""

    messages = [{'role': 'user', 'content': prompt}]

    # Capture news_items for post-processing
    _news = news_items[:20]

    def run(task):
        task.add_chunk('phase', '正在调用AI模型 [dispatch]...')
        try:
            dispatch_stream(messages,
                            on_thinking=lambda t: task.add_chunk('thinking', t),
                            on_content=lambda t: task.add_chunk('content', t),
                            max_tokens=16384, temperature=1,
                            capability='thinking',
                            log_prefix='[Decision-BG]')
        except Exception as e:
            logger.error('[Decision-BG] LLM call failed: %s', e, exc_info=True)
            task.finish(error=str(e))
            return
        # Post-processing: save briefing, extract trades, save strategies
        try:
            _db = get_thread_db(DOMAIN_TRADING)
            today = datetime.now().strftime('%Y-%m-%d')
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _db.execute('INSERT OR REPLACE INTO trading_daily_briefing (date,content,news_json,created_at) VALUES (?,?,?,?)',
                        (today, task.result, json.dumps(_news, ensure_ascii=False), now_str))
            _db.commit()
            _extract_and_queue_trades(_db, task.result)
            _auto_save_strategies(_db, task.result)
        except Exception as e:
            logger.error('[Decision-BG] Post-processing error: %s', e, exc_info=True)
        task.finish()

    task_id = submit_task('decision', run, params={
        'strategy_group_id': group_id,
        'cash': cash,
    })
    return jsonify({'task_id': task_id, 'status': 'running'})


def _submit_autopilot(data):
    """Submit an autopilot analysis cycle task."""
    from lib.llm_dispatch import dispatch_stream
    from lib.trading_autopilot import (
        _apply_strategy_updates,
        _store_cycle_result,
        build_autopilot_streaming_body,
        parse_autopilot_result,
    )
    from routes.trading_decision import _gather_news_cached

    db = get_db(DOMAIN_TRADING)
    news = _gather_news_cached()

    # Get cycle number
    cnt_row = db.execute('SELECT COUNT(*) as cnt FROM trading_autopilot_cycles').fetchone()
    cycle_number = (cnt_row['cnt'] if cnt_row else 0) + 1

    body, context = build_autopilot_streaming_body(db, news_items=news, cycle_number=cycle_number)

    cycle_id = f"autopilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _context = context  # Capture for closure

    def run(task):
        task.add_chunk('phase', '正在启动自我进化分析...')
        try:
            joined = ''

            def on_thinking(chunk):
                task.add_chunk('thinking', chunk)

            def on_content(chunk):
                nonlocal joined
                joined += chunk
                task.add_chunk('content', chunk)

            _msg, _finish, _usage = dispatch_stream(
                body,
                on_thinking=on_thinking,
                on_content=on_content,
                abort_check=lambda: task.cancelled,
                prefer_model=body.get('model', ''),
                log_prefix='[Autopilot-BG]',
            )

            # Parse and store result
            try:
                _db = get_thread_db(DOMAIN_TRADING)
                parsed = parse_autopilot_result(joined) or {}
                _store_cycle_result(
                    _db, cycle_id, cycle_number, joined,
                    parsed, _context.get('kpi_evaluations', {}),
                    _context.get('correlations', [])
                )
                _apply_strategy_updates(_db, parsed.get('strategy_updates', []))
                _db.commit()
                # Add structured data as a special chunk for the frontend
                task.add_chunk('autopilot_result', json.dumps({
                    'cycle_id': cycle_id,
                    'recommendations': parsed.get('position_recommendations', parsed.get('recommendations', [])),
                    'risk_factors': parsed.get('risk_factors', []),
                    'strategy_updates': parsed.get('strategy_updates', []),
                    'market_outlook': parsed.get('market_outlook', ''),
                    'confidence_score': parsed.get('confidence_score', 0),
                    'reasoning_chain': parsed.get('reasoning_chain', []),
                    'debate_verdict': parsed.get('debate_verdict', {}),
                    'next_review': parsed.get('next_review', ''),
                }, ensure_ascii=False))
            except Exception as e:
                logger.error('[Autopilot-BG] Parse/store error: %s', e, exc_info=True)

        except Exception as e:
            logger.error('[Autopilot-BG] LLM call failed: %s', e, exc_info=True)
            task.finish(error=str(e))
            return
        task.finish()

    task_id = submit_task('autopilot', run, params={
        'cycle_id': cycle_id,
        'cycle_number': cycle_number,
    })
    return jsonify({'task_id': task_id, 'status': 'running', 'cycle_id': cycle_id})


def _submit_intel_backtest(data):
    """Submit an intel-driven backtest analysis task."""
    from lib.llm_dispatch import dispatch_stream as _dispatch_stream

    db = get_db(DOMAIN_TRADING)
    strategy_group_id = data.get('strategy_group_id')
    existing_assets = data.get('existing_assets', [])

    # Build context
    grp = db.execute("SELECT * FROM trading_strategy_groups WHERE id=?", (strategy_group_id,)).fetchone()
    if not grp:
        return jsonify({'error': 'Strategy group not found'}), 404
    grp = dict(grp)
    try:
        sids = json.loads(grp.get('strategy_ids', '[]'))
    except (json.JSONDecodeError, TypeError):
        logger.warning('[FundTasks] corrupt strategy_ids JSON in group %s', grp.get('id'), exc_info=True)
        sids = []
    strategies = []
    if sids:
        ph = ','.join('?' * len(sids))
        strategies = [dict(r) for r in db.execute(f'SELECT * FROM trading_strategies WHERE id IN ({ph})', sids).fetchall()]

    intel_rows = db.execute(
        "SELECT * FROM trading_intel_cache WHERE expires_at > ? ORDER BY relevance_score DESC LIMIT 50",
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),)
    ).fetchall()

    existing_ctx = ""
    if existing_assets:
        existing_ctx = "\n已有持仓:\n" + "\n".join([
            f"- {f.get('symbol', '?')}: {f.get('shares', 0)}份 @ {f.get('buy_price', 0)}"
            for f in existing_assets
        ])

    intel_summary = ""
    for item in [dict(r) for r in intel_rows[:50]]:
        analysis = item.get('analysis', '')
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except Exception as e:
                analysis = {}
                logger.warning('Failed to parse intel analysis JSON for task context: %s', e, exc_info=True)
        snippet = ''
        if isinstance(analysis, dict):
            snippet = analysis.get('summary', analysis.get('key_points', ''))
        elif isinstance(analysis, str):
            snippet = analysis[:200]
        intel_summary += f"\n- [{item.get('category','?')}] {item.get('title','?')}: {snippet}"

    strat_ctx = "\n".join([f"- {s['name']}: {s['logic']} (场景: {s.get('scenario', '通用')})" for s in strategies])

    prompt = f"""你是一位资深量化投资分析师。请根据以下市场情报和策略组，推荐最适合当前市场的资产组合，并给出详细回测建议。

## 策略组: {grp['name']}
{grp.get('description', '')}
风险级别: {grp.get('risk_level', 'medium')}

## 策略:
{strat_ctx}
{existing_ctx}

## 市场情报摘要:
{intel_summary if intel_summary.strip() else "暂无情报数据"}

## 要求:
1. 基于情报分析，推荐3-5只具体ETF或股票（给出代码和名称）
2. 说明每只标的的推荐逻辑（与哪条情报/策略关联）
3. 给出建议配置比例
4. 预期收益区间和风险评估
5. 具体的建仓时机建议

请使用 Markdown 格式。深度思考后给出专业建议。"""

    messages = [{'role': 'user', 'content': prompt}]

    def run(task):
        task.add_chunk('phase', '正在基于情报构建回测分析 [dispatch]...')
        try:
            _dispatch_stream(messages,
                             on_thinking=lambda t: task.add_chunk('thinking', t),
                             on_content=lambda t: task.add_chunk('content', t),
                             max_tokens=8192, temperature=1,
                             capability='thinking',
                             log_prefix='[IntelBacktest-BG]')
        except Exception as e:
            logger.error('[IntelBacktest-BG] LLM call failed: %s', e, exc_info=True)
            task.finish(error=str(e))
            return
        task.finish()

    task_id = submit_task('intel_backtest', run, params={
        'strategy_group_id': strategy_group_id,
    })
    return jsonify({'task_id': task_id, 'status': 'running'})
