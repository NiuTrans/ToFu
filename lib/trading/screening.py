"""lib/trading/screening.py — ETF Fund & Stock Screening Stock Screening Engine.

Multi-dimensional screening that combines:
  1. Performance ranking from eastmoney (top assets by period)
  2. Quantitative signal scoring (asset_signals engine)
  3. Risk profiling (trading_risk engine)
  4. Intelligence overlay (sentiment from intel cache)
  5. Strategy alignment (match candidates to active strategies)

Screening pipeline:
  discover → filter → score → rank → select → backtest → recommend

Also provides stock-level screening for A-share equities via eastmoney APIs.
"""

import json
import math
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from lib.log import get_logger
from lib.trading._common import TradingClient, _get_default_client

logger = get_logger(__name__)

__all__ = [
    'screen_assets',
    'screen_stocks',
    'smart_select_assets',
    'fetch_asset_ranking',
    'fetch_stock_list',
    'fetch_asset_detail_batch',
    'score_asset_candidate',
    'run_screening_pipeline',
]

# ── In-memory screening cache ──
_screen_cache = {}
_screen_lock = threading.Lock()
_SCREEN_CACHE_TTL = 600  # 10 min


def _cache_get(key):
    with _screen_lock:
        entry = _screen_cache.get(key)
    if entry and (time.time() - entry['ts']) < _SCREEN_CACHE_TTL:
        return entry['data']
    return None


def _cache_set(key, data):
    with _screen_lock:
        _screen_cache[key] = {'data': data, 'ts': time.time()}


# Consolidated into lib.utils — single source of truth
from lib.utils import safe_float as _safe_float

# ═══════════════════════════════════════════════════════════
#  Fund Ranking / Discovery (from eastmoney)
# ═══════════════════════════════════════════════════════════

# Fund type codes for eastmoney API
FUND_TYPE_MAP = {
    'all': 'all',       # 全部开放式
    'stock': 'gp',      # 股票型
    'mixed': 'hh',      # 混合型
    'bond': 'zq',       # 债券型
    'index': 'zs',      # 指数型
    'qdii': 'qdii',     # QDII
    'etf': 'etf',       # ETF联接
    'fof': 'fof',       # FOF
    'money': 'hb',      # 货币型
}


def fetch_asset_ranking(asset_type='all', sort='3month', limit=100,
                       min_size=0, *, client=None):
    """Fetch asset ranking from eastmoney with filtering.

    Args:
        asset_type: 'all','stock','mixed','bond','index','qdii','etf','fof','money'
        sort: 'day','week','month','3month','6month','year','2year','3year','5year','ytd'
        limit: number of results (max 500)
        min_size: minimum asset size in 亿 (0 = no filter)
        client: Optional TradingClient for DI.

    Returns list of asset dicts with performance metrics.
    """
    cache_key = f'ranking:{asset_type}:{sort}:{limit}'
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return []

    ft = FUND_TYPE_MAP.get(asset_type, 'all')
    sort_map = {
        'day': 'dm', 'week': 'Z', 'month': 'Y', '3month': '3Y',
        '6month': '6Y', 'year': '1N', '2year': '2N', '3year': '3N',
        '5year': '5N', 'ytd': 'JN',
    }
    sc = sort_map.get(sort, '3Y')

    url = (
        f'https://fund.eastmoney.com/data/rankhandler.aspx?'
        f'op=ph&dt=kf&ft={ft}&rs=&gs=0&sc={sc}&st=desc'
        f'&pi=1&pn={min(limit, 500)}'
        f'&dx=1&v=0.{int(time.time()*1000)}'
    )
    try:
        r = client.session.get(url, timeout=8, headers={
            **client.headers,
            'Referer': 'https://fund.eastmoney.com/data/fundranking.html',
        })
        text = r.text
        datas_m = re.search(r'datas:\[(.*?)\]', text, re.S)
        if not datas_m:
            return []

        raw_items = datas_m.group(1).split('","')
        result = []
        for raw in raw_items:
            raw = raw.strip('"')
            parts = raw.split(',')
            # Eastmoney data has 24-25 fields depending on asset type.
            # Core fields [0]-[15] are always present; [16]=inception date,
            # [17]=type code, [18]=since_inception_pct, [19]=front fee,
            # [20]=discount fee, [21]=?, [22]=?, [23]=?, [24]=size(亿).
            if len(parts) < 17:
                continue
            try:
                # Size is at index 24 when present; some types omit it
                size_raw = parts[24] if len(parts) > 24 else ''
                # Strip possible '%' suffix (some rows have fee in wrong slot)
                size_val = _safe_float(size_raw.rstrip('%')) if size_raw and '%' not in size_raw else 0
                asset = {
                    'code': parts[0],
                    'name': parts[1],
                    'nav_date': parts[3],
                    'nav': _safe_float(parts[4]),
                    'day_pct': _safe_float(parts[6]),
                    'week_pct': _safe_float(parts[7]),
                    'month_pct': _safe_float(parts[8]),
                    '3m_pct': _safe_float(parts[9]),
                    '6m_pct': _safe_float(parts[10]),
                    '1y_pct': _safe_float(parts[11]),
                    '2y_pct': _safe_float(parts[12]),
                    '3y_pct': _safe_float(parts[13]),
                    'ytd_pct': _safe_float(parts[14]),
                    'since_inception_pct': _safe_float(parts[15]),
                    'inception_date': parts[16] if len(parts) > 16 else '',
                    'asset_type_code': parts[17] if len(parts) > 17 else '',
                    'size': size_val,
                    'manager': '',  # Manager not available in ranking API
                }
                # Size filter (in 亿)
                if min_size > 0 and asset['size'] < min_size:
                    continue
                result.append(asset)
            except (ValueError, IndexError) as exc:
                logger.debug('[Screening] Skipping malformed asset row: %s', exc)
                continue

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning('[Screening] Fund ranking fetch failed (type=%s, sort=%s): %s',
                       asset_type, sort, e, exc_info=True)
        return []


def fetch_asset_detail_batch(codes, *, client=None):
    """Fetch detailed info for multiple asset codes in parallel.

    Returns {code: {name, type, size, manager, ...}}
    """
    if client is None:
        client = _get_default_client()
    if not client.check_network() or not codes:
        return {}

    results = {}

    def _fetch_one(code):
        try:
            from lib.trading.info import fetch_asset_info
            info = fetch_asset_info(code, client=client)
            return code, info
        except Exception as e:
            logger.debug('[Trading] screening fetch failed for %s: %s', code, e)
            return code, None

    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as pool:
        futs = {pool.submit(_fetch_one, c): c for c in codes}
        for f in as_completed(futs, timeout=10):
            try:
                code, info = f.result()
                if info:
                    results[code] = info
            except Exception as e:
                logger.debug('[Trading] screening future failed: %s', e)

    return results


# ═══════════════════════════════════════════════════════════
#  Stock Screening (A-share equities from eastmoney)
# ═══════════════════════════════════════════════════════════

def fetch_stock_list(market='all', sort='pct', order='desc', limit=50,
                     min_market_cap=0, sector=None, *, client=None):
    """Fetch A-share stock list from eastmoney push API.

    Args:
        market: 'all','sh' (沪市),'sz' (深市),'cyb' (创业板),'kc' (科创板)
        sort: 'pct' (涨幅), 'volume' (成交量), 'amount' (成交额), 'market_cap' (总市值)
        order: 'desc' or 'asc'
        limit: number of stocks
        min_market_cap: minimum market cap in 亿
        sector: industry sector name filter (optional)
        client: Optional TradingClient for DI.

    Returns list of stock dicts.
    """
    cache_key = f'stocks:{market}:{sort}:{order}:{limit}'
    cached = _cache_get(cache_key)
    if cached:
        return cached

    if client is None:
        client = _get_default_client()
    if not client.check_network():
        return []

    # Market filter
    market_map = {
        'all': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
        'sh': 'm:1+t:2,m:1+t:23',
        'sz': 'm:0+t:6,m:0+t:80',
        'cyb': 'm:0+t:80',     # 创业板
        'kc': 'm:1+t:23',       # 科创板
    }
    fs = market_map.get(market, market_map['all'])

    # Sort field
    sort_map = {
        'pct': 'f3',          # 涨跌幅
        'volume': 'f5',       # 成交量
        'amount': 'f6',       # 成交额
        'market_cap': 'f20',  # 总市值
        'pe': 'f9',           # 市盈率
        'pb': 'f23',          # 市净率
        'turnover': 'f8',     # 换手率
    }
    fid = sort_map.get(sort, 'f3')
    po = 0 if order == 'asc' else 1

    url = (
        f'http://push2delay.eastmoney.com/api/qt/clist/get?'
        f'pn=1&pz={min(limit, 200)}&po={po}&np=1&fltt=2&invt=2&fid={fid}'
        f'&fs={fs}'
        f'&fields=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26'
        f'&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=8, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        data = json.loads(m.group(1)) if m else r.json()

        result = []
        for item in (data.get('data', {}) or {}).get('diff', []) or []:
            code = item.get('f12', '')
            name = item.get('f14', '')
            market_id = item.get('f13', 0)  # 0=深, 1=沪
            price = _safe_float(item.get('f2'))
            pct = _safe_float(item.get('f3'))
            change = _safe_float(item.get('f4'))
            volume = item.get('f5', 0)       # 成交量(手)
            amount = _safe_float(item.get('f6', 0))  # 成交额
            amplitude = _safe_float(item.get('f7'))   # 振幅%
            turnover = _safe_float(item.get('f8'))    # 换手率%
            pe = _safe_float(item.get('f9'))          # PE(TTM)
            pb = _safe_float(item.get('f23'))         # PB
            total_mv = _safe_float(item.get('f20', 0))    # 总市值
            circ_mv = _safe_float(item.get('f21', 0))     # 流通市值
            high = _safe_float(item.get('f15'))
            low = _safe_float(item.get('f16'))
            open_ = _safe_float(item.get('f17'))
            prev_close = _safe_float(item.get('f18'))

            # Filter by market cap (convert from 元 to 亿)
            total_mv_yi = total_mv / 1e8 if total_mv > 0 else 0
            if min_market_cap > 0 and total_mv_yi < min_market_cap:
                continue

            # Sector filter (basic — by name matching)
            if sector and sector not in name:
                continue

            stock = {
                'code': code,
                'name': name,
                'market': '沪' if market_id == 1 else '深',
                'secid': f'{market_id}.{code}',
                'price': price,
                'pct': pct,
                'change': change,
                'volume': volume,
                'amount': amount,
                'amplitude': amplitude,
                'turnover': turnover,
                'pe': pe,
                'pb': pb,
                'total_mv': total_mv,
                'total_mv_yi': round(total_mv_yi, 2),
                'circ_mv': circ_mv,
                'circ_mv_yi': round(circ_mv / 1e8, 2) if circ_mv > 0 else 0,
                'high': high,
                'low': low,
                'open': open_,
                'prev_close': prev_close,
            }
            result.append(stock)

        _cache_set(cache_key, result)
        return result

    except Exception as e:
        logger.warning('[Screening] Stock list fetch failed: %s', e, exc_info=True)
        return []


def screen_stocks(criteria=None, *, client=None):
    """Screen A-share stocks by multiple criteria.

    Args:
        criteria: dict with keys:
            market: 'all','sh','sz','cyb','kc'
            min_pe: minimum PE ratio
            max_pe: maximum PE ratio
            min_pb: minimum PB ratio
            max_pb: maximum PB ratio
            min_market_cap: minimum market cap in 亿
            max_market_cap: maximum market cap in 亿
            min_turnover: minimum turnover rate %
            min_pct: minimum daily change %
            max_pct: maximum daily change %
            sector: sector name filter
            sort: 'pct','volume','amount','market_cap','pe','pb','turnover'
            limit: number of results
        client: Optional TradingClient.

    Returns:
        {stocks: [...], criteria_applied: {...}, total_matched: int}
    """
    criteria = criteria or {}
    market = criteria.get('market', 'all')
    sort = criteria.get('sort', 'pct')
    limit = int(criteria.get('limit', 100))

    # Fetch raw data
    raw = fetch_stock_list(
        market=market, sort=sort, order=criteria.get('order', 'desc'),
        limit=max(limit * 3, 200),  # over-fetch for post-filtering
        min_market_cap=float(criteria.get('min_market_cap', 0)),
        sector=criteria.get('sector'),
        client=client,
    )

    # Post-filtering
    filtered = []
    for s in raw:
        if criteria.get('min_pe') and (s['pe'] <= 0 or s['pe'] < float(criteria['min_pe'])):
            continue
        if criteria.get('max_pe') and s['pe'] > 0 and s['pe'] > float(criteria['max_pe']):
            continue
        if criteria.get('min_pb') and (s['pb'] <= 0 or s['pb'] < float(criteria['min_pb'])):
            continue
        if criteria.get('max_pb') and s['pb'] > 0 and s['pb'] > float(criteria['max_pb']):
            continue
        if criteria.get('max_market_cap') and s['total_mv_yi'] > float(criteria['max_market_cap']):
            continue
        if criteria.get('min_turnover') and s['turnover'] < float(criteria['min_turnover']):
            continue
        if criteria.get('min_pct') is not None and s['pct'] < float(criteria['min_pct']):
            continue
        if criteria.get('max_pct') is not None and s['pct'] > float(criteria['max_pct']):
            continue
        filtered.append(s)
        if len(filtered) >= limit:
            break

    return {
        'stocks': filtered,
        'criteria_applied': criteria,
        'total_matched': len(filtered),
        'timestamp': time.time(),
    }


# ═══════════════════════════════════════════════════════════
#  Fund Screening (Multi-Dimensional Scoring)
# ═══════════════════════════════════════════════════════════

def score_asset_candidate(code, navs, *, intel_sentiment=None, strategies=None):
    """Score a asset candidate across multiple dimensions.

    Dimensions (weights):
      1. Performance (30%): Returns across multiple periods
      2. Risk-adjusted (25%): Sharpe, Sortino, max drawdown
      3. Signal strength (20%): Current quantitative signals
      4. Stability (15%): Consistency of returns, volatility
      5. Intel alignment (10%): Sentiment from intelligence

    Args:
        code: symbol
        navs: list of {'date': str, 'nav': float}
        intel_sentiment: overall market sentiment from intel ('bullish','neutral','bearish')
        strategies: list of active strategy dicts

    Returns:
        {total_score, dimension_scores: {...}, signal_snapshot, risk_profile, recommendation}
    """
    if not navs or len(navs) < 60:
        return {'error': 'Insufficient data', 'data_points': len(navs) if navs else 0}

    n = len(navs)

    # ── 1. Performance Score (30%) ──
    perf_score = 0
    returns = {}
    if n >= 20:
        r20 = (navs[-1]['nav'] - navs[-20]['nav']) / navs[-20]['nav'] * 100
        returns['1m'] = round(r20, 2)
        if r20 > 5: perf_score += 30
        elif r20 > 2: perf_score += 20
        elif r20 > 0: perf_score += 10
        elif r20 > -3: perf_score += 0
        else: perf_score -= 15
    if n >= 60:
        r60 = (navs[-1]['nav'] - navs[-60]['nav']) / navs[-60]['nav'] * 100
        returns['3m'] = round(r60, 2)
        if r60 > 10: perf_score += 35
        elif r60 > 5: perf_score += 25
        elif r60 > 0: perf_score += 10
        else: perf_score -= 10
    if n >= 120:
        r120 = (navs[-1]['nav'] - navs[-120]['nav']) / navs[-120]['nav'] * 100
        returns['6m'] = round(r120, 2)
        if r120 > 15: perf_score += 20
        elif r120 > 5: perf_score += 10
        elif r120 > 0: perf_score += 5
    if n >= 250:
        r250 = (navs[-1]['nav'] - navs[-250]['nav']) / navs[-250]['nav'] * 100
        returns['1y'] = round(r250, 2)
        if r250 > 20: perf_score += 15
        elif r250 > 10: perf_score += 10
        elif r250 > 0: perf_score += 5

    perf_score = max(-100, min(100, perf_score))

    # ── 2. Risk-Adjusted Score (25%) ──
    risk_score = 0
    risk_metrics = {}
    daily_rets = [(navs[i]['nav'] - navs[i-1]['nav']) / navs[i-1]['nav']
                  for i in range(1, n) if navs[i-1]['nav'] > 0]
    if daily_rets:
        avg_ret = sum(daily_rets) / len(daily_rets)
        std_ret = (sum((r - avg_ret) ** 2 for r in daily_rets) / max(len(daily_rets) - 1, 1)) ** 0.5
        ann_ret = avg_ret * 252
        ann_vol = std_ret * math.sqrt(252)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0

        # Sortino (downside deviation)
        downside_rets = [r for r in daily_rets if r < 0]
        downside_std = (sum(r ** 2 for r in downside_rets) / max(len(downside_rets), 1)) ** 0.5
        sortino = avg_ret * 252 / (downside_std * math.sqrt(252)) if downside_std > 0 else 0

        # Max drawdown
        peak = navs[0]['nav']
        max_dd = 0
        for point in navs:
            peak = max(peak, point['nav'])
            dd = (point['nav'] - peak) / peak * 100
            max_dd = min(max_dd, dd)

        risk_metrics = {
            'sharpe': round(sharpe, 3),
            'sortino': round(sortino, 3),
            'annual_vol': round(ann_vol * 100, 2),
            'max_drawdown': round(max_dd, 2),
            'annual_return': round(ann_ret * 100, 2),
        }

        # Score risk-adjusted
        if sharpe > 1.5: risk_score += 40
        elif sharpe > 1.0: risk_score += 30
        elif sharpe > 0.5: risk_score += 20
        elif sharpe > 0: risk_score += 10
        else: risk_score -= 20

        if max_dd > -10: risk_score += 30
        elif max_dd > -20: risk_score += 15
        elif max_dd > -30: risk_score += 0
        else: risk_score -= 15

        if sortino > 2.0: risk_score += 15
        elif sortino > 1.0: risk_score += 10
        elif sortino > 0.5: risk_score += 5

    risk_score = max(-100, min(100, risk_score))

    # ── 3. Signal Score (20%) ──
    signal_score = 0
    signal_snapshot = {}
    try:
        from lib.trading_signals import compute_signal_snapshot
        signal_snapshot = compute_signal_snapshot(navs)
        if 'error' not in signal_snapshot:
            cs = signal_snapshot.get('composite_score', 0)
            signal_score = max(-100, min(100, cs * 2))  # scale composite to [-100, 100]
    except Exception as e:
        logger.debug('[Screening] Signal computation failed for %s: %s', code, e, exc_info=True)

    # ── 4. Stability Score (15%) ──
    stability_score = 0
    stability_metrics = {}
    if len(daily_rets) >= 20:
        # Monthly return consistency
        monthly_rets = []
        for i in range(0, len(daily_rets), 20):
            chunk = daily_rets[i:i+20]
            if chunk:
                monthly_rets.append(sum(chunk))
        if monthly_rets:
            pos_months = sum(1 for r in monthly_rets if r > 0)
            win_rate = pos_months / len(monthly_rets)
            ret_std = (sum((r - sum(monthly_rets)/len(monthly_rets))**2
                          for r in monthly_rets) / max(len(monthly_rets)-1, 1)) ** 0.5

            stability_metrics = {
                'monthly_win_rate': round(win_rate * 100, 1),
                'monthly_return_std': round(ret_std * 100, 2),
                'months_analyzed': len(monthly_rets),
            }

            if win_rate > 0.7: stability_score += 50
            elif win_rate > 0.6: stability_score += 35
            elif win_rate > 0.5: stability_score += 20
            else: stability_score -= 10

            if ret_std < 0.03: stability_score += 30
            elif ret_std < 0.05: stability_score += 15
            elif ret_std < 0.08: stability_score += 5
            else: stability_score -= 10

    stability_score = max(-100, min(100, stability_score))

    # ── 5. Intel Alignment Score (10%) ──
    # Maps all (sentiment × regime) combos to a score:
    #   Aligned bullish  → strong positive (intel confirms technical uptrend)
    #   Aligned bearish  → strong negative (both say avoid)
    #   Bullish + bear   → conflicting signal → mild negative (be cautious)
    #   Bearish + bull   → contrarian opportunity → mild positive
    #   Sideways combos  → moderate directional nudge
    intel_score = 0
    if intel_sentiment:
        regime = signal_snapshot.get('trend_regime', 'sideways') if signal_snapshot else 'sideways'
        _intel_matrix = {
            ('bullish', 'strong_bull'): 70,    # perfect alignment
            ('bullish', 'bull'):        60,    # good alignment
            ('bullish', 'sideways'):    30,    # intel positive, trend neutral
            ('bullish', 'bear'):       -15,    # conflicting: intel up, trend down
            ('bullish', 'strong_bear'):-30,    # strong conflict: risky
            ('bearish', 'strong_bear'):-60,    # aligned bearish — strong avoid
            ('bearish', 'bear'):       -50,    # aligned bearish — avoid
            ('bearish', 'sideways'):   -15,    # intel negative, trend neutral
            ('bearish', 'bull'):        20,    # contrarian opportunity
            ('bearish', 'strong_bull'): 10,    # contrarian but risky
            ('neutral', 'strong_bull'): 25,    # neutral intel, strong trend
            ('neutral', 'bull'):        15,
            ('neutral', 'sideways'):     0,
            ('neutral', 'bear'):       -10,
            ('neutral', 'strong_bear'):-20,
        }
        intel_score = _intel_matrix.get((intel_sentiment, regime), 0)

    # ── 6. Strategy Alignment Bonus ──
    # When active strategies are provided, compute a bonus/penalty based on
    # how well this asset aligns with the user's stated strategy keywords.
    # This is additive (not weighted into the 5-dimension system) so it
    # preserves backward compatibility with existing scoring.
    strategy_bonus = 0
    strategy_alignment = {}
    if strategies:
        regime = signal_snapshot.get('trend_regime', 'sideways') if signal_snapshot else 'sideways'
        asset_signal = signal_snapshot.get('signal', 'neutral') if signal_snapshot else 'neutral'
        latest_sharpe = risk_metrics.get('sharpe', 0)
        latest_dd = risk_metrics.get('max_drawdown', 0)

        for s in strategies:
            logic = (s.get('logic', '') + ' ' + s.get('scenario', '')).lower()
            s_name = s.get('name', 'unknown')

            # Keyword matching: does this asset's profile match the strategy?
            alignment = 0
            reasons = []

            # Trend/momentum strategies prefer bullish regime + buy signals
            if any(kw in logic for kw in ('趋势', '动量', 'momentum', 'trend', '追涨')):
                if regime in ('strong_bull', 'bull'):
                    alignment += 15
                    reasons.append('trend ↑')
                elif regime in ('strong_bear', 'bear'):
                    alignment -= 10
                    reasons.append('trend ↓')

            # Value/contrarian strategies prefer oversold + dip conditions
            if any(kw in logic for kw in ('价值', '低估', 'value', '逢低', '抄底', 'contrarian')):
                if regime in ('strong_bear', 'bear') and asset_signal in ('buy', 'strong_buy'):
                    alignment += 20
                    reasons.append('value dip buy')
                elif regime in ('strong_bull', 'bull') and asset_signal in ('sell', 'strong_sell'):
                    alignment -= 5
                    reasons.append('overvalued')

            # Defensive strategies prefer low drawdown + stable returns
            if any(kw in logic for kw in ('防守', '稳健', 'defensive', '低风险', '保守')):
                if latest_dd > -10:
                    alignment += 10
                    reasons.append('low DD')
                elif latest_dd < -25:
                    alignment -= 15
                    reasons.append('high DD')
                if latest_sharpe > 1.0:
                    alignment += 5
                    reasons.append('good Sharpe')

            # Growth strategies prefer strong performance + signals
            if any(kw in logic for kw in ('成长', 'growth', '高增长', '进攻')):
                r3m = returns.get('3m', 0)
                if r3m > 10:
                    alignment += 15
                    reasons.append(f'3m +{r3m:.0f}%')
                elif r3m < -5:
                    alignment -= 10
                    reasons.append(f'3m {r3m:.0f}%')

            # Income/dividend strategies prefer stable + positive returns
            if any(kw in logic for kw in ('分红', '收益', 'income', 'dividend', '定投')):
                monthly_wr = stability_metrics.get('monthly_win_rate', 50)
                if monthly_wr > 60:
                    alignment += 10
                    reasons.append(f'win rate {monthly_wr:.0f}%')

            if alignment != 0:
                strategy_alignment[s_name] = {
                    'score': alignment,
                    'reasons': reasons,
                }

            strategy_bonus += alignment

        # Cap strategy bonus to ±20 to prevent dominating other dimensions
        strategy_bonus = max(-20, min(20, strategy_bonus))

    # ── Weighted Total Score ──
    total_score = (
        perf_score * 0.30 +
        risk_score * 0.25 +
        signal_score * 0.20 +
        stability_score * 0.15 +
        intel_score * 0.10 +
        strategy_bonus  # additive bonus, not weighted
    )

    # Recommendation
    if total_score >= 50:
        recommendation = 'strong_buy'
    elif total_score >= 25:
        recommendation = 'buy'
    elif total_score >= 0:
        recommendation = 'hold'
    elif total_score >= -25:
        recommendation = 'cautious'
    else:
        recommendation = 'avoid'

    result = {
        'code': code,
        'total_score': round(total_score, 2),
        'recommendation': recommendation,
        'dimension_scores': {
            'performance': round(perf_score, 2),
            'risk_adjusted': round(risk_score, 2),
            'signal_strength': round(signal_score, 2),
            'stability': round(stability_score, 2),
            'intel_alignment': round(intel_score, 2),
        },
        'dimension_weights': {
            'performance': 0.30,
            'risk_adjusted': 0.25,
            'signal_strength': 0.20,
            'stability': 0.15,
            'intel_alignment': 0.10,
        },
        'returns': returns,
        'risk_metrics': risk_metrics,
        'stability_metrics': stability_metrics,
        'signal_snapshot': signal_snapshot,
    }

    if strategy_bonus != 0:
        result['strategy_bonus'] = round(strategy_bonus, 2)
        result['strategy_alignment'] = strategy_alignment

    return result


def screen_assets(criteria=None, *, client=None, db=None):
    """Screen assets by multiple criteria and score them.

    Args:
        criteria: dict with keys:
            asset_type: 'all','stock','mixed','bond','index','qdii','etf'
            sort: 'day','week','month','3month','6month','year','ytd'
            limit: how many candidates to discover (pre-filter)
            top_n: how many to deeply score (default 20)
            min_size: minimum asset size in 亿
            min_3m_return: minimum 3-month return %
            max_drawdown: maximum acceptable drawdown %
            risk_level: 'low','medium','high'
        client: Optional TradingClient.
        db: Optional DB connection for intel sentiment.

    Returns:
        {candidates: [...], summary: {...}, criteria: {...}}
    """
    criteria = criteria or {}
    asset_type = criteria.get('asset_type', 'all')
    sort = criteria.get('sort', '3month')
    limit = int(criteria.get('limit', 100))
    top_n = int(criteria.get('top_n', 20))
    min_size = float(criteria.get('min_size', 0))

    # Step 1: Discover candidates from ranking
    ranking = fetch_asset_ranking(
        asset_type=asset_type, sort=sort, limit=limit,
        min_size=min_size, client=client,
    )

    if not ranking:
        return {'candidates': [], 'summary': {'error': 'No ranking data available'}}

    # Step 2: Pre-filter by basic criteria
    filtered = ranking
    if criteria.get('min_3m_return') is not None:
        min_3m = float(criteria['min_3m_return'])
        filtered = [f for f in filtered if f.get('3m_pct', 0) >= min_3m]
    if criteria.get('min_1y_return') is not None:
        min_1y = float(criteria['min_1y_return'])
        filtered = [f for f in filtered if f.get('1y_pct', 0) >= min_1y]

    # Limit to top_n for deep scoring
    candidates = filtered[:top_n]

    # Step 3: Get intel sentiment + active strategies
    intel_sentiment = None
    active_strategies = None
    if db:
        try:
            recent = db.execute(
                "SELECT sentiment FROM trading_intel_cache WHERE sentiment != '' "
                "ORDER BY fetched_at DESC LIMIT 30"
            ).fetchall()
            if recent:
                sentiments = [r['sentiment'] for r in recent]
                bull_count = sum(1 for s in sentiments if s == 'bullish')
                bear_count = sum(1 for s in sentiments if s == 'bearish')
                if bull_count > bear_count * 1.5:
                    intel_sentiment = 'bullish'
                elif bear_count > bull_count * 1.5:
                    intel_sentiment = 'bearish'
                else:
                    intel_sentiment = 'neutral'
        except Exception as e:
            logger.debug('[Screening] Intel sentiment lookup failed: %s', e, exc_info=True)

        # Fetch active strategies so score_asset_candidate can compute alignment
        try:
            rows = db.execute(
                "SELECT name, type, logic, scenario FROM trading_strategies "
                "WHERE status = 'active'"
            ).fetchall()
            if rows:
                active_strategies = [dict(r) for r in rows]
                logger.debug('[Screening] Using %d active strategies for alignment scoring',
                             len(active_strategies))
        except Exception as e:
            logger.debug('[Screening] Strategies lookup failed: %s', e, exc_info=True)

    # Step 4: Fetch price histories and score in parallel
    from lib.trading.nav import fetch_price_history
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')

    scored = []
    errors = []

    def _score_one(cand):
        code = cand['code']
        try:
            navs = fetch_price_history(code, start_date, end_date, client=client)
            if navs and len(navs) >= 60:
                result = score_asset_candidate(
                    code, navs,
                    intel_sentiment=intel_sentiment,
                    strategies=active_strategies,
                )
                result['name'] = cand.get('name', '')
                result['asset_type'] = cand.get('asset_type', '')
                result['ranking_data'] = cand
                return result
            else:
                return {'code': code, 'error': f'Insufficient data: {len(navs) if navs else 0} points'}
        except Exception as e:
            logger.warning('Fund scoring failed for %s: %s', code, e, exc_info=True)
            return {'code': code, 'error': str(e)}

    with ThreadPoolExecutor(max_workers=min(len(candidates), 6)) as pool:
        futs = {pool.submit(_score_one, c): c for c in candidates}
        for f in as_completed(futs, timeout=60):
            try:
                result = f.result()
                if 'error' not in result or 'total_score' in result:
                    scored.append(result)
                else:
                    errors.append(result)
            except Exception as e:
                logger.warning('Fund screening future failed: %s', e, exc_info=True)
                errors.append({'code': futs[f].get('code', '?'), 'error': str(e)})

    # Sort by total score
    scored.sort(key=lambda x: x.get('total_score', -999), reverse=True)

    # Summary
    summary = {
        'total_discovered': len(ranking),
        'pre_filtered': len(filtered),
        'deeply_scored': len(scored),
        'errors': len(errors),
        'intel_sentiment': intel_sentiment,
        'top_3': [{'code': s['code'], 'name': s.get('name', ''),
                    'score': s['total_score'], 'rec': s['recommendation']}
                  for s in scored[:3]],
        'timestamp': datetime.now().isoformat(),
    }

    return {
        'candidates': scored,
        'summary': summary,
        'criteria': criteria,
        'errors': errors[:5],
    }


# ═══════════════════════════════════════════════════════════
#  Smart Fund Selection (Strategy-Driven)
# ═══════════════════════════════════════════════════════════

def smart_select_assets(strategies=None, holdings=None, cash=0,
                       risk_level='medium', *, client=None, db=None):
    """AI-free smart asset selection driven by strategies, signals & risk.

    Leverages:
      1. Active strategies → determine what asset types to look for
      2. Market regime → adjust risk appetite
      3. Current holdings → avoid overlap, check correlation
      4. Signal scoring → pick highest-scoring candidates
      5. Portfolio optimization → weight recommendations

    Args:
        strategies: list of strategy dicts (from trading_strategies table)
        holdings: list of holding dicts
        cash: available cash
        risk_level: 'low','medium','high'
        client: Optional TradingClient.
        db: Optional DB connection.

    Returns:
        {selections: [...], allocation: {...}, rationale: str}
    """
    holdings = holdings or []
    strategies = strategies or []

    held_codes = {h.get('symbol', '') for h in holdings if h.get('symbol')}

    # Determine screening parameters from strategies
    asset_types_to_screen = set()
    for s in strategies:
        logic = (s.get('logic', '') + ' ' + s.get('scenario', '')).lower()
        assets = s.get('assets', '').lower()
        if any(kw in logic or kw in assets for kw in ['宽基', '指数', '沪深300', '中证500', 'etf']):
            asset_types_to_screen.add('index')
        if any(kw in logic or kw in assets for kw in ['股票', '权益', '主动管理']):
            asset_types_to_screen.add('stock')
        if any(kw in logic or kw in assets for kw in ['混合', '平衡']):
            asset_types_to_screen.add('mixed')
        if any(kw in logic or kw in assets for kw in ['债券', '固收', '债基']):
            asset_types_to_screen.add('bond')
        if any(kw in logic or kw in assets for kw in ['qdii', '海外', '全球', '黄金']):
            asset_types_to_screen.add('qdii')

    if not asset_types_to_screen:
        # Default screening based on risk level
        if risk_level == 'low':
            asset_types_to_screen = {'bond', 'mixed'}
        elif risk_level == 'high':
            asset_types_to_screen = {'stock', 'index'}
        else:
            asset_types_to_screen = {'mixed', 'index', 'stock'}

    # Screen each asset type
    all_candidates = []
    for ft in asset_types_to_screen:
        result = screen_assets(
            criteria={
                'asset_type': ft,
                'sort': '3month',
                'top_n': 10,
                'min_size': 1.0,  # at least 1亿
            },
            client=client, db=db,
        )
        for cand in result.get('candidates', []):
            if cand.get('code') not in held_codes:  # exclude already held
                cand['screened_type'] = ft
                all_candidates.append(cand)

    # Sort all candidates by total score
    all_candidates.sort(key=lambda x: x.get('total_score', -999), reverse=True)

    # Select top candidates with diversification
    selections = []
    selected_types = defaultdict(int)
    max_per_type = 3 if risk_level == 'high' else 2

    for cand in all_candidates:
        ft = cand.get('screened_type', 'unknown')
        if selected_types[ft] >= max_per_type:
            continue
        if cand.get('recommendation', 'avoid') in ('avoid',):
            continue
        selections.append(cand)
        selected_types[ft] += 1
        if len(selections) >= 8:  # max 8 new selections
            break

    # Portfolio optimization on selections + existing
    allocation = {}
    if selections:
        try:
            from lib.trading.nav import fetch_price_history
            from lib.trading_strategy_engine.portfolio import optimize_portfolio_allocation

            # Build nav data for optimization
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=300)).strftime('%Y-%m-%d')
            opt_navs = {}
            for sel in selections[:6]:
                code = sel['code']
                navs = fetch_price_history(code, start_date, end_date, client=client)
                if navs and len(navs) >= 60:
                    opt_navs[code] = navs

            if len(opt_navs) >= 2:
                opt_result = optimize_portfolio_allocation(
                    opt_navs,
                    max_single_weight=0.35 if risk_level == 'high' else 0.25,
                    min_weight=0.08,
                    method='risk_signal',
                )
                if 'weights' in opt_result:
                    allocation = opt_result['weights']
                    # Apply weights to cash
                    for sel in selections:
                        w = allocation.get(sel['code'], 0)
                        sel['suggested_weight'] = round(w, 4)
                        sel['suggested_amount'] = round(cash * w, 2)
        except Exception as e:
            logger.warning('[Screening] Portfolio optimization failed: %s', e, exc_info=True)

    # Build rationale
    rationale_parts = [
        f"基于 {len(strategies)} 条活跃策略和 {risk_level} 风险偏好",
        f"从 {len(asset_types_to_screen)} 个资产类别中筛选了 {len(all_candidates)} 只候选标的",
        f"综合信号评分、风险指标和业绩表现，推荐 {len(selections)} 只标的",
    ]
    if held_codes:
        rationale_parts.append(f"已排除当前持仓的 {len(held_codes)} 只标的")

    return {
        'selections': selections,
        'allocation': allocation,
        'screened_types': list(asset_types_to_screen),
        'total_candidates': len(all_candidates),
        'rationale': '。'.join(rationale_parts) + '。',
        'risk_level': risk_level,
        'timestamp': datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════
#  Full Screening Pipeline (Discover → Screen → Backtest)
# ═══════════════════════════════════════════════════════════

def run_screening_pipeline(criteria=None, backtest_top_n=5,
                            backtest_days=365, *, client=None, db=None):
    """Complete pipeline: discover → screen → score → backtest top candidates.

    This is the unified entry point that:
    1. Screens assets using multi-dimensional criteria
    2. Scores and ranks candidates
    3. Runs quantitative backtest on top candidates
    4. Returns comprehensive selection report

    Args:
        criteria: screening criteria dict
        backtest_top_n: how many top candidates to backtest
        backtest_days: how many days of backtest history
        client: Optional TradingClient.
        db: Optional DB connection.

    Returns:
        {screening: {...}, backtest_results: [...], final_ranking: [...]}
    """
    # Step 1: Screen
    screening = screen_assets(criteria=criteria, client=client, db=db)
    candidates = screening.get('candidates', [])

    if not candidates:
        return {
            'screening': screening,
            'backtest_results': [],
            'final_ranking': [],
            'error': 'No candidates found matching criteria',
        }

    # Step 2: Backtest top candidates
    top_codes = [c['code'] for c in candidates[:backtest_top_n]]
    backtest_results = []

    from lib.trading.nav import fetch_price_history
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=backtest_days + 30)).strftime('%Y-%m-%d')

    for code in top_codes:
        try:
            navs = fetch_price_history(code, start_date, end_date, client=client)
            if not navs or len(navs) < 60:
                backtest_results.append({
                    'code': code, 'error': f'Insufficient data: {len(navs) if navs else 0}'
                })
                continue

            from lib.trading_backtest_engine import BacktestEngine
            asset_prices = {code: navs}

            # Run adaptive strategy
            engine = BacktestEngine({
                'strategy': 'adaptive',
                'initial_capital': 100000,
            })
            bt_result = engine.run(asset_prices)

            # Also run buy-and-hold for comparison
            bh_engine = BacktestEngine({
                'strategy': 'buy_and_hold',
                'initial_capital': 100000,
            })
            bh_result = bh_engine.run(asset_prices)

            bt_summary = bt_result.get('summary', {})
            bh_summary = bh_result.get('summary', {})

            adaptive_return = bt_summary.get('total_return_pct', 0)
            bh_return = bh_summary.get('total_return_pct', 0)
            excess = round(adaptive_return - bh_return, 2)

            backtest_results.append({
                'code': code,
                'name': next((c['name'] for c in candidates if c['code'] == code), ''),
                'adaptive': {
                    'total_return_pct': adaptive_return,
                    'annualized_return_pct': bt_summary.get('annualized_return_pct', 0),
                    'max_drawdown_pct': bt_summary.get('max_drawdown_pct', 0),
                    'sharpe_ratio': bt_summary.get('sharpe_ratio', 0),
                    'total_trades': bt_summary.get('total_trades', 0),
                    'win_rate_pct': bt_summary.get('win_rate_pct', 0),
                    'benchmark_return_pct': bh_return,
                    'excess_return_pct': excess,
                },
                'buy_and_hold': {
                    'total_return_pct': bh_return,
                    'max_drawdown_pct': bh_summary.get('max_drawdown_pct', 0),
                },
                'excess_return': excess,
            })
        except Exception as e:
            logger.warning('[Pipeline] Backtest failed for %s: %s', code, e, exc_info=True)
            backtest_results.append({'code': code, 'error': str(e)})

    # Step 3: Build final ranking combining screening score + backtest
    final_ranking = []
    for cand in candidates[:backtest_top_n]:
        code = cand['code']
        bt = next((b for b in backtest_results if b.get('code') == code and 'error' not in b), None)

        screening_score = cand.get('total_score', 0)
        combined_score = screening_score
        backtest_bonus = 0
        if bt:
            # Boost score by backtest performance
            adaptive_return = bt['adaptive'].get('total_return_pct', 0)
            sharpe = bt['adaptive'].get('sharpe_ratio', 0)
            excess = bt.get('excess_return', 0)

            if adaptive_return > 20: backtest_bonus += 15
            elif adaptive_return > 10: backtest_bonus += 10
            elif adaptive_return > 0: backtest_bonus += 5

            if sharpe > 1.0: backtest_bonus += 10
            elif sharpe > 0.5: backtest_bonus += 5

            if excess > 5: backtest_bonus += 10
            elif excess > 0: backtest_bonus += 5

            combined_score += backtest_bonus

        final_ranking.append({
            'code': code,
            'name': cand.get('name', ''),
            'screening_score': round(screening_score, 2),
            'backtest_score': round(backtest_bonus, 2),
            'combined_score': round(combined_score, 2),
            'recommendation': cand.get('recommendation', ''),
            'backtest': bt,
            'dimension_scores': cand.get('dimension_scores', {}),
        })

    final_ranking.sort(key=lambda x: x['combined_score'], reverse=True)

    return {
        'screening': screening,
        'backtest_results': backtest_results,
        'final_ranking': final_ranking,
        'pipeline_summary': {
            'total_discovered': screening.get('summary', {}).get('total_discovered', 0),
            'deeply_scored': len(candidates),
            'backtested': sum(1 for b in backtest_results if 'error' not in b),
            'top_pick': final_ranking[0] if final_ranking else None,
        },
    }
