"""routes/trading_decision.py — Trade queue, execution, rollback, fees, briefing.

Decision-making endpoints (``/api/trading/recommend`` sync and stream) have been
removed — the frontend exclusively uses ``/api/trading/brain/stream`` (brain.js).
News gathering has been moved to ``lib/trading/news_gathering.py``.

Remaining endpoints:
  - GET  /api/trading/briefing          — cached daily briefing
  - GET  /api/trading/decisions         — decision history
  - POST /api/trading/decisions/<id>/results — record actual results
  - GET  /api/trading/trades            — trade queue listing
  - POST /api/trading/trades/execute    — execute trades
  - POST /api/trading/trades/rollback   — rollback executed trades
  - DEL  /api/trading/trades/<id>       — dismiss pending trade
  - POST /api/trading/trades/rollback-batch — batch rollback
  - GET  /api/trading/fees/<code>       — fee info
"""

import asyncio
import json
import re
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

from tofu_trading.web.v1.decision import api_v1_trading_decision_bp as trading_decision_bp  # noqa: E402
# (alias kept for back-compat with `from tofu_trading.web.handlers.trading_decision import trading_decision_bp` callers)


def _auto_save_strategies(db, content, uid):
    """Extract <strategies> from AI output and upsert them for this user."""
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
        existing = db.execute('SELECT id FROM trading_strategies WHERE name=? AND user_id=?',
                              (s['name'], uid)).fetchone()
        if existing:
            db.execute('''UPDATE trading_strategies SET
                          logic=?, scenario=?, assets=?, type=?, updated_at=?, source=?
                          WHERE id=? AND user_id=?''',
                       (s.get('logic', ''), s.get('scenario', ''), s.get('assets', ''),
                        s.get('type', 'buy_signal'), now, 'ai', existing['id'], uid))
        else:
            db.execute(
                'INSERT INTO trading_strategies (user_id,name,type,status,logic,scenario,assets,result,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (uid, s['name'], s.get('type', 'buy_signal'), 'active',
                 s.get('logic', ''), s.get('scenario', ''), s.get('assets', ''),
                 '', 'ai', now, now))
    db.commit()


def _extract_and_queue_trades(db, content, uid):
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
    from tofu_trading.trading import calc_buy_fee, calc_sell_fee, fetch_asset_info
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
            h = db.execute('SELECT * FROM trading_holdings WHERE symbol=? AND user_id=? LIMIT 1',
                           (code, uid)).fetchone()
            if h:
                sell_info = calc_sell_fee(dict(h))
                fee_amount = sell_info['fee_amount']
                fee_detail = f"赎回费率{sell_info['fee_rate']*100:.2f}%（持有{sell_info['holding_days']}天）"
        info = fetch_asset_info(code) or {}
        nav = float(info.get('nav') or 0)
        db.execute(
            'INSERT INTO trading_trade_queue (user_id,batch_id,symbol,asset_name,action,shares,amount,price,est_fee,fee_detail,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (uid, batch_id, code, t.get('asset_name', info.get('name', code)), action,
             shares, amount, nav, fee_amount, fee_detail,
             t.get('reason', ''), 'pending', now))
    db.commit()
    logger.info('[Decision] queued %d trades in batch %s', len(trades), batch_id)


# ── Route handlers ──

@trading_decision_bp.route('/api/v1/trading/briefing', methods=['GET'])
async def asset_briefing_get():
    """Get today's cached briefing."""
    uid = current_user_id()
    today = datetime.now().strftime('%Y-%m-%d')
    row = await async_fetchone('SELECT * FROM trading_daily_briefing WHERE date=? AND user_id=?',
                               (today, uid), domain=DOMAIN_TRADING)
    if row:
        row = dict(row)
        return jsonify({'briefing': row['content'], 'date': row['date'], 'created_at': row['created_at']})
    return jsonify({'briefing': None, 'date': today})


@trading_decision_bp.route('/api/v1/trading/decisions', methods=['GET'])
async def trading_decisions_list():
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT * FROM trading_decision_history WHERE user_id=? '
        'ORDER BY created_at DESC LIMIT 50', (uid,), domain=DOMAIN_TRADING)
    return jsonify({'decisions': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/v1/trading/decisions/<int:did>/results', methods=['POST'])
async def trading_decisions_record_results(did):
    """Record actual results for a past decision."""
    uid = current_user_id()
    data = await async_parse_body()
    await async_execute('UPDATE trading_decision_history SET actual_result=? WHERE id=? AND user_id=?',
                        (data.get('actual_result', ''), did, uid), domain=DOMAIN_TRADING)
    return api_ok()
# ── Trade Queue ──

@trading_decision_bp.route('/api/v1/trading/trades', methods=['GET'])
async def trading_trades_list():
    uid = current_user_id()
    status = request.args.get('status', '')
    if status:
        rows = await async_fetchall(
            'SELECT * FROM trading_trade_queue WHERE status=? AND user_id=? '
            'ORDER BY created_at DESC', (status, uid), domain=DOMAIN_TRADING)
    else:
        rows = await async_fetchall(
            'SELECT * FROM trading_trade_queue WHERE user_id=? '
            'ORDER BY created_at DESC LIMIT 50', (uid,), domain=DOMAIN_TRADING)
    return jsonify({'trades': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/v1/trading/trades/execute', methods=['POST'])
async def trading_trades_execute():
    """Execute trades."""
    uid = current_user_id()
    data = await async_parse_body()
    trade_ids = data.get('trade_ids', [])
    raw_trades = data.get('trades', [])
    batch_id_in = data.get('batch_id', datetime.now().strftime('%Y%m%d%H%M%S'))

    # The whole execution is a multi-statement, interleaved read/write workflow
    # that also calls blocking market-data helpers (fetch_asset_info /
    # get_latest_price). Run it on a single pooled connection inside a worker
    # thread so the event loop is never blocked and the connection is always
    # returned to the pool.
    def _execute():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            ids = list(trade_ids)
            if raw_trades and not ids:
                batch_id = batch_id_in
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                for t in raw_trades:
                    db.execute(
                        'INSERT INTO trading_trade_queue (user_id,batch_id,symbol,asset_name,action,shares,amount,price,est_fee,fee_detail,reason,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (uid, batch_id, t.get('symbol', ''), t.get('asset_name', ''), t.get('action', 'buy'),
                         float(t.get('shares', 0)), float(t.get('amount', 0)), float(t.get('price', 0)),
                         0, '{}', t.get('reason', ''), 'pending', now))
                db.commit()
                rows = db.execute('SELECT id FROM trading_trade_queue WHERE batch_id=? AND status=? AND user_id=?', (batch_id, 'pending', uid)).fetchall()
                ids = [r['id'] for r in rows]

            if not ids:
                return None, None  # signals bad request

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            executed = []
            errors = []

            for tid in ids:
                trade = db.execute('SELECT * FROM trading_trade_queue WHERE id=? AND status=? AND user_id=?', (tid, 'pending', uid)).fetchone()
                if not trade:
                    errors.append(f'Trade {tid} not found or already processed')
                    continue
                trade = dict(trade)
                try:
                    if trade['action'] == 'buy':
                        from tofu_trading.trading import fetch_asset_info
                        info = fetch_asset_info(trade['symbol'])
                        nav = float(info.get('nav', trade['price'])) if info.get('nav') else trade['price']
                        shares = trade['shares'] if trade['shares'] > 0 else (trade['amount'] / nav if nav > 0 else 0)
                        db.execute(
                            "INSERT INTO trading_holdings (user_id,symbol,asset_name,shares,buy_price,buy_date,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (uid, trade['symbol'], trade['asset_name'], round(shares, 2), nav,
                             datetime.now().strftime('%Y-%m-%d'), f"[自动] {trade['reason']}",
                             int(time.time()*1000), int(time.time()*1000)))
                        cfg = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'", (uid,)).fetchone()
                        cash = float(cfg['value']) if cfg else 0
                        new_cash = max(0, cash - trade['amount'] - trade['est_fee'])
                        db.execute("INSERT OR REPLACE INTO trading_user_config (user_id,key,value) VALUES (?,'available_cash',?)", (uid, str(new_cash)))
                    elif trade['action'] == 'sell':
                        h = db.execute('SELECT * FROM trading_holdings WHERE symbol=? AND user_id=? LIMIT 1', (trade['symbol'], uid)).fetchone()
                        if h:
                            h = dict(h)
                            sell_shares = trade['shares'] if trade['shares'] > 0 else h['shares']
                            remaining = h['shares'] - sell_shares
                            if remaining <= 0.01:
                                db.execute('DELETE FROM trading_holdings WHERE id=? AND user_id=?', (h['id'], uid))
                            else:
                                db.execute('UPDATE trading_holdings SET shares=?,updated_at=? WHERE id=? AND user_id=?',
                                           (remaining, int(time.time()*1000), h['id'], uid))
                            from tofu_trading.trading import get_latest_price
                            nav_val, _ = get_latest_price(trade['symbol'])
                            proceed = sell_shares * (nav_val or trade['price']) - trade['est_fee']
                            cfg = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'", (uid,)).fetchone()
                            cash = float(cfg['value']) if cfg else 0
                            db.execute("INSERT OR REPLACE INTO trading_user_config (user_id,key,value) VALUES (?,'available_cash',?)", (uid, str(cash + proceed)))
                    db.execute('UPDATE trading_trade_queue SET status=?,executed_at=? WHERE id=? AND user_id=?', ('executed', now, tid, uid))
                    executed.append(tid)
                except Exception as e:
                    logger.error('[Decision] Trade execution failed for trade %s: %s', tid, e, exc_info=True)
                    errors.append(f'Trade {tid}: {str(e)}')

            db.commit()
            return executed, errors
        finally:
            _pool_put(db)

    executed, errors = await asyncio.to_thread(_execute)
    if executed is None and errors is None:
        return api_bad_request('No trades selected')
    return api_ok({'executed': executed, 'errors': errors})
def _rollback_trade(db, trade, now, uid):
    """Rollback a single executed trade for this user. Returns True on success."""
    trade = dict(trade) if not isinstance(trade, dict) else trade
    if trade['action'] == 'buy':
        h = db.execute(
            "SELECT * FROM trading_holdings WHERE symbol=? AND user_id=? AND note LIKE '%自动%' ORDER BY created_at DESC LIMIT 1",
            (trade['symbol'], uid)).fetchone()
        if h:
            db.execute('DELETE FROM trading_holdings WHERE id=? AND user_id=?', (h['id'], uid))
        cfg = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'", (uid,)).fetchone()
        cash = float(cfg['value']) if cfg else 0
        db.execute("INSERT OR REPLACE INTO trading_user_config (user_id,key,value) VALUES (?,'available_cash',?)",
                   (uid, str(cash + trade['amount'] + trade['est_fee'])))
    elif trade['action'] == 'sell':
        from tofu_trading.trading import get_latest_price
        nav_val, _ = get_latest_price(trade['symbol'])
        shares = trade['shares'] if trade['shares'] > 0 else 0
        db.execute(
            "INSERT INTO trading_holdings (user_id,symbol,asset_name,shares,buy_price,buy_date,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, trade['symbol'], trade['asset_name'], shares, trade['price'],
             datetime.now().strftime('%Y-%m-%d'), "[回滚] 恢复已卖出持仓",
             int(time.time()*1000), int(time.time()*1000)))
        proceed = shares * (nav_val or trade['price']) - trade['est_fee']
        cfg = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'", (uid,)).fetchone()
        cash = float(cfg['value']) if cfg else 0
        db.execute("INSERT OR REPLACE INTO trading_user_config (user_id,key,value) VALUES (?,'available_cash',?)",
                   (uid, str(max(0, cash - proceed))))
    db.execute('UPDATE trading_trade_queue SET status=?,rolled_back_at=? WHERE id=? AND user_id=?',
               ('rolled_back', now, trade['id'], uid))


@trading_decision_bp.route('/api/v1/trading/trades/rollback', methods=['POST'])
async def trading_trades_rollback():
    """Rollback executed trades."""
    uid = current_user_id()
    data = await async_parse_body()
    trade_ids = data.get('trade_ids', [])
    if not trade_ids:
        return api_bad_request('No trades selected')

    # _rollback_trade takes a raw DB connection and calls blocking market-data
    # helpers (get_latest_price). Run the whole loop on a single pooled
    # connection inside a worker thread so the event loop stays free.
    def _rollback_all():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            rolled_back = []
            errors = []
            for tid in trade_ids:
                trade = db.execute('SELECT * FROM trading_trade_queue WHERE id=? AND status=? AND user_id=?', (tid, 'executed', uid)).fetchone()
                if not trade:
                    errors.append(f'Trade {tid} not found or not in executed state')
                    continue
                try:
                    _rollback_trade(db, dict(trade), now, uid)
                    rolled_back.append(tid)
                except Exception as e:
                    logger.error('[Decision] Trade rollback failed for trade %s: %s', tid, e, exc_info=True)
                    errors.append(f'Trade {tid}: {str(e)}')
            db.commit()
            return rolled_back, errors
        finally:
            _pool_put(db)

    rolled_back, errors = await asyncio.to_thread(_rollback_all)
    return api_ok({'rolled_back': rolled_back, 'errors': errors})
@trading_decision_bp.route('/api/v1/trading/trades/<int:tid>', methods=['DELETE'])
async def trading_trades_dismiss(tid):
    uid = current_user_id()
    await async_execute('UPDATE trading_trade_queue SET status=? WHERE id=? AND status=? AND user_id=?',
                        ('dismissed', tid, 'pending', uid), domain=DOMAIN_TRADING)
    return api_ok()
@trading_decision_bp.route('/api/v1/trading/trades/rollback-batch', methods=['POST'])
async def trading_trades_rollback_batch():
    """Rollback all executed trades for a batch_id (decision rollback)."""
    uid = current_user_id()
    data = await async_parse_body()
    batch_id = data.get('batch_id', '')
    if not batch_id:
        return api_bad_request('batch_id required')

    trades = await async_fetchall('SELECT * FROM trading_trade_queue WHERE batch_id=? AND status=? AND user_id=?', (batch_id, 'executed', uid), domain=DOMAIN_TRADING)
    if not trades:
        return api_not_found('No executed trades found for this batch')

    trades = [dict(t) for t in trades]

    # _rollback_trade takes a raw DB connection and calls blocking market-data
    # helpers (get_latest_price). Run the whole loop on a single pooled
    # connection inside a worker thread so the event loop stays free.
    def _rollback_batch():
        from tofu_trading.storage import _pool_get, _pool_put
        db = _pool_get(owner_user_id=uid)
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            rolled_back = []
            errors = []
            for trade in trades:
                try:
                    _rollback_trade(db, trade, now, uid)
                    rolled_back.append(trade['id'])
                except Exception as e:
                    logger.error('[Decision] Batch rollback failed for trade %s: %s', trade.get('id', '?'), e, exc_info=True)
                    errors.append(f'Trade {trade["id"]}: {str(e)}')
            db.execute('UPDATE trading_decision_history SET status=? WHERE batch_id=? AND user_id=?', ('rolled_back', batch_id, uid))
            db.commit()
            return rolled_back, errors
        finally:
            _pool_put(db)

    rolled_back, errors = await asyncio.to_thread(_rollback_batch)
    return api_ok({'rolled_back': rolled_back, 'errors': errors})
@trading_decision_bp.route('/api/v1/trading/fees/<code>', methods=['GET'])
async def trading_fees_get(code):
    from tofu_trading.trading import fetch_trading_fees
    # Blocking market-data call — offload to a worker thread off the event loop.
    fees = await asyncio.to_thread(fetch_trading_fees, code)
    return jsonify(fees)
