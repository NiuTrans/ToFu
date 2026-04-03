"""routes/trading_decision.py — AI decision, streaming recommend, trade queue, rollback."""

import json
import re
import time
from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from lib.database import DOMAIN_TRADING, get_db, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

trading_decision_bp = Blueprint('trading_decision', __name__)


# ── News cache for recommendation prompts ──
_news_cache = {'items': [], 'ts': 0}


def _gather_news_from_db():
    """PRIMARY: gather recent news from DB intel cache."""
    try:
        db = get_db(DOMAIN_TRADING)
        rows = db.execute(
            "SELECT title, summary, category, source_url, fetched_at "
            "FROM trading_intel_cache ORDER BY fetched_at DESC LIMIT 60"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            items.append({
                'title': d.get('title', ''),
                'snippet': d.get('summary', '')[:200],
                'url': d.get('source_url', ''),
                'category': d.get('category', ''),
            })
        if items:
            logger.debug('[Trading] gathered %d news items from DB intel cache', len(items))
        return items
    except Exception as e:
        logger.warning('[Trading] DB intel gather error: %s', e, exc_info=True)
        return []


def _gather_news_cached():
    """Gather market news: DB first (instant), live search fallback."""
    import time as _t
    if _news_cache['items'] and (_t.time() - _news_cache['ts']) < 300:
        return _news_cache['items']
    items = _gather_news_from_db()
    if len(items) < 5:
        try:
            from lib.trading import _check_external_network
            if _check_external_network():
                from concurrent.futures import ThreadPoolExecutor, as_completed

                from lib.search import perform_web_search
                queries = ['A股ETF和股票市场', '投资市场动态', '宏观经济']
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futs = {executor.submit(perform_web_search, q, 3): q for q in queries}
                    for fut in as_completed(futs, timeout=5):
                        try:
                            for r in fut.result():
                                items.append({
                                    'title': r.get('title', ''),
                                    'snippet': r.get('snippet', ''),
                                    'url': r.get('url', ''),
                                    'source': futs[fut]
                                })
                        except Exception as e:
                            logger.warning('Web search future failed for news gathering: %s', e, exc_info=True)
        except Exception as e:
            logger.warning('Live news search fallback failed (non-critical): %s', e, exc_info=True)
    _news_cache['items'] = items
    _news_cache['ts'] = _t.time()
    return items


def _build_recommend_prompt(holdings_ctx, cash, news_items, strategies_ctx):
    """Build the recommendation prompt."""
    news_text = "\n".join([f"- [{n.get('category', '新闻')}] {n['title']}: {n['snippet']}" for n in news_items[:20]])
    return f"""你是一位资深的投资交易顾问。请根据以下信息，给出详细的投资分析和建议。

## 市场动态
{news_text if news_text.strip() else "（暂未获取到实时市场数据）"}

## 用户当前持仓
{holdings_ctx if holdings_ctx else "用户暂无持仓。"}

## 可用资金
¥{cash:,.2f}

{strategies_ctx}

请给出详细的分析和建议，必须包含以下部分：

### 1. 持仓诊断
对用户现有每只标的的评估（如果有持仓），包括是否建议继续持有、减仓或加仓。

### 2. 新标的推荐
根据市场环境和用户资金情况，推荐2-3只具体ETF或股票（给出标的代码和名称），说明推荐逻辑。

### 3. 操作计划
具体的买入/卖出/调仓建议，包括建议金额。

### 4. 风险评估
当前市场主要风险点和应对策略。

### 5. 策略总结
将本次分析凝练为1-2条可执行的投资策略（名称 + 核心逻辑 + 触发条件 + 适用场景）。
用 JSON 数组格式输出在 <strategies> 标签中，例如：
<strategies>
[{{"name":"低位分批建仓","type":"buy_signal","logic":"沪深300跌破3200点时分3批建仓宽基ETFETF","scenario":"市场恐慌性下跌","assets":"510300,159919"}}]
</strategies>

请深度思考后给出专业、有依据的建议。使用 Markdown 格式。"""


def _auto_save_strategies(db, content):
    """Extract <strategies> from AI output and upsert them."""
    m = re.search(r'<strategies>\s*(\[.*?\])\s*</strategies>', content, re.DOTALL)
    if not m:
        return
    try:
        strats = json.loads(m.group(1))
    except Exception as e:
        logger.warning('Failed to parse <strategies> JSON from AI output: %s', e, exc_info=True)
        return
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for s in strats:
        if not isinstance(s, dict) or not s.get('name'):
            continue
        existing = db.execute('SELECT id FROM trading_strategies WHERE name=?', (s['name'],)).fetchone()
        if existing:
            db.execute('''UPDATE trading_strategies SET
                          logic=?, scenario=?, assets=?, type=?, updated_at=?, source=?
                          WHERE id=?''',
                       (s.get('logic', ''), s.get('scenario', ''), s.get('assets', ''),
                        s.get('type', 'buy_signal'), now, 'ai', existing['id']))
        else:
            db.execute(
                'INSERT INTO trading_strategies (name,type,status,logic,scenario,assets,result,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (s['name'], s.get('type', 'buy_signal'), 'active',
                 s.get('logic', ''), s.get('scenario', ''), s.get('assets', ''),
                 '', 'ai', now, now))
    db.commit()


def _extract_and_queue_trades(db, content):
    """Extract <trades> JSON from AI output and create trade queue entries."""
    m = re.search(r'<trades>\s*(\[.*?\])\s*</trades>', content, re.DOTALL)
    if not m:
        return
    try:
        trades = json.loads(m.group(1))
    except Exception as e:
        logger.warning('Failed to parse <trades> JSON from AI output: %s', e, exc_info=True)
        return
    if not trades:
        return
    batch_id = f"batch_{int(time.time()*1000)}"
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    from lib.trading import calc_buy_fee, calc_sell_fee, fetch_asset_info
    for t in trades:
        if not isinstance(t, dict):
            continue
        code = t.get('symbol', '')
        action = t.get('action', 'buy')
        amount = float(t.get('amount') or 0)
        shares = float(t.get('shares') or 0)
        fee_amount = 0
        fee_detail = ''
        if action == 'buy' and amount > 0:
            fee_info = calc_buy_fee(code, amount)
            fee_amount = fee_info['fee_amount']
            fee_detail = f"申购费率{fee_info['fee_rate']*100:.2f}%"
        elif action == 'sell':
            h = db.execute('SELECT * FROM trading_holdings WHERE symbol=? LIMIT 1', (code,)).fetchone()
            if h:
                sell_info = calc_sell_fee(dict(h))
                fee_amount = sell_info['fee_amount']
                fee_detail = f"赎回费率{sell_info['fee_rate']*100:.2f}%（持有{sell_info['holding_days']}天）"
        info = fetch_asset_info(code) or {}
        nav = float(info.get('nav') or 0)
        db.execute(
            'INSERT INTO trading_trade_queue (batch_id,symbol,asset_name,action,shares,amount,price,est_fee,fee_detail,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (batch_id, code, t.get('asset_name', info.get('name', code)), action,
             shares, amount, nav, fee_amount, fee_detail,
             t.get('reason', ''), 'pending', now))
    db.commit()
    logger.info('[Decision] queued %d trades in batch %s', len(trades), batch_id)


# ── Route handlers ──

@trading_decision_bp.route('/api/trading/recommend', methods=['POST'])
def trading_recommend():
    """Non-streaming recommendation using Opus with thinking."""
    from .trading_intel import _get_holdings_ctx, _get_strategies_ctx
    db = get_db(DOMAIN_TRADING)
    holdings_ctx = _get_holdings_ctx(db)
    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
    cash = float(cfg['value']) if cfg else 0
    strategies_ctx = _get_strategies_ctx(db)
    news_items = _gather_news_cached()
    prompt = _build_recommend_prompt(holdings_ctx, cash, news_items, strategies_ctx)

    from lib.llm_dispatch import smart_chat
    news_text = "\n".join([f"- [{n['title']}] {n['snippet']}" for n in news_items[:20]])
    logger.debug('[FundRecommend] non-stream, prompt=%d chars', len(prompt))
    content, _usage = smart_chat(
        messages=[{'role': 'user', 'content': prompt}],
        max_tokens=16384, temperature=1.0,
        capability='thinking',
        log_prefix='[FundRecommend]',
    )
    now = int(time.time() * 1000)
    db.execute('INSERT INTO trading_recommendations (content, market_context, created_at) VALUES (?, ?, ?)',
               (content, news_text, now))
    db.commit()
    _auto_save_strategies(db, content)
    return jsonify({'recommendation': content})


@trading_decision_bp.route('/api/trading/recommend/stream', methods=['POST'])
def trading_recommend_stream():
    """Streaming recommendation with thinking display."""
    from lib.llm_dispatch import dispatch_stream

    from .trading_intel import _get_holdings_ctx, _get_strategies_ctx

    db = get_db(DOMAIN_TRADING)
    req_data = request.get_json(silent=True) or {}
    holdings_ctx = _get_holdings_ctx(db)
    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
    cash = float(cfg['value']) if cfg else 0
    strategies_ctx = _get_strategies_ctx(db)

    # Strategy group context
    group_id = req_data.get('strategy_group_id')
    group_ctx = ""
    group_name = ""
    if group_id:
        grp = db.execute("SELECT * FROM trading_strategy_groups WHERE id=?", (group_id,)).fetchone()
        if grp:
            grp = dict(grp)
            group_name = grp['name']
            group_ctx = f"\n## 使用策略组: {grp['name']}\n描述: {grp['description']}\n风险级别: {grp.get('risk_level', 'medium')}\n"
            try:
                sids = json.loads(grp.get('strategy_ids', '[]'))
            except (json.JSONDecodeError, TypeError):
                logger.warning('[Decision] corrupt strategy_ids JSON in group %s', grp.get('id'), exc_info=True)
                sids = []
            if sids:
                # Safe: ph is purely '?,?,?' placeholders (one per integer sid)
                ph = ','.join('?' * len(sids))
                gstrats = [dict(r) for r in db.execute('SELECT * FROM trading_strategies WHERE id IN (' + ph + ')', sids).fetchall()]
                group_ctx += "组内策略:\n" + "\n".join([f"- {s['name']}: {s['logic']}" for s in gstrats])

    news_items = _gather_news_cached()
    from lib.trading import build_intel_context
    intel_ctx, _intel_n = build_intel_context(db)

    from lib.trading import calc_sell_fee, fetch_trading_fees
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

    def generate():
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'
        import queue as _q
        import threading as _th
        q = _q.Queue()
        full_content = ''

        def _run():
            try:
                dispatch_stream(messages,
                                on_thinking=lambda t: q.put(('think', t)),
                                on_content=lambda t: q.put(('text', t)),
                                max_tokens=16384, temperature=1,
                                thinking_enabled=True, preset='max',
                                capability='thinking',
                                log_prefix='[Recommend]')
            except Exception as e:
                logger.error('[Recommend] LLM streaming call failed: %s', e, exc_info=True)
                q.put(('error', str(e)))
            finally:
                q.put(None)

        t = _th.Thread(target=_run, daemon=True)
        t.start()
        while True:
            ev = q.get()
            if ev is None:
                _db = get_thread_db(DOMAIN_TRADING)
                today = datetime.now().strftime('%Y-%m-%d')
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                try:
                    _db.execute('INSERT OR REPLACE INTO trading_daily_briefing (date,content,news_json,created_at) VALUES (?,?,?,?)',
                                (today, full_content, json.dumps(news_items[:20], ensure_ascii=False), now_str))
                    _db.commit()
                except Exception as e:
                    logger.warning('[Recommend] Failed to save daily briefing to DB: %s', e, exc_info=True)
                _extract_and_queue_trades(_db, full_content)
                _auto_save_strategies(_db, full_content)
                yield "data: [DONE]\n\n"
                break
            kind, val = ev
            if kind == 'text':
                full_content += val
                yield f"data: {json.dumps({'type':'content','text': val}, ensure_ascii=False)}\n\n"
            elif kind == 'think':
                yield f"data: {json.dumps({'type':'thinking','text': val}, ensure_ascii=False)}\n\n"
            elif kind == 'error':
                yield f"data: {json.dumps({'type':'error','text': val}, ensure_ascii=False)}\n\n"
                break

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


@trading_decision_bp.route('/api/trading/briefing/refresh', methods=['POST'])
def asset_briefing_refresh():
    """Alias for streaming recommend — generates today's briefing."""
    return trading_recommend_stream()


@trading_decision_bp.route('/api/trading/briefing', methods=['GET'])
def asset_briefing_get():
    """Get today's cached briefing."""
    db = get_db(DOMAIN_TRADING)
    today = datetime.now().strftime('%Y-%m-%d')
    row = db.execute('SELECT * FROM trading_daily_briefing WHERE date=?', (today,)).fetchone()
    if row:
        row = dict(row)
        return jsonify({'briefing': row['content'], 'date': row['date'], 'created_at': row['created_at']})
    return jsonify({'briefing': None, 'date': today})


@trading_decision_bp.route('/api/trading/decisions', methods=['GET'])
def trading_decisions_list():
    db = get_db(DOMAIN_TRADING)
    rows = db.execute('SELECT * FROM trading_decision_history ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'decisions': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/trading/decisions/<int:did>/results', methods=['POST'])
def trading_decisions_record_results(did):
    """Record actual results for a past decision."""
    db = get_db(DOMAIN_TRADING)
    data = request.get_json(silent=True) or {}
    db.execute('UPDATE trading_decision_history SET actual_result=? WHERE id=?',
               (data.get('actual_result', ''), did))
    db.commit()
    return jsonify({'ok': True})


# ── Trade Queue ──

@trading_decision_bp.route('/api/trading/trades', methods=['GET'])
def trading_trades_list():
    db = get_db(DOMAIN_TRADING)
    status = request.args.get('status', '')
    if status:
        rows = db.execute('SELECT * FROM trading_trade_queue WHERE status=? ORDER BY created_at DESC', (status,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM trading_trade_queue ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'trades': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/trading/trades/execute', methods=['POST'])
def trading_trades_execute():
    """Execute trades."""
    data = request.get_json(silent=True) or {}
    trade_ids = data.get('trade_ids', [])

    raw_trades = data.get('trades', [])
    if raw_trades and not trade_ids:
        batch_id = data.get('batch_id', datetime.now().strftime('%Y%m%d%H%M%S'))
        db = get_db(DOMAIN_TRADING)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for t in raw_trades:
            db.execute(
                'INSERT INTO trading_trade_queue (batch_id,symbol,asset_name,action,shares,amount,price,est_fee,fee_detail,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (batch_id, t.get('symbol', ''), t.get('asset_name', ''), t.get('action', 'buy'),
                 float(t.get('shares', 0)), float(t.get('amount', 0)), float(t.get('price', 0)),
                 0, '{}', t.get('reason', ''), 'pending', now))
        db.commit()
        rows = db.execute('SELECT id FROM trading_trade_queue WHERE batch_id=? AND status=?', (batch_id, 'pending')).fetchall()
        trade_ids = [r['id'] for r in rows]

    if not trade_ids:
        return jsonify({'error': 'No trades selected'}), 400

    db = get_db(DOMAIN_TRADING)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    executed = []
    errors = []

    for tid in trade_ids:
        trade = db.execute('SELECT * FROM trading_trade_queue WHERE id=? AND status=?', (tid, 'pending')).fetchone()
        if not trade:
            errors.append(f'Trade {tid} not found or already processed')
            continue
        trade = dict(trade)
        try:
            if trade['action'] == 'buy':
                from lib.trading import fetch_asset_info
                info = fetch_asset_info(trade['symbol'])
                nav = float(info.get('nav', trade['price'])) if info.get('nav') else trade['price']
                shares = trade['shares'] if trade['shares'] > 0 else (trade['amount'] / nav if nav > 0 else 0)
                db.execute(
                    "INSERT INTO trading_holdings (symbol,asset_name,shares,buy_price,buy_date,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (trade['symbol'], trade['asset_name'], round(shares, 2), nav,
                     datetime.now().strftime('%Y-%m-%d'), f"[自动] {trade['reason']}",
                     int(time.time()*1000), int(time.time()*1000)))
                cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
                cash = float(cfg['value']) if cfg else 0
                new_cash = max(0, cash - trade['amount'] - trade['est_fee'])
                db.execute("INSERT OR REPLACE INTO trading_config (key,value) VALUES ('available_cash',?)", (str(new_cash),))
            elif trade['action'] == 'sell':
                h = db.execute('SELECT * FROM trading_holdings WHERE symbol=? LIMIT 1', (trade['symbol'],)).fetchone()
                if h:
                    h = dict(h)
                    sell_shares = trade['shares'] if trade['shares'] > 0 else h['shares']
                    remaining = h['shares'] - sell_shares
                    if remaining <= 0.01:
                        db.execute('DELETE FROM trading_holdings WHERE id=?', (h['id'],))
                    else:
                        db.execute('UPDATE trading_holdings SET shares=?,updated_at=? WHERE id=?',
                                   (remaining, int(time.time()*1000), h['id']))
                    from lib.trading import get_latest_price
                    nav_val, _ = get_latest_price(trade['symbol'])
                    proceed = sell_shares * (nav_val or trade['price']) - trade['est_fee']
                    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
                    cash = float(cfg['value']) if cfg else 0
                    db.execute("INSERT OR REPLACE INTO trading_config (key,value) VALUES ('available_cash',?)", (str(cash + proceed),))
            db.execute('UPDATE trading_trade_queue SET status=?,executed_at=? WHERE id=?', ('executed', now, tid))
            executed.append(tid)
        except Exception as e:
            logger.error('[Decision] Trade execution failed for trade %s: %s', tid, e, exc_info=True)
            errors.append(f'Trade {tid}: {str(e)}')

    db.commit()
    return jsonify({'ok': True, 'executed': executed, 'errors': errors})


def _rollback_trade(db, trade, now):
    """Rollback a single executed trade. Returns True on success."""
    trade = dict(trade) if not isinstance(trade, dict) else trade
    if trade['action'] == 'buy':
        h = db.execute(
            "SELECT * FROM trading_holdings WHERE symbol=? AND note LIKE '%自动%' ORDER BY created_at DESC LIMIT 1",
            (trade['symbol'],)).fetchone()
        if h:
            db.execute('DELETE FROM trading_holdings WHERE id=?', (h['id'],))
        cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
        cash = float(cfg['value']) if cfg else 0
        db.execute("INSERT OR REPLACE INTO trading_config (key,value) VALUES ('available_cash',?)",
                   (str(cash + trade['amount'] + trade['est_fee']),))
    elif trade['action'] == 'sell':
        from lib.trading import get_latest_price
        nav_val, _ = get_latest_price(trade['symbol'])
        shares = trade['shares'] if trade['shares'] > 0 else 0
        db.execute(
            "INSERT INTO trading_holdings (symbol,asset_name,shares,buy_price,buy_date,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (trade['symbol'], trade['asset_name'], shares, trade['price'],
             datetime.now().strftime('%Y-%m-%d'), "[回滚] 恢复已卖出持仓",
             int(time.time()*1000), int(time.time()*1000)))
        proceed = shares * (nav_val or trade['price']) - trade['est_fee']
        cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
        cash = float(cfg['value']) if cfg else 0
        db.execute("INSERT OR REPLACE INTO trading_config (key,value) VALUES ('available_cash',?)",
                   (str(max(0, cash - proceed)),))
    db.execute('UPDATE trading_trade_queue SET status=?,rolled_back_at=? WHERE id=?',
               ('rolled_back', now, trade['id']))


@trading_decision_bp.route('/api/trading/trades/rollback', methods=['POST'])
def trading_trades_rollback():
    """Rollback executed trades."""
    data = request.get_json(silent=True) or {}
    trade_ids = data.get('trade_ids', [])
    if not trade_ids:
        return jsonify({'error': 'No trades selected'}), 400

    db = get_db(DOMAIN_TRADING)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rolled_back = []
    errors = []

    for tid in trade_ids:
        trade = db.execute('SELECT * FROM trading_trade_queue WHERE id=? AND status=?', (tid, 'executed')).fetchone()
        if not trade:
            errors.append(f'Trade {tid} not found or not in executed state')
            continue
        try:
            _rollback_trade(db, dict(trade), now)
            rolled_back.append(tid)
        except Exception as e:
            logger.error('[Decision] Trade rollback failed for trade %s: %s', tid, e, exc_info=True)
            errors.append(f'Trade {tid}: {str(e)}')

    db.commit()
    return jsonify({'ok': True, 'rolled_back': rolled_back, 'errors': errors})


@trading_decision_bp.route('/api/trading/trades/<int:tid>', methods=['DELETE'])
def trading_trades_dismiss(tid):
    db = get_db(DOMAIN_TRADING)
    db.execute('UPDATE trading_trade_queue SET status=? WHERE id=? AND status=?', ('dismissed', tid, 'pending'))
    db.commit()
    return jsonify({'ok': True})


@trading_decision_bp.route('/api/trading/trades/rollback-batch', methods=['POST'])
def trading_trades_rollback_batch():
    """Rollback all executed trades for a batch_id (decision rollback)."""
    data = request.get_json(silent=True) or {}
    batch_id = data.get('batch_id', '')
    if not batch_id:
        return jsonify({'error': 'batch_id required'}), 400

    db = get_db(DOMAIN_TRADING)
    trades = db.execute('SELECT * FROM trading_trade_queue WHERE batch_id=? AND status=?', (batch_id, 'executed')).fetchall()
    if not trades:
        return jsonify({'error': 'No executed trades found for this batch'}), 404

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rolled_back = []
    errors = []
    for trade in trades:
        try:
            _rollback_trade(db, dict(trade), now)
            rolled_back.append(dict(trade)['id'])
        except Exception as e:
            logger.error('[Decision] Batch rollback failed for trade %s: %s', dict(trade).get('id', '?'), e, exc_info=True)
            errors.append(f'Trade {dict(trade)["id"]}: {str(e)}')

    db.execute('UPDATE trading_decision_history SET status=? WHERE batch_id=?', ('rolled_back', batch_id))
    db.commit()
    return jsonify({'ok': True, 'rolled_back': rolled_back, 'errors': errors})


@trading_decision_bp.route('/api/trading/fees/<code>', methods=['GET'])
def trading_fees_get(code):
    from lib.trading import fetch_trading_fees
    fees = fetch_trading_fees(code)
    return jsonify(fees)
