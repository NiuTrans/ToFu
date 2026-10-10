"""tofu_trading/web/handlers/trading_holdings.py — Holdings CRUD, cash, search, NAV updates.

Every query in this module is scoped by ``user_id``. Before the multi-tenant
pass the queries were global: ``SELECT * FROM trading_holdings`` returned every
user's portfolio and ``DELETE /holdings/all`` truncated the table for everyone.
The scoping helper lives in ``tofu_trading.identity`` and fails closed.
"""

import asyncio
import time
from datetime import datetime

from flask import jsonify, request

from lib.log import get_logger
from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.request_parser import async_parse_body

from tofu_trading.identity import current_user_id
from tofu_trading.storage import (
    DOMAIN_TRADING,
    async_execute,
    async_fetchall,
    async_fetchone,
)

logger = get_logger(__name__)

from tofu_trading.web.v1.holdings import api_v1_trading_holdings_bp as trading_holdings_bp  # noqa: E402
# (alias kept for back-compat with `from tofu_trading.web.handlers.trading_holdings import trading_holdings_bp` callers)


@trading_holdings_bp.route('/api/v1/trading/holdings', methods=['GET'])
async def trading_holdings_list():
    """List this user's holdings with price data. Uses 3-layer cache — never blocks."""
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT * FROM trading_holdings WHERE user_id=? ORDER BY buy_date DESC',
        (uid,), domain=DOMAIN_TRADING)
    holdings = [dict(r) for r in rows]
    from tofu_trading.trading import _prewarm_price_cache, fetch_asset_info, get_latest_price

    def _enrich():
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

    await asyncio.to_thread(_enrich)
    cfg = await async_fetchone(
        "SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'",
        (uid,), domain=DOMAIN_TRADING)
    cash = float(cfg['value']) if cfg else 0
    return jsonify({'holdings': holdings, 'available_cash': cash})


@trading_holdings_bp.route('/api/v1/trading/holdings', methods=['POST'])
async def trading_holdings_add():
    uid = current_user_id()
    data = await async_parse_body()
    code = data.get('symbol', '').strip()
    if not code:
        return api_bad_request('symbol required')
    from tofu_trading.trading import fetch_asset_info
    info = await asyncio.to_thread(fetch_asset_info, code)
    name = info.get('name', '') if info else data.get('asset_name', '')
    now = int(time.time() * 1000)
    await async_execute('''INSERT INTO trading_holdings (user_id, symbol, asset_name, shares, buy_price, buy_date, note, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
               (uid, code, name, data.get('shares', 0), data.get('buy_price', 0),
                data.get('buy_date', ''), data.get('note', ''), now, now),
               domain=DOMAIN_TRADING)
    await async_execute('''INSERT INTO trading_transactions (user_id, symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                  VALUES (?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?)''',
               (uid, code, name, data.get('shares', 0), data.get('buy_price', 0),
                round(data.get('shares', 0) * data.get('buy_price', 0), 2),
                data.get('note', ''), data.get('buy_date', ''), now),
               domain=DOMAIN_TRADING)
    return api_ok()


@trading_holdings_bp.route('/api/v1/trading/holdings/<int:hid>', methods=['PUT'])
async def trading_holdings_update(hid):
    uid = current_user_id()
    data = await async_parse_body()
    now = int(time.time() * 1000)
    # user_id in the WHERE clause is what stops user A editing user B's row by
    # guessing an id — the id alone is not an authorisation token.
    await async_execute('''UPDATE trading_holdings SET shares=?, buy_price=?, buy_date=?, note=?, updated_at=?
                  WHERE id=? AND user_id=?''',
               (data.get('shares', 0), data.get('buy_price', 0),
                data.get('buy_date', ''), data.get('note', ''), now, hid, uid),
               domain=DOMAIN_TRADING)
    return api_ok()


@trading_holdings_bp.route('/api/v1/trading/holdings/<int:hid>', methods=['DELETE'])
async def trading_holdings_delete(hid):
    uid = current_user_id()
    row = await async_fetchone(
        'SELECT * FROM trading_holdings WHERE id=? AND user_id=?',
        (hid, uid), domain=DOMAIN_TRADING)
    if not row:
        # Same response whether the row belongs to someone else or does not
        # exist — distinguishing them would leak other users' row ids.
        return api_not_found('holding not found')
    now = int(time.time() * 1000)
    await async_execute('''INSERT INTO trading_transactions (user_id, symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                  VALUES (?, ?, ?, 'sell', ?, ?, ?, '清仓卖出', ?, ?)''',
               (uid, row['symbol'], row['asset_name'], row['shares'], row['buy_price'],
                round(row['shares'] * row['buy_price'], 2),
                datetime.now().strftime('%Y-%m-%d'), now),
               domain=DOMAIN_TRADING)
    await async_execute('DELETE FROM trading_holdings WHERE id=? AND user_id=?',
                        (hid, uid), domain=DOMAIN_TRADING)
    return api_ok()


@trading_holdings_bp.route('/api/v1/trading/holdings/all', methods=['DELETE'])
async def trading_holdings_delete_all():
    """Clear THIS user's holdings (一键清仓).

    Records a sell transaction for each holding before deletion. Previously
    this truncated the table for every user on the host.
    """
    uid = current_user_id()
    rows = await async_fetchall('SELECT * FROM trading_holdings WHERE user_id=?',
                                (uid,), domain=DOMAIN_TRADING)
    if not rows:
        logger.info('[Portfolio] Delete-all requested but no holdings found (user=%s)', uid)
        return api_ok({'deleted': 0})
    now = int(time.time() * 1000)
    deleted = 0
    for row in rows:
        row = dict(row)
        try:
            await async_execute('''INSERT INTO trading_transactions
                (user_id, symbol, asset_name, type, shares, price, amount, note, tx_date, created_at)
                VALUES (?, ?, ?, 'sell', ?, ?, ?, '一键清仓', ?, ?)''',
                (uid, row['symbol'], row.get('asset_name', ''), row['shares'], row['buy_price'],
                 round(row['shares'] * row['buy_price'], 2),
                 datetime.now().strftime('%Y-%m-%d'), now),
                domain=DOMAIN_TRADING)
        except Exception as e:
            logger.warning('[Portfolio] Failed to record sell tx for %s during delete-all: %s',
                           row['symbol'], e)
        deleted += 1

    await async_execute('DELETE FROM trading_holdings WHERE user_id=?', (uid,),
                        domain=DOMAIN_TRADING)
    logger.info('[Portfolio] Delete-all completed: %d holdings removed (user=%s)', deleted, uid)
    return api_ok({'deleted': deleted})


@trading_holdings_bp.route('/api/v1/trading/cash', methods=['POST'])
async def trading_set_cash():
    uid = current_user_id()
    data = await async_parse_body()
    cash = float(data.get('amount', 0))
    await async_execute(
        "INSERT OR REPLACE INTO trading_user_config (user_id, key, value) "
        "VALUES (?, 'available_cash', ?)",
        (uid, str(cash)), domain=DOMAIN_TRADING)
    return api_ok()


@trading_holdings_bp.route('/api/v1/trading/cash', methods=['GET'])
async def trading_get_cash():
    uid = current_user_id()
    cfg = await async_fetchone(
        "SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'",
        (uid,), domain=DOMAIN_TRADING)
    return jsonify({'cash': float(cfg['value']) if cfg else 0})


@trading_holdings_bp.route('/api/v1/trading/search', methods=['GET'])
async def trading_search():
    """Search stocks, ETFs, and funds by keyword/code (universal search).

    Not user-scoped: this queries the public market universe, not user data.
    """
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': []})
    from tofu_trading.trading.info import search_asset_universal
    results = await asyncio.to_thread(search_asset_universal, q)
    return jsonify({'results': results})


@trading_holdings_bp.route('/api/v1/trading/nav/update', methods=['POST'])
async def trading_price_update():
    """Update the shared NAV cache. Not user-scoped — market prices are global."""
    data = await async_parse_body()
    code = data.get('code', '').strip()
    nav = data.get('nav')
    nav_date = data.get('nav_date', datetime.now().strftime('%Y-%m-%d'))
    name = data.get('name', '')
    if not code or nav is None:
        return api_bad_request('code and nav required')
    from tofu_trading.trading import update_nav_cache
    await asyncio.to_thread(update_nav_cache, code, float(nav), nav_date, name)
    return api_ok({'code': code, 'nav': float(nav), 'date': nav_date})


@trading_holdings_bp.route('/api/v1/trading/nav/batch_update', methods=['POST'])
async def trading_price_batch_update():
    """Batch NAV cache update. Not user-scoped — market prices are global."""
    data = await async_parse_body()
    items = data.get('items', [])
    from tofu_trading.trading import update_nav_cache
    updated = 0
    for item in items:
        code = item.get('code', '').strip()
        nav = item.get('nav')
        if code and nav is not None:
            await asyncio.to_thread(update_nav_cache, code, float(nav),
                             item.get('nav_date', datetime.now().strftime('%Y-%m-%d')),
                             item.get('name', ''))
            updated += 1
    return api_ok({'updated': updated})


@trading_holdings_bp.route('/api/v1/trading/network_status', methods=['GET'])
async def trading_network_status():
    """External data-source reachability + per-K-line-source health."""
    from tofu_trading.trading._common import _check_external_network
    from tofu_trading.trading.kline import get_source_health
    is_ok = await asyncio.to_thread(_check_external_network)
    return jsonify({
        'external_network_ok': is_ok,
        'kline_sources': get_source_health(),
        'message': '外部金融数据接口可用' if is_ok else '外部网络不可达，使用本地缓存和AI分析',
    })


@trading_holdings_bp.route('/api/v1/trading/nav_history', methods=['GET'])
async def trading_price_history():
    """Price history for one symbol. Not user-scoped — market data is global."""
    code = request.args.get('code', '').strip()
    days = int(request.args.get('days', '365'))
    if not code:
        return api_bad_request('code required')
    from datetime import timedelta

    from tofu_trading.trading import fetch_price_history
    _start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    _end = datetime.now().strftime('%Y-%m-%d')
    data = await asyncio.to_thread(fetch_price_history, code, _start, _end)
    return jsonify({'code': code, 'history': data})


@trading_holdings_bp.route('/api/v1/trading/transactions', methods=['GET'])
async def trading_transactions():
    uid = current_user_id()
    rows = await async_fetchall(
        'SELECT * FROM trading_transactions WHERE user_id=? '
        'ORDER BY created_at DESC LIMIT 200',
        (uid,), domain=DOMAIN_TRADING)
    return jsonify({'transactions': [dict(r) for r in rows]})
