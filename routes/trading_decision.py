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

import json
import re
import time
from datetime import datetime

from flask import jsonify, request

from lib.database import DOMAIN_TRADING, get_db
from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import parse_body

logger = get_logger(__name__)

from routes.api_v1.trading.decision import api_v1_trading_decision_bp as trading_decision_bp  # noqa: E402
# (alias kept for back-compat with `from routes.trading_decision import trading_decision_bp` callers)


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

@trading_decision_bp.route('/api/v1/trading/briefing', methods=['GET'])
def asset_briefing_get():
    """Get today's cached briefing."""
    db = get_db(DOMAIN_TRADING)
    today = datetime.now().strftime('%Y-%m-%d')
    row = db.execute('SELECT * FROM trading_daily_briefing WHERE date=?', (today,)).fetchone()
    if row:
        row = dict(row)
        return jsonify({'briefing': row['content'], 'date': row['date'], 'created_at': row['created_at']})
    return jsonify({'briefing': None, 'date': today})


@trading_decision_bp.route('/api/v1/trading/decisions', methods=['GET'])
def trading_decisions_list():
    db = get_db(DOMAIN_TRADING)
    rows = db.execute('SELECT * FROM trading_decision_history ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'decisions': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/v1/trading/decisions/<int:did>/results', methods=['POST'])
def trading_decisions_record_results(did):
    """Record actual results for a past decision."""
    db = get_db(DOMAIN_TRADING)
    data = parse_body()
    db.execute('UPDATE trading_decision_history SET actual_result=? WHERE id=?',
               (data.get('actual_result', ''), did))
    db.commit()
    return api_ok()
# ── Trade Queue ──

@trading_decision_bp.route('/api/v1/trading/trades', methods=['GET'])
def trading_trades_list():
    db = get_db(DOMAIN_TRADING)
    status = request.args.get('status', '')
    if status:
        rows = db.execute('SELECT * FROM trading_trade_queue WHERE status=? ORDER BY created_at DESC', (status,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM trading_trade_queue ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'trades': [dict(r) for r in rows]})


@trading_decision_bp.route('/api/v1/trading/trades/execute', methods=['POST'])
def trading_trades_execute():
    """Execute trades."""
    data = parse_body()
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
        return api_bad_request('No trades selected')

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
    return api_ok({'executed': executed, 'errors': errors})
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


@trading_decision_bp.route('/api/v1/trading/trades/rollback', methods=['POST'])
def trading_trades_rollback():
    """Rollback executed trades."""
    data = parse_body()
    trade_ids = data.get('trade_ids', [])
    if not trade_ids:
        return api_bad_request('No trades selected')

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
    return api_ok({'rolled_back': rolled_back, 'errors': errors})
@trading_decision_bp.route('/api/v1/trading/trades/<int:tid>', methods=['DELETE'])
def trading_trades_dismiss(tid):
    db = get_db(DOMAIN_TRADING)
    db.execute('UPDATE trading_trade_queue SET status=? WHERE id=? AND status=?', ('dismissed', tid, 'pending'))
    db.commit()
    return api_ok()
@trading_decision_bp.route('/api/v1/trading/trades/rollback-batch', methods=['POST'])
def trading_trades_rollback_batch():
    """Rollback all executed trades for a batch_id (decision rollback)."""
    data = parse_body()
    batch_id = data.get('batch_id', '')
    if not batch_id:
        return api_bad_request('batch_id required')

    db = get_db(DOMAIN_TRADING)
    trades = db.execute('SELECT * FROM trading_trade_queue WHERE batch_id=? AND status=?', (batch_id, 'executed')).fetchall()
    if not trades:
        return api_not_found('No executed trades found for this batch')

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
    return api_ok({'rolled_back': rolled_back, 'errors': errors})
@trading_decision_bp.route('/api/v1/trading/fees/<code>', methods=['GET'])
def trading_fees_get(code):
    from lib.trading import fetch_trading_fees
    fees = fetch_trading_fees(code)
    return jsonify(fees)
