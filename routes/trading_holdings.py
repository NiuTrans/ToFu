"""routes/trading_holdings.py — Holdings CRUD, cash, search, NAV updates, network status."""

import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from lib.database import DOMAIN_TRADING, db_execute_with_retry, get_db
from lib.log import get_logger

logger = get_logger(__name__)

trading_holdings_bp = Blueprint('trading_holdings', __name__)


@trading_holdings_bp.route('/api/trading/holdings', methods=['GET'])
def trading_holdings_list():
    """List all holdings with price data. Uses 3-layer cache — never blocks."""
    db = get_db(DOMAIN_TRADING)
    rows = db.execute('SELECT * FROM trading_holdings ORDER BY buy_date DESC').fetchall()
    holdings = [dict(r) for r in rows]
    from lib.trading import _prewarm_price_cache, fetch_asset_info, get_latest_price
    _prewarm_price_cache([h['symbol'] for h in holdings])
    for h in holdings:
        code = h['symbol']
        nav_val, nav_date = get_latest_price(code)
        info = fetch_asset_info(code)
        if nav_val:
            h['current_nav'] = nav_val
            h['nav_date'] = nav_date
        else:
            h['current_nav'] = h['buy_price']
            h['nav_date'] = h.get('buy_date', '')
            h['nav_source'] = 'cost_fallback'
        h['market_value'] = round(h['current_nav'] * h['shares'], 2)
        h['profit'] = round((h['current_nav'] - h['buy_price']) * h['shares'], 2)
        h['profit_pct'] = round((h['current_nav'] - h['buy_price']) / h['buy_price'] * 100, 2) if h['buy_price'] > 0 else 0
        if info:
            h['asset_name'] = info.get('name', h.get('asset_name', ''))
            h['asset_type'] = info.get('type', '')
            if info.get('est_nav'):
                try:
                    est = float(info['est_nav'])
                    h['est_nav'] = est
                    h['est_change'] = info.get('est_change', '')
                    h['est_market_value'] = round(est * h['shares'], 2)
                    h['est_profit'] = round((est - h['buy_price']) * h['shares'], 2)
                    h['est_profit_pct'] = round((est - h['buy_price']) / h['buy_price'] * 100, 2) if h['buy_price'] > 0 else 0
                except (ValueError, TypeError):
                    logger.warning('Failed to compute estimated NAV values for holding %s: est_nav=%r, buy_price=%r',
                                   h.get('symbol', '?'), info.get('est_nav'), h.get('buy_price'), exc_info=True)
    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
    cash = float(cfg['value']) if cfg else 0
    return jsonify({'holdings': holdings, 'available_cash': cash})


@trading_holdings_bp.route('/api/trading/holdings', methods=['POST'])
def trading_holdings_add():
    data = request.get_json(silent=True) or {}
    code = data.get('symbol', '').strip()
    if not code:
        return jsonify({'error': 'symbol required'}), 400
    from lib.trading import fetch_asset_info
    info = fetch_asset_info(code)
    name = info.get('name', '') if info else data.get('asset_name', '')
    db = get_db(DOMAIN_TRADING)
    now = int(time.time() * 1000)
    db_execute_with_retry(db, '''INSERT INTO trading_holdings (symbol, asset_name, shares, buy_price, buy_date, note, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
               (code, name, data.get('shares', 0), data.get('buy_price', 0),
                data.get('buy_date', ''), data.get('note', ''), now, now))
    db_execute_with_retry(db, '''INSERT INTO trading_transactions (symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                  VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, ?)''',
               (code, name, data.get('shares', 0), data.get('buy_price', 0),
                round(data.get('shares', 0) * data.get('buy_price', 0), 2),
                data.get('note', ''), data.get('buy_date', ''), now))
    return jsonify({'ok': True})


@trading_holdings_bp.route('/api/trading/holdings/<int:hid>', methods=['PUT'])
def trading_holdings_update(hid):
    data = request.get_json(silent=True) or {}
    db = get_db(DOMAIN_TRADING)
    now = int(time.time() * 1000)
    db_execute_with_retry(db, '''UPDATE trading_holdings SET shares=?, buy_price=?, buy_date=?, note=?, updated_at=?
                  WHERE id=?''',
               (data.get('shares', 0), data.get('buy_price', 0),
                data.get('buy_date', ''), data.get('note', ''), now, hid))
    return jsonify({'ok': True})


@trading_holdings_bp.route('/api/trading/holdings/<int:hid>', methods=['DELETE'])
def trading_holdings_delete(hid):
    db = get_db(DOMAIN_TRADING)
    row = db.execute('SELECT * FROM trading_holdings WHERE id=?', (hid,)).fetchone()
    if row:
        now = int(time.time() * 1000)
        db_execute_with_retry(db, '''INSERT INTO trading_transactions (symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                      VALUES (?, ?, 'sell', ?, ?, ?, '清仓卖出', ?, ?)''',
                   (row['symbol'], row['asset_name'], row['shares'], row['buy_price'],
                    round(row['shares'] * row['buy_price'], 2),
                    datetime.now().strftime('%Y-%m-%d'), now))
    db_execute_with_retry(db, 'DELETE FROM trading_holdings WHERE id=?', (hid,))
    return jsonify({'ok': True})


@trading_holdings_bp.route('/api/trading/holdings/all', methods=['DELETE'])
def trading_holdings_delete_all():
    """Delete all holdings at once (一键清仓).

    Records a sell transaction for each holding before deletion.
    """
    db = get_db(DOMAIN_TRADING)
    rows = db.execute('SELECT * FROM trading_holdings').fetchall()
    if not rows:
        logger.info('[Portfolio] Delete-all requested but no holdings found')
        return jsonify({'ok': True, 'deleted': 0})

    now = int(time.time() * 1000)
    deleted = 0
    for row in rows:
        row = dict(row)
        try:
            db_execute_with_retry(db, '''INSERT INTO trading_transactions
                (symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                VALUES (?, ?, 'sell', ?, ?, ?, '一键清仓', ?, ?)''',
                (row['symbol'], row.get('asset_name', ''), row['shares'], row['buy_price'],
                 round(row['shares'] * row['buy_price'], 2),
                 datetime.now().strftime('%Y-%m-%d'), now))
        except Exception as e:
            logger.warning('[Portfolio] Failed to record sell tx for %s during delete-all: %s',
                           row['symbol'], e)
        deleted += 1

    db_execute_with_retry(db, 'DELETE FROM trading_holdings')
    logger.info('[Portfolio] Delete-all completed: %d holdings removed', deleted)
    return jsonify({'ok': True, 'deleted': deleted})


@trading_holdings_bp.route('/api/trading/cash', methods=['POST'])
def trading_set_cash():
    data = request.get_json(silent=True) or {}
    cash = float(data.get('amount', 0))
    db = get_db(DOMAIN_TRADING)
    db_execute_with_retry(db, "INSERT OR REPLACE INTO trading_config (key, value) VALUES ('available_cash', ?)", (str(cash),))
    return jsonify({'ok': True})


@trading_holdings_bp.route('/api/trading/cash', methods=['GET'])
def trading_get_cash():
    db = get_db(DOMAIN_TRADING)
    cfg = db.execute("SELECT value FROM trading_config WHERE key='available_cash'").fetchone()
    return jsonify({'cash': float(cfg['value']) if cfg else 0})


@trading_holdings_bp.route('/api/trading/search', methods=['GET'])
def trading_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': []})
    from lib.trading import search_asset
    results = search_asset(q)
    return jsonify({'results': results})


@trading_holdings_bp.route('/api/trading/nav/update', methods=['POST'])
def trading_price_update():
    data = request.get_json(silent=True) or {}
    code = data.get('code', '').strip()
    nav = data.get('nav')
    nav_date = data.get('nav_date', datetime.now().strftime('%Y-%m-%d'))
    name = data.get('name', '')
    if not code or nav is None:
        return jsonify({'error': 'code and nav required'}), 400
    from lib.trading import update_nav_cache
    update_nav_cache(code, float(nav), nav_date, name)
    return jsonify({'ok': True, 'code': code, 'nav': float(nav), 'date': nav_date})


@trading_holdings_bp.route('/api/trading/nav/batch_update', methods=['POST'])
def trading_price_batch_update():
    data = request.get_json(silent=True) or {}
    items = data.get('items', [])
    from lib.trading import update_nav_cache
    updated = 0
    for item in items:
        code = item.get('code', '').strip()
        nav = item.get('nav')
        if code and nav is not None:
            update_nav_cache(code, float(nav),
                             item.get('nav_date', datetime.now().strftime('%Y-%m-%d')),
                             item.get('name', ''))
            updated += 1
    return jsonify({'ok': True, 'updated': updated})


@trading_holdings_bp.route('/api/trading/network_status', methods=['GET'])
def trading_network_status():
    from lib.trading._common import _check_external_network, _net_state
    is_ok = _check_external_network()
    return jsonify({
        'external_network_ok': is_ok,
        'last_check': _net_state['last_check'],
        'message': '外部金融数据接口可用' if is_ok else '外部网络不可达，使用本地缓存和AI分析',
    })


@trading_holdings_bp.route('/api/trading/nav_history', methods=['GET'])
def trading_price_history():
    code = request.args.get('code', '').strip()
    days = int(request.args.get('days', '365'))
    if not code:
        return jsonify({'error': 'code required'}), 400
    from datetime import timedelta

    from lib.trading import fetch_price_history
    _start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    _end = datetime.now().strftime('%Y-%m-%d')
    data = fetch_price_history(code, _start, _end)
    return jsonify({'code': code, 'history': data})


@trading_holdings_bp.route('/api/trading/transactions', methods=['GET'])
def trading_transactions():
    db = get_db(DOMAIN_TRADING)
    rows = db.execute('SELECT * FROM trading_transactions ORDER BY created_at DESC LIMIT 200').fetchall()
    return jsonify({'transactions': [dict(r) for r in rows]})
