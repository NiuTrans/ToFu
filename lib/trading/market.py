"""lib/trading/market.py — Real-time market data from eastmoney APIs.

Provides:
  - Major index quotes (上证/深证/创业板/科创50/沪深300/中证500/中证1000)
  - Sector (行业) performance heatmap
  - Top gainers / losers across assets
  - Northbound capital flow (北向资金)
  - Market breadth (涨跌家数)
  - Index intraday trend (分时走势)
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from datetime import time as dtime

from lib.log import get_logger
from lib.trading._common import _get_default_client

logger = get_logger(__name__)


def _is_timeout(e: Exception) -> bool:
    """Check if an exception is a network timeout (expected/transient)."""
    return 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()

# A-share trading hours: 09:30–11:30, 13:00–15:00
# Use a buffer: 09:15–15:10 to account for pre-market and settlement
_MARKET_OPEN_TIME = dtime(9, 15)
_MARKET_CLOSE_TIME = dtime(15, 10)


def _is_after_hours() -> bool:
    """Return True if current local time is outside A-share trading hours on a weekday.

    This is used to distinguish 'after hours on a normal trading day' from
    'true holiday/weekend' when the EastMoney API returns placeholder data.
    """
    now = datetime.now()
    # Weekend is never "after hours" — it's a non-trading day
    if now.weekday() >= 5:
        return False
    t = now.time()
    # If it's a weekday but outside 09:15-15:10, it's after hours
    return t < _MARKET_OPEN_TIME or t > _MARKET_CLOSE_TIME

__all__ = [
    'fetch_major_indices',
    'fetch_sector_performance',
    'fetch_market_breadth',
    'fetch_northbound_flow',
    'fetch_index_trend',
    'fetch_top_assets',
    'fetch_market_overview',
]

# ── In-memory cache ──
_market_cache = {}
_market_lock = threading.Lock()
_CACHE_TTL = {
    'indices': 30,       # 30s for indices
    'sectors': 120,      # 2min for sectors
    'breadth': 60,       # 1min for breadth
    'northbound': 300,   # 5min for northbound
    'trend': 30,         # 30s for intraday trend
    'top_assets': 120,    # 2min for top assets
}


def _cache_get(key):
    """Get from cache if not expired."""
    with _market_lock:
        entry = _market_cache.get(key)
    if entry and (time.time() - entry['ts']) < _CACHE_TTL.get(key.split(':')[0], 60):
        return entry['data']
    return None


def _cache_set(key, data):
    """Store in cache."""
    with _market_lock:
        _market_cache[key] = {'data': data, 'ts': time.time()}


# ═══════════════════════════════════════════════════════════
#  Major Indices (大盘指数)
# ═══════════════════════════════════════════════════════════

# secid format: market.code — 1=沪 0=深
MAJOR_INDICES = [
    {'secid': '1.000001', 'name': '上证指数', 'short': '上证'},
    {'secid': '0.399001', 'name': '深证成指', 'short': '深证'},
    {'secid': '0.399006', 'name': '创业板指', 'short': '创业板'},
    {'secid': '1.000688', 'name': '科创50',   'short': '科创50'},
    {'secid': '1.000300', 'name': '沪深300',  'short': '沪深300'},
    {'secid': '1.000905', 'name': '中证500',  'short': '中证500'},
    {'secid': '1.000852', 'name': '中证1000', 'short': '中证1000'},
    {'secid': '100.HSI',  'name': '恒生指数', 'short': '恒生'},
]


def fetch_major_indices(*, client=None):
    """Fetch real-time major index quotes.

    Uses eastmoney push2his API for real-time quotes.
    """
    cached = _cache_get('indices')
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return []

    secids = ','.join(idx['secid'] for idx in MAJOR_INDICES)
    url = (
        f'http://push2delay.eastmoney.com/api/qt/ulist.np/get?'
        f'fltt=2&secids={secids}'
        f'&fields=f1,f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18'
        f'&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        if m:
            data = json.loads(m.group(1))
        else:
            data = r.json()

        result = []
        diff_list = data.get('data', {}).get('diff', [])
        for i, item in enumerate(diff_list):
            idx_info = MAJOR_INDICES[i] if i < len(MAJOR_INDICES) else {}
            price = item.get('f2', 0)     # 最新价
            change = item.get('f4', 0)    # 涨跌额
            pct = item.get('f3', 0)       # 涨跌幅%
            volume = item.get('f5', 0)    # 成交量(手)
            amount = item.get('f6', 0)    # 成交额
            high = item.get('f15', 0)     # 最高
            low = item.get('f16', 0)      # 最低
            open_ = item.get('f17', 0)    # 开盘
            prev_close = item.get('f18', 0)  # 昨收
            amplitude = item.get('f7', 0)    # 振幅%

            result.append({
                'secid': idx_info.get('secid', ''),
                'name': idx_info.get('name', item.get('f14', '')),
                'short': idx_info.get('short', ''),
                'price': price if price != '-' else 0,
                'change': change if change != '-' else 0,
                'pct': pct if pct != '-' else 0,
                'volume': volume,
                'amount': amount,
                'high': high if high != '-' else 0,
                'low': low if low != '-' else 0,
                'open': open_ if open_ != '-' else 0,
                'prev_close': prev_close if prev_close != '-' else 0,
                'amplitude': amplitude if amplitude != '-' else 0,
            })

        _cache_set('indices', result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch major indices: %s', e, exc_info=not _is_timeout(e))
        return []


# ═══════════════════════════════════════════════════════════
#  Sector Performance (板块行情)
# ═══════════════════════════════════════════════════════════

def fetch_sector_performance(*, client=None):
    """Fetch industry sector performance — used for heatmap.

    Uses eastmoney board API for 申万行业 sectors.
    """
    cached = _cache_get('sectors')
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return []

    url = (
        'http://push2delay.eastmoney.com/api/qt/clist/get?'
        'pn=1&pz=50&po=1&np=1&fltt=2&invt=2&fid=f3'
        '&fs=m:90+t:2+f:!50'
        '&fields=f2,f3,f4,f12,f14,f104,f105,f128,f136,f140,f141'
        '&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        data = json.loads(m.group(1)) if m else r.json()

        result = []
        for item in (data.get('data', {}) or {}).get('diff', []) or []:
            name = item.get('f14', '')
            code = item.get('f12', '')
            pct = item.get('f3', 0)
            price = item.get('f2', 0)
            change = item.get('f4', 0)
            up_count = item.get('f104', 0)    # 上涨家数
            down_count = item.get('f105', 0)  # 下跌家数
            lead_stock = item.get('f140', '')  # 领涨股
            lead_pct = item.get('f136', 0)    # 领涨股涨幅
            lead_name = item.get('f128', '')  # 领涨股名称

            result.append({
                'code': code,
                'name': name,
                'pct': pct if pct != '-' else 0,
                'price': price if price != '-' else 0,
                'change': change if change != '-' else 0,
                'up_count': up_count or 0,
                'down_count': down_count or 0,
                'lead_stock': lead_name or lead_stock,
                'lead_pct': lead_pct if lead_pct != '-' else 0,
            })

        _cache_set('sectors', result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch sector performance: %s', e, exc_info=not _is_timeout(e))
        return []


# ═══════════════════════════════════════════════════════════
#  Market Breadth (涨跌统计)
# ═══════════════════════════════════════════════════════════

def fetch_market_breadth(*, client=None):
    """Fetch A-share market breadth: up/down/flat counts + limit up/down.

    Uses eastmoney market summary API.
    """
    cached = _cache_get('breadth')
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return {}

    # Fetch all A-shares stats
    url = (
        'http://push2delay.eastmoney.com/api/qt/clist/get?'
        'pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f3'
        '&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
        '&fields=f3,f104,f105,f106'
        '&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        data = json.loads(m.group(1)) if m else r.json()

        total_data = data.get('data', {}) or {}
        total = total_data.get('total', 0)
        if not isinstance(total, (int, float)):
            total = 0
        # f104 = 上涨, f105 = 下跌, f106 = 持平
        diff = total_data.get('diff', [{}])
        market_closed = False
        after_hours = False
        if diff and isinstance(diff, list) and isinstance(diff[0], dict):
            item = diff[0]
            up_count = item.get('f104', 0)
            down_count = item.get('f105', 0)
            flat_count = item.get('f106', 0)
            # When market is closed / after hours, API returns '-' for all three fields
            if up_count == '-' and down_count == '-' and flat_count == '-':
                if _is_after_hours():
                    after_hours = True
                    logger.debug('[Market] Breadth: after hours (all fields are "-", weekday post-close)')
                else:
                    market_closed = True
                    logger.debug('[Market] Breadth: market closed/holiday (all fields are "-")')
        else:
            up_count = down_count = flat_count = 0

        # Eastmoney sometimes returns strings (e.g. '-') for these fields
        if not isinstance(up_count, (int, float)):   up_count = 0
        if not isinstance(down_count, (int, float)): down_count = 0
        if not isinstance(flat_count, (int, float)): flat_count = 0

        # NOTE: limit-up/limit-down counts are hardcoded to 0 because computing
        #       them requires fetching the full A-share stock list and filtering by
        #       pct_change >= 9.9% / <= -9.9%, which is too expensive for this endpoint.
        limit_up = 0
        limit_down = 0

        result = {
            'total': total,
            'up': up_count or 0,
            'down': down_count or 0,
            'flat': flat_count or 0,
            'limit_up': limit_up,
            'limit_down': limit_down,
            'up_pct': round(up_count / max(total, 1) * 100, 1) if total else 0,
            'market_closed': market_closed,
            'after_hours': after_hours,
        }

        _cache_set('breadth', result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch market breadth: %s', e, exc_info=not _is_timeout(e))
        return {}


# ═══════════════════════════════════════════════════════════
#  Northbound Capital Flow (北向资金)
# ═══════════════════════════════════════════════════════════

def fetch_northbound_flow(*, client=None):
    """Fetch northbound capital flow data (沪股通 + 深股通).

    Returns today's net inflow in 亿元.
    """
    cached = _cache_get('northbound')
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return {}

    url = (
        'http://push2delay.eastmoney.com/api/qt/kamt.rtmin/get?'
        'fields1=f1,f2,f3,f4'
        '&fields2=f51,f52,f53,f54,f55,f56'
        '&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://data.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        data = json.loads(m.group(1)) if m else r.json()

        d = data.get('data', {}) or {}
        # n2s = 北向资金 (northbound: HK → A shares)
        # API returns list of minute-level CSV strings:
        #   "time,沪股通净流入,沪股通额度,深股通净流入,深股通额度,合计净流入"
        # Values are in 万元 (ten-thousands of yuan).
        n2s_raw = d.get('n2s', [])
        minutes = []
        sh_net = '--'
        sz_net = '--'
        total_net = '--'

        if isinstance(n2s_raw, list) and n2s_raw:
            minutes = n2s_raw
            # Parse the last entry for aggregate totals
            last_entry = n2s_raw[-1]
            try:
                parts = last_entry.split(',')
                if len(parts) >= 6:
                    sh_net = _safe_float(parts[1], '--')
                    sz_net = _safe_float(parts[3], '--')
                    total_net = _safe_float(parts[5], '--')
                    logger.debug('[Market] Northbound last entry: time=%s sh=%.0f sz=%.0f total=%.0f',
                                 parts[0], sh_net if sh_net != '--' else 0,
                                 sz_net if sz_net != '--' else 0,
                                 total_net if total_net != '--' else 0)
            except (IndexError, ValueError) as e:
                logger.warning('[Market] Failed to parse northbound last entry: %s — raw: %.200s', e, last_entry)
        elif isinstance(n2s_raw, dict):
            # Legacy dict format (f1/f2/f3 keys) — keep for backward compat
            sh_net = n2s_raw.get('f1', '--')
            sz_net = n2s_raw.get('f2', '--')
            total_net = n2s_raw.get('f3', '--')
            minutes = n2s_raw.get('f4', []) or []
        else:
            logger.warning('[Market] Unexpected n2s type: %s — preview: %.200s',
                           type(n2s_raw).__name__, str(n2s_raw)[:200])

        # Detect market closed vs after-hours: all minute entries have 0.00 net flow
        nb_market_closed = False
        nb_after_hours = False
        if isinstance(total_net, (int, float)) and total_net == 0:
            # Check if ALL minute entries show zero (market closed / pre-open / after hours)
            non_zero = any(
                ',' in entry and any(
                    _safe_float(p, 0) != 0
                    for p in entry.split(',')[1::2]  # odd-indexed = net flow columns
                )
                for entry in (n2s_raw[:5] if isinstance(n2s_raw, list) else [])
            )
            if not non_zero:
                if _is_after_hours():
                    nb_after_hours = True
                    logger.debug('[Market] Northbound: after hours (all entries zero, weekday post-close)')
                else:
                    nb_market_closed = True
                    logger.debug('[Market] Northbound: market closed/holiday (all entries zero)')

        result = {
            'sh_net': sh_net,   # 沪股通 in 万元
            'sz_net': sz_net,   # 深股通 in 万元
            'total_net': total_net,  # 合计 in 万元
            'sh_net_yi': round(sh_net / 10000, 2) if isinstance(sh_net, (int, float)) else '--',
            'sz_net_yi': round(sz_net / 10000, 2) if isinstance(sz_net, (int, float)) else '--',
            'total_net_yi': round(total_net / 10000, 2) if isinstance(total_net, (int, float)) else '--',
            'minutes': minutes,
            'date': d.get('n2sDate', ''),
            'market_closed': nb_market_closed,
            'after_hours': nb_after_hours,
        }

        _cache_set('northbound', result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch northbound flow: %s', e, exc_info=not _is_timeout(e))
        return {}


# ═══════════════════════════════════════════════════════════
#  Index Intraday Trend (分时走势)
# ═══════════════════════════════════════════════════════════

def fetch_index_trend(secid='1.000001', *, client=None):
    """Fetch intraday minute-level trend for an index.

    Args:
        secid: eastmoney secid format (market.code), default 上证指数.
    """
    cache_key = f'trend:{secid}'
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return {}

    url = (
        f'http://push2delay.eastmoney.com/api/qt/stock/trends2/get?'
        f'secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13'
        f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58'
        f'&iscr=0&ndays=1&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m_re = re.search(r'jQuery\((.*)\)', text, re.S)
        data = json.loads(m_re.group(1)) if m_re else r.json()

        d = data.get('data', {}) or {}
        prev_close = d.get('preClose', 0)
        name = d.get('name', '')
        trends = d.get('trends', []) or []

        # Parse trend lines: "2025/01/15 09:31,3234.56,3235.12,3233.89,3234.88,12345,678901234"
        points = []
        for t in trends:
            parts = t.split(',')
            if len(parts) >= 6:
                points.append({
                    'time': parts[0].split(' ')[-1] if ' ' in parts[0] else parts[0],
                    'price': float(parts[1]) if parts[1] != '-' else 0,
                    'avg': float(parts[2]) if parts[2] != '-' else 0,
                    'volume': int(parts[5]) if parts[5] not in ('-', '') else 0,
                })

        result = {
            'secid': secid,
            'name': name,
            'prev_close': prev_close,
            'points': points,
        }

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch index trend for %s: %s', secid, e, exc_info=not _is_timeout(e))
        return {}


# ═══════════════════════════════════════════════════════════
#  Top Performing Funds (ETF/股票涨幅榜)
# ═══════════════════════════════════════════════════════════

def fetch_top_assets(sort='day', limit=20, *, client=None):
    """Fetch top performing ETFs/stocks.

    Args:
        sort: 'day' (日涨幅), 'week' (周涨幅), 'month' (月涨幅),
              '3month', 'year', 'ytd'
        limit: number of assets to return
    """
    cache_key = f'top_assets:{sort}'
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return []

    sort_map = {
        'day': 'dm',       # 日增长值
        'week': 'Z',       # 近1周
        'month': 'Y',      # 近1月
        '3month': '3Y',    # 近3月
        '6month': '6Y',    # 近6月
        'year': '1N',      # 近1年
        'ytd': 'JN',       # 今年来
    }
    sc = sort_map.get(sort, 'dm')
    st = 'desc'

    url = (
        f'http://fund.eastmoney.com/data/rankhandler.aspx?'
        f'op=ph&dt=kf&ft=all&rs=&gs=0&sc={sc}&st={st}'
        f'&pi=1&pn={limit}'
        f'&dx=1&v=0.{int(time.time()*1000)}'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'http://fund.eastmoney.com/data/fundranking.html',
        })
        text = r.text

        # Parse response: var rankData = { ... datas:["...","..."], ... }
        datas_m = re.search(r'datas:\[(.*?)\]', text, re.S)
        if not datas_m:
            return []

        raw_items = datas_m.group(1).split('","')
        result = []
        for raw in raw_items:
            raw = raw.strip('"')
            parts = raw.split(',')
            if len(parts) < 25:
                continue
            try:
                code = parts[0]
                name = parts[1]
                nav = _safe_float(parts[3])
                nav_date = parts[4]
                day_pct = _safe_float(parts[6])
                week_pct = _safe_float(parts[7])
                month_pct = _safe_float(parts[8])
                m3_pct = _safe_float(parts[9])
                m6_pct = _safe_float(parts[10])
                y1_pct = _safe_float(parts[11])
                ytd_pct = _safe_float(parts[14])
                asset_type = parts[18] if len(parts) > 18 else ''

                result.append({
                    'code': code,
                    'name': name,
                    'nav': nav,
                    'nav_date': nav_date,
                    'day_pct': day_pct,
                    'week_pct': week_pct,
                    'month_pct': month_pct,
                    '3m_pct': m3_pct,
                    '6m_pct': m6_pct,
                    '1y_pct': y1_pct,
                    'ytd_pct': ytd_pct,
                    'type': asset_type,
                })
            except (ValueError, IndexError) as exc:
                logger.debug('[Market] Skipping malformed asset row: %s', exc)
                continue

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning('[Market] Failed to fetch top assets (sort=%s): %s', sort, e, exc_info=not _is_timeout(e))
        return []


# Consolidated into lib.utils — single source of truth
from lib.utils import safe_float as _safe_float

# ═══════════════════════════════════════════════════════════
#  Combined Market Overview
# ═══════════════════════════════════════════════════════════

def fetch_market_overview(*, client=None):
    """Fetch all market data in parallel for the dashboard.

    Returns a combined dict with all market components.
    """
    if client is None:
        client = _get_default_client()

    results = {}

    def _fetch(key, fn, **kwargs):
        try:
            results[key] = fn(client=client, **kwargs)
        except Exception as e:
            logger.warning('[Market] %s fetch failed: %s', key, e, exc_info=not _is_timeout(e))
            results[key] = [] if key in ('indices', 'sectors', 'top_assets') else {}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(_fetch, 'indices', fetch_major_indices),
            pool.submit(_fetch, 'sectors', fetch_sector_performance),
            pool.submit(_fetch, 'breadth', fetch_market_breadth),
            pool.submit(_fetch, 'northbound', fetch_northbound_flow),
            pool.submit(_fetch, 'trend', fetch_index_trend, secid='1.000001'),
            pool.submit(_fetch, 'top_assets', fetch_top_assets, sort='day', limit=15),
        ]
        for f in as_completed(futs, timeout=10):
            try:
                f.result()
            except Exception as e:
                logger.warning('[Market] parallel fetch error: %s', e, exc_info=not _is_timeout(e))

    results['timestamp'] = time.time()
    return results
