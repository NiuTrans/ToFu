"""lib/trading/nav.py — Multi-layer NAV caching: memory → DB → holdings fallback."""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.log import get_logger
from lib.trading._common import TradingClient, _get_default_client

logger = get_logger(__name__)

__all__ = [
    'get_latest_price',
    'fetch_price_history',
    'update_nav_cache',
    '_prewarm_price_cache',
    '_nav_from_memory',
    '_nav_to_memory',
    '_nav_from_db',
    '_nav_to_db',
    '_nav_from_holdings',
]

# ── In-memory NAV cache (L1) ────────────────────────────
# {symbol: {'nav': float, 'date': str, 'name': str, 'ts': float, 'source': str}}
_nav_cache = {}
_nav_lock = threading.Lock()
_NAV_CACHE_TTL = 1800  # 30 min for L1 memory cache

def _nav_from_memory(code):
    """L1: check in-memory cache."""
    with _nav_lock:
        entry = _nav_cache.get(code)
    if entry and (time.time() - entry['ts']) < _NAV_CACHE_TTL:
        return entry
    return None

def _nav_to_memory(code, nav, nav_date, name='', source='api'):
    """Store in L1 memory cache."""
    with _nav_lock:
        _nav_cache[code] = {
            'nav': nav, 'date': nav_date, 'name': name,
            'ts': time.time(), 'source': source,
        }

def _nav_from_db(code):
    """L2: check DB nav cache (survives restarts, 24h TTL)."""
    try:
        from lib.database import DOMAIN_TRADING, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)
        row = db.execute(
            'SELECT * FROM trading_price_cache WHERE symbol=? AND updated_at > ?',
            (code, int(time.time() * 1000) - 86400 * 1000)
        ).fetchone()
        if row:
            d = dict(row)
            # Also warm L1
            _nav_to_memory(code, d['nav'], d['nav_date'], d.get('asset_name', ''), 'db_cache')
            return d
    except Exception as e:
        logger.warning('[Trading] L2 DB nav cache read failed for %s: %s', code, e, exc_info=True)
    return None

def _nav_to_db(code, nav, nav_date, name=''):
    """Store in L2 DB cache."""
    try:
        from lib.database import DOMAIN_TRADING, db_execute_with_retry, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)
        db_execute_with_retry(db, '''INSERT OR REPLACE INTO trading_price_cache
                      (symbol, asset_name, nav, nav_date, updated_at)
                      VALUES (?, ?, ?, ?, ?)''',
                   (code, name, nav, nav_date, int(time.time() * 1000)))
    except Exception as e:
        logger.warning('[Trading] L2 DB nav cache write failed for %s: %s', code, e, exc_info=True)

def _nav_from_holdings(code):
    """L3: fallback to buy_price from holdings table (always available, never blocks)."""
    try:
        from lib.database import DOMAIN_TRADING, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)
        row = db.execute(
            'SELECT symbol, asset_name, buy_price, buy_date FROM trading_holdings WHERE symbol=? LIMIT 1',
            (code,)
        ).fetchone()
        if row:
            d = dict(row)
            return {
                'nav': d['buy_price'], 'nav_date': d.get('buy_date', ''),
                'asset_name': d.get('asset_name', ''), 'source': 'holdings_cost',
            }
    except Exception as e:
        logger.warning('[Trading] L3 holdings fallback failed for %s: %s', code, e, exc_info=True)
    return None


def get_latest_price(code, *, client=None):
    """Get latest price with 3-layer cache. Never blocks more than 3s total.

    Args:
        code:   Fund code.
        client: Optional ``TradingClient`` instance for dependency injection.
                Defaults to the lazily-initialised module-level singleton.
    """
    if client is None:
        client = _get_default_client()
    # Try fetch_asset_info first (uses full cache chain: memory → DB → network)
    from lib.trading.info import fetch_asset_info
    info = fetch_asset_info(code, client=client)
    if info and info.get('nav'):
        try:
            return float(info['nav']), info.get('nav_date', '')
        except (ValueError, TypeError):
            logger.debug('[NAV] Failed to convert NAV to float for %s: %r', code, info.get('nav'), exc_info=True)
    # Only try history API if network is up
    if client.check_network():
        try:
            url = (f'https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery'
                   f'&fundCode={code}&pageIndex=1&pageSize=1')
            r = client.session.get(url, timeout=3, headers={
                **client.headers,
                'Referer': f'https://fundf10.eastmoney.com/jjjz_{code}.html',
            })
            text = r.text
            m = re.search(r'jQuery\((.*)\)', text, re.S)
            try:
                if m:
                    data = json.loads(m.group(1))
                else:
                    data = r.json()
            except (json.JSONDecodeError, ValueError) as je:
                logger.warning('[Trading] NAV JSONP parse failed for code=%s: %s', code, je, exc_info=True)
                data = {}
            items = data.get('Data', {}).get('LSJZList', [])
            if items:
                nav = float(items[0].get('DWJZ', 0))
                nav_date = items[0].get('FSRQ', '')
                # Cache it
                _nav_to_memory(code, nav, nav_date, '', 'api_history')
                _nav_to_db(code, nav, nav_date, '')
                return nav, nav_date
        except Exception as e:
            logger.error('get_latest_price API error for %s: %s', code, e, exc_info=True)
    return None, None


def update_nav_cache(code, nav, nav_date, name=''):
    """Manually update NAV cache — called by browser extension or manual entry."""
    _nav_to_memory(code, nav, nav_date, name, 'manual')
    _nav_to_db(code, nav, nav_date, name)
    logger.debug('NAV cache updated: %s = %s (%s)', code, nav, nav_date)


def fetch_price_history(code, start_date=None, end_date=None, max_pages=50,
                           *, client=None):
    """Fetch asset price history. Fast-fails if network unreachable.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    if not client.check_network():
        logger.debug('Skipping history fetch for %s: external network unreachable', code)
        return []
    all_data = []
    page = 1
    PAGE_SIZE = 20  # eastmoney API caps at 20 regardless of request
    while page <= max_pages:
        try:
            url = (f'https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery'
                   f'&fundCode={code}&pageIndex={page}&pageSize={PAGE_SIZE}')
            if start_date:
                url += f'&startDate={start_date}'
            if end_date:
                url += f'&endDate={end_date}'
            r = client.session.get(url, timeout=5,
                          headers={**client.headers, 'Referer': f'https://fundf10.eastmoney.com/jjjz_{code}.html'})
            text = r.text
            m = re.search(r'jQuery\((.*)\)', text, re.S)
            try:
                resp_json = json.loads(m.group(1)) if m else r.json()
            except (json.JSONDecodeError, ValueError) as je:
                logger.warning('[Trading] history JSONP parse failed for code=%s: %s', code, je, exc_info=True)
                break
            data = resp_json.get('Data', {})
            items = data.get('LSJZList', [])
            if not items:
                break
            for item in items:
                try:
                    nav_val = float(item.get('DWJZ', 0))
                    acc_val = float(item.get('LJJZ', 0))
                    change = item.get('JZZZL', '')
                    change_pct = float(change) if change and change != '' else 0.0
                    all_data.append({
                        'date': item['FSRQ'],
                        'nav': nav_val,
                        'acc_nav': acc_val,
                        'change_pct': change_pct,
                    })
                except (ValueError, KeyError):
                    logger.debug('[NAV] Skipping malformed NAV record for %s: %r', code, item, exc_info=True)
                    continue
            # Break if we got fewer items than page size (last page) or
            # if TotalCount is known and we've fetched enough.
            total_count = data.get('TotalCount', 0)
            if len(items) < PAGE_SIZE:
                break
            if total_count > 0 and page * PAGE_SIZE >= total_count:
                break
            page += 1
            time.sleep(0.1)
        except Exception as e:
            logger.error('Error fetching %s page %s: %s', code, page, e, exc_info=True)
            break
    return sorted(all_data, key=lambda x: x['date'])


def _prewarm_price_cache(codes, *, client=None):
    """Pre-warm NAV cache for multiple asset codes in parallel.
    Returns immediately if network is down. Max 2s total.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    if not codes:
        return
    # Only fetch codes not already in memory cache
    with _nav_lock:
        missing = [c for c in set(codes) if c not in _nav_cache]
    if not missing:
        return
    if not client.check_network():
        # Try DB cache for all missing
        for c in missing:
            _nav_from_db(c)
        return
    # Parallel fetch with ThreadPool — max 2s
    def _fetch_one(code):
        try:
            return code, get_latest_price(code, client=client)
        except Exception as e:
            logger.debug('[Trading] prewarm fetch failed for %s: %s', code, e, exc_info=True)
            return code, (None, None)
    with ThreadPoolExecutor(max_workers=min(len(missing), 6)) as pool:
        futs = {pool.submit(_fetch_one, c): c for c in missing}
        try:
            for f in as_completed(futs, timeout=2):
                try:
                    f.result()
                except Exception as e:
                    logger.debug('[Trading] prewarm future failed for %s: %s', futs.get(f, '?'), e, exc_info=True)
        except TimeoutError:
            # Some futures didn't complete within 2s — that's fine,
            # the holdings endpoint will use cost_fallback for those.
            timed_out = [futs[f] for f in futs if not f.done()]
            logger.debug('[Trading] prewarm timeout: %d/%d codes unfinished: %s',
                         len(timed_out), len(missing), timed_out[:5])
