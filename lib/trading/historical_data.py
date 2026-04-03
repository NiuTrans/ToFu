"""lib/trading/historical_data.py — Historical Data Fetcher for LLM Simulation.

Fetches and caches all data needed to reconstruct any past time period:
  1. ETF K-line close prices (eastmoney push2his API — real market prices,
     NOT fund NAV which differs due to premium/discount)
  2. Major index history (eastmoney K-line API)
  3. Macro economic indicators (akshare — CPI/PPI/PMI/GDP/interest rates)
  4. Historical news & intel (Google News RSS with date range, web search backfill)

All data is stored locally so the LLM simulator can replay any period
without network access.

Design principle:
  Every fetched item has a date.  The simulator's time-lock layer
  ensures the LLM only sees data published <= the simulated date.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

from lib.log import get_logger, log_context

logger = get_logger(__name__)


__all__ = [
    'fetch_and_store_price_history',
    'fetch_and_store_index_history',
    'fetch_and_store_macro_data',
    'backfill_historical_intel',
    'run_full_historical_fetch',
    'get_data_coverage_report',
]

# ── Human-readable asset names for progress messages ──
# Pre-populated with common ETFs; dynamically expanded at runtime via
# register_asset_name() when user adds custom stocks/funds.
_FUND_NAMES = {
    '510300': '华泰柏瑞沪深300ETF',
    '510500': '南方中证500ETF',
    '159915': '易方达创业板ETF',
    '510050': '华夏上证50ETF',
    '512100': '南方中证1000ETF',
    '512880': '国泰中证全指证券公司ETF',
    '512010': '易方达沪深300医药卫生ETF',
    '515790': '华泰柏瑞中证光伏产业ETF',
    '159825': '天弘中证农业主题ETF',
    '512690': '鹏华中证酒ETF',
    '511010': '国泰上证5年期国债ETF',
    '511260': '国泰上证10年期国债ETF',
    '511220': '海富通上证城投债ETF',
    '159972': '华夏中证海外中国互联网50ETF',
    '513500': '博时标普500ETF',
}


def register_asset_name(code: str, name: str):
    """Register a human-readable name for an asset code.

    Called when users add custom stocks/funds to the simulator
    so that progress messages display friendly names.
    """
    if code and name:
        _FUND_NAMES[code] = name


def _fund_label(code: str) -> str:
    """Human-readable asset label for progress messages."""
    name = _FUND_NAMES.get(code)
    return f'{name}({code})' if name else code


def _etf_secid(code: str) -> str:
    """Convert stock/ETF/fund code to eastmoney secid format for K-line API.

    Works for ALL A-share securities:
      Shenzhen (深交所): codes starting with 0, 1, 3 → prefix '0.'
      Shanghai (上交所): codes starting with 5, 6 → prefix '1.'
    """
    if code.startswith('15') or code.startswith('0') or code.startswith('3'):
        return f'0.{code}'  # Shenzhen
    return f'1.{code}'  # Shanghai


# ═══════════════════════════════════════════════════════════
#  1. Asset Price History (ETF K-line Close Price)
# ═══════════════════════════════════════════════════════════

def fetch_and_store_price_history(
    db: Any,
    symbols: list[str],
    start_date: str,
    end_date: str,
    progress_cb: Callable | None = None,
) -> dict[str, Any]:
    """Fetch and store historical price data for stocks, ETFs, and funds.

    For exchange-traded securities (stocks, ETFs), uses eastmoney push2his
    K-line API — the SAME price data you see on any stock/ETF chart.
    For open-end funds (non-exchange-traded), falls back to the fund NAV API.

    Args:
        db: Database connection.
        symbols: List of asset codes (e.g. ['510300', '600519', '110011']).
        start_date: 'YYYY-MM-DD'.
        end_date: 'YYYY-MM-DD'.
        progress_cb: Optional fn(done, total, message).

    Returns:
        {symbol: {count, first_date, last_date, status}} for each symbol.
    """
    _ensure_sim_tables(db)

    from lib.trading._common import _get_default_client
    client = _get_default_client()

    results = {}

    if progress_cb:
        progress_cb(
            0, len(symbols),
            f'📈 需要抓取 {len(symbols)} 只标的的K线收盘价 ({start_date} → {end_date})'
        )

    for i, symbol in enumerate(symbols):
        label = _fund_label(symbol)
        try:
            # Check existing coverage — also verify data has K-line close
            # (old data from NAV API will have close=0)
            existing = db.execute(
                'SELECT MIN(date) as mn, MAX(date) as mx, COUNT(*) as cnt '
                'FROM trading_sim_prices WHERE symbol=? AND date>=? AND date<=?',
                (symbol, start_date, end_date)
            ).fetchone()

            cached_cnt = existing['cnt'] if existing and existing['cnt'] else 0

            # Check if cached data is K-line format (close > 0)
            # Old NAV-only data won't have close populated
            _has_kline = False
            if cached_cnt > 0:
                try:
                    _sample = db.execute(
                        'SELECT close FROM trading_sim_prices '
                        'WHERE symbol=? AND date>=? AND date<=? LIMIT 1',
                        (symbol, start_date, end_date)
                    ).fetchone()
                    _has_kline = _sample and _sample['close'] and _sample['close'] > 0
                except Exception as _e:
                    logger.debug('[HistData] K-line check failed for %s: %s', symbol, _e)
                    _has_kline = False

            # Skip ONLY if:
            #   a) cache fully spans the requested date range, AND
            #   b) data is K-line format (not old NAV-only data)
            if (_has_kline
                    and cached_cnt > 0
                    and existing['mn'] and existing['mx']
                    and _days_between(start_date, existing['mn']) <= 5
                    and _days_between(existing['mx'], end_date) <= 5):
                results[symbol] = {
                    'count': cached_cnt,
                    'first_date': existing['mn'],
                    'last_date': existing['mx'],
                    'status': 'cached',
                }
                if progress_cb:
                    progress_cb(
                        i + 1, len(symbols),
                        f'✅ {label}: 本地已有 {cached_cnt} 天K线数据 '
                        f'({existing["mn"]}~{existing["mx"]})，完整'
                    )
                continue

            # Need to fetch K-line data
            gap_msg = ''
            if cached_cnt > 0 and not _has_kline:
                gap_msg = '，旧数据为基金净值格式，需重新获取K线收盘价'
            elif cached_cnt > 0:
                gap_msg = f'，本地已有{cached_cnt}天，边界数据不完整'
            if progress_cb:
                progress_cb(
                    i, len(symbols),
                    f'⏳ {label}: 正在从东方财富抓取K线数据{gap_msg}...'
                )

            # Fetch K-line data from eastmoney push2his API
            secid = _etf_secid(symbol)
            sd = start_date.replace('-', '')
            ed = end_date.replace('-', '')

            with log_context(f'fetch_kline_{symbol}', logger=logger):
                url = (
                    f'http://push2his.eastmoney.com/api/qt/stock/kline/get?'
                    f'secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b'
                    f'&fields1=f1,f2,f3,f4,f5,f6'
                    f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
                    f'&klt=101&fqt=1&beg={sd}&end={ed}&lmt=5000'
                    f'&cb=jQuery'
                )
                # Retry on transient network errors (proxy timeouts etc.)
                _kline_data = None
                for _attempt in range(3):
                    try:
                        r = client.session.get(url, timeout=10, headers={
                            **client.headers,
                            'Referer': 'https://quote.eastmoney.com/',
                        })
                        m = re.search(r'jQuery\((.*)\)', r.text, re.S)
                        _kline_data = json.loads(m.group(1)) if m else r.json()
                        break
                    except (requests.Timeout, requests.ConnectionError) as _net_err:
                        if _attempt < 2:
                            logger.warning(
                                '[HistData] K-line fetch timeout for %s (attempt %d/3): %s',
                                symbol, _attempt + 1, _net_err)
                            if progress_cb:
                                progress_cb(
                                    i, len(symbols),
                                    f'⏳ {label}: 网络超时，第{_attempt + 2}次重试中...'
                                )
                            time.sleep(2 * (_attempt + 1))  # 2s, 4s backoff
                        else:
                            raise  # final attempt — let outer handler catch
                data = _kline_data

            klines = (data.get('data') or {}).get('klines', [])
            if not klines:
                # Fallback: try open-end fund NAV API for non-exchange-traded funds
                nav_count = _fetch_fund_nav_fallback(
                    db, symbol, start_date, end_date, client, progress_cb, i, len(symbols), label
                )
                if nav_count > 0:
                    results[symbol] = {'count': nav_count, 'status': 'nav_fetched'}
                    continue
                logger.warning('[HistData] No K-line or NAV data for %s/%s (%s~%s)',
                               symbol, secid, start_date, end_date)
                results[symbol] = {'count': 0, 'status': 'empty'}
                if progress_cb:
                    progress_cb(i + 1, len(symbols),
                                f'⚠️ {label}: 无法获取数据，该标的可能不在此时段交易')
                continue

            stored = 0
            first_d = ''
            last_d = ''
            for line in klines:
                parts = line.split(',')
                if len(parts) < 7:
                    continue
                # f51=date, f52=open, f53=close, f54=high, f55=low,
                # f56=volume, f57=amount, f58=amplitude, f59=change_pct
                dt = parts[0]
                try:
                    open_ = float(parts[1])
                    close = float(parts[2])
                    high = float(parts[3])
                    low = float(parts[4])
                    volume = float(parts[5])
                    amount = float(parts[6])
                    change_pct = float(parts[8]) if len(parts) > 8 else 0.0
                except (ValueError, IndexError) as e:
                    logger.debug('[HistData] K-line parse error %s/%s: %s', symbol, dt, e)
                    continue

                if not first_d:
                    first_d = dt
                last_d = dt

                try:
                    # nav = close for backward compatibility with simulator
                    # that reads price_data['nav']
                    db.execute(
                        '''INSERT INTO trading_sim_prices
                           (symbol, date, nav, acc_nav, change_pct,
                            open, high, low, close, volume, amount)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (symbol, date) DO UPDATE SET
                             nav=EXCLUDED.nav, change_pct=EXCLUDED.change_pct,
                             open=EXCLUDED.open, high=EXCLUDED.high,
                             low=EXCLUDED.low, close=EXCLUDED.close,
                             volume=EXCLUDED.volume, amount=EXCLUDED.amount''',
                        (symbol, dt, close, 0, change_pct,
                         open_, high, low, close, volume, amount)
                    )
                    stored += 1
                except Exception as e:
                    logger.debug('[HistData] Insert error for %s/%s: %s', symbol, dt, e)
            db.commit()

            results[symbol] = {
                'count': stored,
                'first_date': first_d,
                'last_date': last_d,
                'status': 'fetched',
            }
            logger.info('[HistData] Stored %d K-line records for %s (%s~%s)',
                        stored, symbol, start_date, end_date)

            if progress_cb:
                progress_cb(
                    i + 1, len(symbols),
                    f'✅ {label}: 成功获取 {stored} 天K线 '
                    f'({first_d}~{last_d})'
                )

        except Exception as e:
            logger.error('[HistData] K-line fetch failed for %s: %s', symbol, e, exc_info=True)
            results[symbol] = {'count': 0, 'status': 'error', 'error': str(e)}
            if progress_cb:
                progress_cb(i + 1, len(symbols), f'❌ {label}: 抓取失败 - {e}')

        time.sleep(0.3)  # Rate limit

    return results


def _fetch_fund_nav_fallback(
    db: Any,
    symbol: str,
    start_date: str,
    end_date: str,
    client: Any,
    progress_cb: Callable | None,
    idx: int,
    total: int,
    label: str,
) -> int:
    """Fetch open-end fund NAV data as fallback when K-line API returns nothing.

    Open-end funds (e.g. 110011, 001234) are not exchange-traded so they have
    no K-line data. Instead we fetch daily NAV from EastMoney's fund API.

    Returns:
        Number of NAV records stored, or 0 if API fails.
    """
    try:
        if progress_cb:
            progress_cb(idx, total, f'⏳ {label}: K线无数据，尝试基金净值API...')

        # EastMoney open-end fund NAV API (paginated)
        sd = start_date
        ed = end_date
        page = 1
        per_page = 40
        stored = 0
        first_d = ''
        last_d = ''

        while True:
            url = (
                f'http://api.fund.eastmoney.com/f10/lsjz?callback=jQuery'
                f'&fundCode={symbol}&pageIndex={page}&pageSize={per_page}'
                f'&startDate={sd}&endDate={ed}&_=0'
            )
            r = client.session.get(url, timeout=8, headers={
                **client.headers,
                'Referer': 'http://fundf10.eastmoney.com/',
            })
            m = re.search(r'jQuery\((.*)\)', r.text, re.S)
            if not m:
                break
            data = json.loads(m.group(1))
            nav_data = data.get('Data', {})
            records = nav_data.get('LSJZList') or []
            total_count = nav_data.get('TotalCount', 0)

            if not records:
                break

            for rec in records:
                dt = rec.get('FSRQ', '')  # 净值日期
                nav_str = rec.get('DWJZ', '')  # 单位净值
                acc_nav_str = rec.get('LJJZ', '')  # 累计净值
                change_str = rec.get('JZZZL', '')  # 日增长率

                if not dt or not nav_str:
                    continue
                try:
                    nav = float(nav_str)
                    acc_nav = float(acc_nav_str) if acc_nav_str else 0.0
                    change_pct = float(change_str) if change_str else 0.0
                except (ValueError, TypeError) as e:
                    logger.debug('Skipping unparseable NAV row (nav=%s, acc=%s, chg=%s): %s', nav_str, acc_nav_str, change_str, e)
                    continue

                if not first_d or dt < first_d:
                    first_d = dt
                if not last_d or dt > last_d:
                    last_d = dt

                try:
                    # Store NAV as both nav and close for compatibility
                    db.execute(
                        '''INSERT INTO trading_sim_prices
                           (symbol, date, nav, acc_nav, change_pct,
                            open, high, low, close, volume, amount)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (symbol, date) DO UPDATE SET
                             nav=EXCLUDED.nav, acc_nav=EXCLUDED.acc_nav,
                             change_pct=EXCLUDED.change_pct,
                             close=EXCLUDED.close''',
                        (symbol, dt, nav, acc_nav, change_pct,
                         nav, nav, nav, nav, 0, 0)
                    )
                    stored += 1
                except Exception as e:
                    logger.debug('[HistData] NAV insert error for %s/%s: %s', symbol, dt, e)

            db.commit()

            # Check if there are more pages
            if page * per_page >= total_count:
                break
            page += 1
            time.sleep(0.2)

        if stored > 0:
            logger.info('[HistData] Stored %d NAV records for fund %s (%s~%s)',
                        stored, symbol, first_d, last_d)
            if progress_cb:
                progress_cb(idx + 1, total,
                            f'✅ {label}: 成功获取 {stored} 天基金净值 ({first_d}~{last_d})')
        return stored

    except Exception as e:
        logger.warning('[HistData] Fund NAV fetch failed for %s: %s', symbol, e, exc_info=True)
        return 0


# ═══════════════════════════════════════════════════════════
#  2. Major Index History
# ═══════════════════════════════════════════════════════════

# Index secid mapping for K-line history
_INDEX_KLINE_CODES = [
    ('1.000001', '上证指数'),
    ('0.399001', '深证成指'),
    ('0.399006', '创业板指'),
    ('1.000300', '沪深300'),
    ('1.000905', '中证500'),
    ('1.000852', '中证1000'),
]


def fetch_and_store_index_history(
    db: Any,
    start_date: str,
    end_date: str,
    progress_cb: Callable | None = None,
) -> dict[str, Any]:
    """Fetch historical K-line data for major indices.

    Uses eastmoney push2his API for daily K-line data.
    """
    _ensure_sim_tables(db)

    from lib.trading._common import _get_default_client
    client = _get_default_client()

    if progress_cb:
        progress_cb(
            0, len(_INDEX_KLINE_CODES),
            f'📊 需要抓取 {len(_INDEX_KLINE_CODES)} 个大盘指数 ({start_date} → {end_date})'
        )

    results = {}
    for i, (secid, name) in enumerate(_INDEX_KLINE_CODES):
        try:
            # Check cache — require full date-range coverage
            existing = db.execute(
                'SELECT COUNT(*) as cnt, MIN(date) as mn, MAX(date) as mx '
                'FROM trading_sim_indices '
                'WHERE secid=? AND date>=? AND date<=?',
                (secid, start_date, end_date)
            ).fetchone()
            cached_cnt = existing['cnt'] if existing else 0
            if (cached_cnt > 0
                    and existing['mn'] and existing['mx']
                    and _days_between(start_date, existing['mn']) <= 5
                    and _days_between(existing['mx'], end_date) <= 5):
                results[secid] = {'name': name, 'count': cached_cnt, 'status': 'cached'}
                if progress_cb:
                    progress_cb(
                        i + 1, len(_INDEX_KLINE_CODES),
                        f'✅ {name}: 本地已有 {cached_cnt} 天，完整'
                    )
                continue

            # Tell user we're fetching this one
            if progress_cb:
                progress_cb(i, len(_INDEX_KLINE_CODES), f'⏳ {name}: 正在从东方财富获取K线...')

            # Eastmoney K-line API
            sd = start_date.replace('-', '')
            ed = end_date.replace('-', '')
            url = (
                f'http://push2his.eastmoney.com/api/qt/stock/kline/get?'
                f'secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b'
                f'&fields1=f1,f2,f3,f4,f5,f6'
                f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
                f'&klt=101&fqt=1&beg={sd}&end={ed}&lmt=5000'
                f'&cb=jQuery'
            )
            # Retry on transient network errors (proxy timeouts etc.)
            _idx_data = None
            for _attempt in range(3):
                try:
                    r = client.session.get(url, timeout=10, headers={
                        **client.headers,
                        'Referer': 'https://quote.eastmoney.com/',
                    })
                    m = re.search(r'jQuery\((.*)\)', r.text, re.S)
                    _idx_data = json.loads(m.group(1)) if m else r.json()
                    break
                except (requests.Timeout, requests.ConnectionError) as _net_err:
                    if _attempt < 2:
                        logger.warning(
                            '[HistData] Index K-line fetch timeout for %s (attempt %d/3): %s',
                            name, _attempt + 1, _net_err)
                        if progress_cb:
                            progress_cb(
                                i, len(_INDEX_KLINE_CODES),
                                f'⏳ {name}: 网络超时，第{_attempt + 2}次重试中...'
                            )
                        time.sleep(2 * (_attempt + 1))  # 2s, 4s backoff
                    else:
                        raise  # final attempt — let outer handler catch
            data = _idx_data

            klines = (data.get('data') or {}).get('klines', [])
            stored = 0
            for line in klines:
                parts = line.split(',')
                if len(parts) < 7:
                    continue
                # f51=date, f52=open, f53=close, f54=high, f55=low, f56=volume, f57=amount
                dt, open_, close, high, low, volume, amount = parts[:7]
                pct = parts[8] if len(parts) > 8 else '0'
                try:
                    db.execute(
                        '''INSERT INTO trading_sim_indices
                           (secid, name, date, open, close, high, low, volume, amount, change_pct)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT (secid, date) DO UPDATE SET
                             close=EXCLUDED.close, high=EXCLUDED.high, low=EXCLUDED.low,
                             volume=EXCLUDED.volume, amount=EXCLUDED.amount,
                             change_pct=EXCLUDED.change_pct''',
                        (secid, name, dt, float(open_), float(close),
                         float(high), float(low), float(volume), float(amount),
                         float(pct))
                    )
                    stored += 1
                except Exception as e:
                    logger.debug('[HistData] Index insert error %s/%s: %s', secid, dt, e)
            db.commit()

            results[secid] = {'name': name, 'count': stored, 'status': 'fetched'}
            logger.info('[HistData] Stored %d index records for %s (%s)', stored, name, secid)

            if progress_cb:
                progress_cb(
                    i + 1, len(_INDEX_KLINE_CODES),
                    f'✅ {name}: 成功获取 {stored} 天K线'
                )

        except Exception as e:
            logger.error('[HistData] Index fetch failed for %s: %s', secid, e, exc_info=True)
            results[secid] = {'name': name, 'count': 0, 'status': 'error', 'error': str(e)}
            if progress_cb:
                progress_cb(i + 1, len(_INDEX_KLINE_CODES), f'❌ {name}: 抓取失败 - {e}')

        time.sleep(0.3)

    return results


# ═══════════════════════════════════════════════════════════
#  3. Macro Economic Indicators
# ═══════════════════════════════════════════════════════════

def fetch_and_store_macro_data(
    db: Any,
    start_date: str,
    end_date: str,
    progress_cb: Callable | None = None,
) -> dict[str, Any]:
    """Fetch macro economic indicators and store them locally.

    Data sources (tried in order):
      1. akshare (free, no API key)
      2. eastmoney API fallback

    Indicators: CPI, PPI, PMI, M2, LPR, Shibor, northbound flow history.
    """
    _ensure_sim_tables(db)

    results = {}
    indicators = [
        ('cpi', '居民消费价格指数CPI'),
        ('ppi', '工业生产者出厂价格PPI'),
        ('pmi', '制造业采购经理指数PMI'),
        ('m2', '货币供应量M2'),
    ]

    # Macro indicators are monthly — dates are normalized to the 1st of
    # each month (e.g. "2026年02月份" → "2026-02-01").  Snap start_date
    # to the 1st so that e.g. start_date="2026-02-02" doesn't exclude
    # the entire February data point.
    macro_start = start_date[:8] + '01' if len(start_date) >= 10 else start_date

    if progress_cb:
        progress_cb(0, len(indicators), f'🏛️ 需要抓取 {len(indicators)} 项宏观经济指标')

    for i, (key, label) in enumerate(indicators):
        try:
            # Check cache — macro indicators are published monthly,
            # so require ~1 record per month in the range to consider
            # the cache complete.  Allow 1 missing month (the latest
            # month may not be published yet at simulation time).
            existing = db.execute(
                'SELECT COUNT(*) as cnt, MIN(date) as mn, MAX(date) as mx '
                'FROM trading_sim_macro '
                'WHERE indicator=? AND date>=? AND date<=?',
                (key, macro_start, end_date)
            ).fetchone()
            cached_cnt = existing['cnt'] if existing else 0
            try:
                _sd = datetime.strptime(start_date, '%Y-%m-%d')
                _ed = datetime.strptime(end_date, '%Y-%m-%d')
                expected_months = (_ed.year - _sd.year) * 12 + _ed.month - _sd.month + 1
            except Exception as _e:
                logger.debug('[HistData] Date parse failed for expected_months: %s', _e)
                expected_months = 6
            if cached_cnt >= max(expected_months - 1, 1):
                results[key] = {'label': label, 'count': cached_cnt, 'status': 'cached'}
                if progress_cb:
                    progress_cb(i + 1, len(indicators),
                                f'✅ {label}: 本地已有 {cached_cnt}/{expected_months} 条，完整')
                continue

            # Fetch via akshare — prefer eastmoney-backed functions
            # (jin10-backed *_yearly variants are blocked by corporate
            #  proxy / SSL inspection, causing persistent failures)
            try:
                import akshare as ak  # lazy — optional heavy dependency
            except ImportError:
                logger.warning('[HistData] akshare not installed — macro data fetch skipped. '
                               'Install with: pip install akshare')
                break
            df = None
            # Per-indicator preferred value column (first match wins)
            _value_col_hint = None
            if key == 'cpi':
                df = ak.macro_china_cpi()           # eastmoney
                _value_col_hint = '全国-同比增长'
            elif key == 'ppi':
                df = ak.macro_china_ppi()            # eastmoney
                _value_col_hint = '当月同比增长'
            elif key == 'pmi':
                df = ak.macro_china_pmi()            # eastmoney
                _value_col_hint = '制造业-指数'
            elif key == 'm2':
                df = ak.macro_china_supply_of_money() # sina
                _value_col_hint = '货币和准货币（广义货币M2）同比增长'

            if df is not None and not df.empty:
                stored = 0
                for _, row in df.iterrows():
                    # Different akshare functions have different column names
                    dt_val = None
                    value = None

                    # --- Use hinted value column if available ---
                    if _value_col_hint and _value_col_hint in df.columns:
                        try:
                            value = float(row[_value_col_hint])
                        except (ValueError, TypeError) as _ve:
                            logger.debug('[HistData] Non-numeric macro hint col %s: %s',
                                         _value_col_hint, _ve)

                    for col in df.columns:
                        col_lower = col.lower()
                        # Date detection — also match '统计时间' (M2)
                        if '日期' in col or '月份' in col or '统计时间' in col or 'date' in col_lower:
                            dt_val = str(row[col])[:10]
                        # Value detection — only used when hint didn't match
                        elif value is None and (
                            '同比' in col or '当月' in col
                            or 'value' in col_lower or '数值' in col
                        ):
                            try:
                                value = float(row[col])
                            except (ValueError, TypeError) as _ve:
                                logger.debug('[HistData] Non-numeric macro value in col %s: %s', col, _ve)

                    if dt_val and value is not None:
                        # Normalize date format
                        dt_val = _normalize_date(dt_val)
                        if not dt_val or dt_val < macro_start or dt_val > end_date:
                            continue
                        try:
                            db.execute(
                                '''INSERT INTO trading_sim_macro
                                   (indicator, label, date, value)
                                   VALUES (?, ?, ?, ?)
                                   ON CONFLICT (indicator, date) DO UPDATE SET
                                     value=EXCLUDED.value''',
                                (key, label, dt_val, value)
                            )
                            stored += 1
                        except Exception as e:
                            logger.debug('[HistData] Macro insert error %s/%s: %s', key, dt_val, e)

                db.commit()
                results[key] = {'label': label, 'count': stored, 'status': 'fetched'}
                logger.info('[HistData] Stored %d %s records', stored, label)
                if progress_cb:
                    progress_cb(i + 1, len(indicators), f'✅ {label}: 成功获取 {stored} 条数据')
            else:
                results[key] = {'label': label, 'count': 0, 'status': 'empty'}
                if progress_cb:
                    progress_cb(i + 1, len(indicators), f'⚠️ {label}: API 未返回数据')

        except Exception as e:
            logger.warning('[HistData] Macro fetch failed for %s: %s', key, e, exc_info=True)
            results[key] = {'label': label, 'count': 0, 'status': 'error', 'error': str(e)}
            if progress_cb:
                progress_cb(i + 1, len(indicators), f'❌ {label}: 抓取失败 - {e}')

    return results


# ═══════════════════════════════════════════════════════════
#  4. Historical News & Intel Backfill
# ═══════════════════════════════════════════════════════════

def backfill_historical_intel(
    db: Any,
    start_date: str,
    end_date: str,
    search_fn: Callable | None = None,
    progress_cb: Callable | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Backfill historical news/intel using multiple strategies.

    Strategy layers (tried in order, from most reliable to least):
      1. Eastmoney News API (np-listapi — structured, precise dates, paginated)
      2. Sina Finance RSS (well-structured, precise dates)
      3. Investing.com RSS (Chinese edition — macro/bonds/forex)
      4. Google News RSS with date-restricted queries (most precise dates)
      5. Web search with time-filter (DDG df=m for past month, iterating)
      6. CLS telegraph (only real-time, limited historical value)

    ★ CACHE-AWARE: Uses trading_intel_crawl_log to skip category+query+month
    combos that have already been successfully crawled, so repeat simulation
    runs are near-instant for the intel phase.

    All items are stored in trading_intel_cache with published_date set.
    """
    import hashlib

    from lib.trading.intel import INTEL_SOURCES, check_crawl_coverage, crawl_intel_source, record_crawl

    if search_fn is None:
        # Default search function using our web search infrastructure
        def search_fn(query, max_results=8):
            try:
                from lib.search import web_search
                return web_search(query, max_results=max_results)
            except Exception as e:
                logger.debug('[HistData] Default search_fn failed: %s', e)
                return []

    total_fetched = 0
    total_queries = 0
    total_cached = 0
    category_results = {}

    # Parse date range
    try:
        sd = datetime.strptime(start_date, '%Y-%m-%d')
        ed = datetime.strptime(end_date, '%Y-%m-%d')
    except (ValueError, TypeError) as e:
        logger.error('[HistData] Invalid date range: %s~%s: %s', start_date, end_date, e)
        return {'error': f'Invalid date range: {e}'}

    # Split into monthly chunks for better search relevance
    monthly_chunks = _split_into_months(sd, ed)
    num_categories = len(INTEL_SOURCES)
    total_months = len(monthly_chunks)
    cal_days = (ed - sd).days + 1

    # ── Pre-scan: check crawl log to find which queries are already done ──
    # Key: (category, source_key, crawl_date) → already crawled?
    # This lets us skip entire query+month combos that succeeded before.
    _crawl_log_cache: dict = {}

    def _is_already_crawled(category: str, query: str, crawl_date: str) -> bool:
        """Check if this specific query was already crawled for this date."""
        source_key = hashlib.md5(query.encode()).hexdigest()[:12]
        cache_key = (category, source_key, crawl_date)
        if cache_key not in _crawl_log_cache:
            try:
                row = db.execute(
                    'SELECT items_fetched FROM trading_intel_crawl_log '
                    'WHERE category=? AND source_key=? AND crawl_date=? AND status=?',
                    (category, source_key, crawl_date, 'ok')
                ).fetchone()
                _crawl_log_cache[cache_key] = row is not None
            except Exception as e:
                logger.debug('[HistData] Crawl log check failed: %s', e)
                _crawl_log_cache[cache_key] = False
        return _crawl_log_cache[cache_key]

    # ── Count how many queries actually need fetching (exclude cached) ──
    queries_needing_fetch = 0
    queries_already_cached = 0
    for cat, src in INTEL_SOURCES.items():
        for chunk_start, chunk_end in monthly_chunks:
            ce = chunk_end.strftime('%Y-%m-%d')
            month_label = chunk_start.strftime('%Y年%m月')
            for query in src['queries']:
                date_query = f'{query} {month_label}'
                if _is_already_crawled(cat, date_query, ce):
                    queries_already_cached += 1
                else:
                    queries_needing_fetch += 1

    total_queries_planned = queries_needing_fetch + queries_already_cached
    queries_done = 0

    if progress_cb:
        if queries_already_cached > 0:
            progress_cb(
                0, total_queries_planned,
                f'📰 情报缓存命中: {queries_already_cached}/{total_queries_planned} 次查询已有缓存，'
                f'仅需新抓取 {queries_needing_fetch} 次'
            )
        else:
            progress_cb(
                0, total_queries_planned,
                f'📰 需要抓取 {num_categories} 类情报 × {total_months} 个月份 '
                f'(共{total_queries_planned}次查询，覆盖 {start_date}~{end_date} 共{cal_days}天)'
            )

    # ── Phase 0: Structured API sources (Eastmoney News API, RSS feeds) ──
    # These provide better-structured data with precise dates and don't
    # need date-appended search queries.
    try:
        from lib.trading.news_apis import fetch_structured_news_sources
        api_fetched = fetch_structured_news_sources(
            db, start_date, end_date, progress_cb=progress_cb,
        )
        total_fetched += api_fetched
        if api_fetched > 0:
            logger.info('[HistData] Structured news APIs: +%d items', api_fetched)
    except Exception as e:
        logger.warning('[HistData] Structured news API fetch failed: %s', e)

    for cat_idx, (cat, src) in enumerate(sorted(INTEL_SOURCES.items(), key=lambda x: x[1]['priority'])):
        cat_fetched = 0
        cat_cached = 0
        cat_errors = 0
        num_queries_in_cat = len(src['queries'])

        if progress_cb:
            progress_cb(
                queries_done, total_queries_planned,
                f'📝 [{cat_idx+1}/{num_categories}] 开始「{src["label"]}」'
                f'({num_queries_in_cat}个关键词 × {total_months}个月)...'
            )

        for chunk_idx, (chunk_start, chunk_end) in enumerate(monthly_chunks):
            cs = chunk_start.strftime('%Y-%m-%d')
            ce = chunk_end.strftime('%Y-%m-%d')
            month_label = chunk_start.strftime('%Y年%m月')
            chunk_fetched = 0

            for q_idx, query in enumerate(src['queries']):
                # Date-restricted query: append date range for search relevance
                date_query = f'{query} {month_label}'

                # ★ CACHE CHECK: skip if this query+month was already crawled
                if _is_already_crawled(cat, date_query, ce):
                    cat_cached += 1
                    total_cached += 1
                    queries_done += 1
                    continue

                # ★ Per-query progress: user sees exactly what's being fetched
                if progress_cb:
                    progress_cb(
                        queries_done, total_queries_planned,
                        f'🔍 {src["label"]} · {month_label} · '
                        f'[{q_idx+1}/{num_queries_in_cat}] {query[:30]}...'
                    )

                try:
                    # crawl_intel_source handles everything:
                    #   multi_source_search (Google News RSS + CLS + DDG)
                    #   → dedup → store in trading_intel_cache
                    n = crawl_intel_source(
                        db, cat, date_query, search_fn,
                        crawl_date=ce,
                        use_multi_source=True,
                    )
                    chunk_fetched += n
                    cat_fetched += n
                    total_fetched += n
                    total_queries += 1

                except Exception as e:
                    logger.warning('[HistData] Intel backfill error %s/%s: %s',
                                   cat, date_query[:40], e)
                    cat_errors += 1

                queries_done += 1
                time.sleep(0.5)  # Rate limit

            # ★ Per-chunk summary (monthly)
            if progress_cb:
                progress_cb(
                    queries_done, total_queries_planned,
                    f'📰 {src["label"]} · {month_label}: '
                    f'+{chunk_fetched}条 (该类累计{cat_fetched}条，总计{total_fetched}条)'
                )

        category_results[cat] = {
            'label': src['label'],
            'fetched': cat_fetched,
            'cached': cat_cached,
            'errors': cat_errors,
        }
        if cat_fetched > 0:
            logger.info('[HistData] Intel backfill %s: %d items, %d cached, %d errors',
                        src['label'], cat_fetched, cat_cached, cat_errors)
        elif cat_cached > 0:
            logger.info('[HistData] Intel backfill %s: all %d queries cached, 0 new fetches',
                        src['label'], cat_cached)

    return {
        'total_fetched': total_fetched,
        'total_queries': total_queries,
        'total_cached': total_cached,
        'categories': category_results,
        'date_range': {'start': start_date, 'end': end_date},
    }


# ═══════════════════════════════════════════════════════════
#  5. Full Historical Data Fetch (orchestrator)
# ═══════════════════════════════════════════════════════════

def run_full_historical_fetch(
    db: Any,
    symbols: list[str],
    start_date: str,
    end_date: str,
    on_progress: Callable | None = None,
    search_fn: Callable | None = None,
    skip_intel: bool = False,
) -> dict[str, Any]:
    """Orchestrate full historical data fetch for simulation.

    Runs all four data fetchers in sequence:
      1. Asset prices (NAV history)
      2. Index history (major indices)
      3. Macro data (CPI/PPI/PMI/M2)
      4. Intel/news backfill (if not skipped)

    Args:
        db: Database connection.
        symbols: Fund/ETF codes to fetch.
        start_date: Period start 'YYYY-MM-DD'.
        end_date: Period end 'YYYY-MM-DD'.
        on_progress: Callback fn(phase, done, total, msg).
        search_fn: Web search function for intel backfill.
        skip_intel: Skip intel backfill (use when intel already populated).

    Returns:
        Comprehensive result dict with status for each phase.
    """
    start_time = time.time()
    try:
        cal_days = (datetime.strptime(end_date, '%Y-%m-%d') -
                    datetime.strptime(start_date, '%Y-%m-%d')).days + 1
    except Exception as _e:
        logger.debug('[HistData] Date parse failed for cal_days, defaulting to 180: %s', _e)
        cal_days = 180

    def _phase_progress(phase):
        def cb(done, total, msg=''):
            if on_progress:
                on_progress(phase, done, total, msg)
        return cb

    result = {
        'start_date': start_date,
        'end_date': end_date,
        'symbols': symbols,
        'phases': {},
    }

    if on_progress:
        on_progress('deps', 1, 1, '✅ 所有依赖已就绪')

    # ── Overview ──
    fund_labels = [_fund_label(s) for s in symbols]
    phases_to_run = 3 + (0 if skip_intel else 1)
    if on_progress:
        on_progress('prices', 0, 0,
                     f'🚀 数据抓取任务启动！时段 {start_date} → {end_date} '
                     f'(共{cal_days}天)')
        on_progress('prices', 0, 0,
                     f'📋 待抓取: ① 价格数据({len(symbols)}只标的) '
                     f'② 大盘指数(6个) ③ 宏观经济(4项)'
                     f'{" ④ 新闻情报" if not skip_intel else ""}')
        on_progress('prices', 0, 0,
                     f'📊 标的列表: {", ".join(fund_labels)}')

    # Phase 1: Asset prices
    logger.info('[HistData] Phase 1: Fetching price history for %d symbols (%s~%s)',
                len(symbols), start_date, end_date)
    if on_progress:
        on_progress('prices', 0, 0,
                     f'══════ 阶段 1/{phases_to_run}: 价格数据 ══════')
    result['phases']['prices'] = fetch_and_store_price_history(
        db, symbols, start_date, end_date, _phase_progress('prices')
    )
    # Phase 1 summary
    prices_result = result['phases']['prices']
    total_price_records = sum(v.get('count', 0) for v in prices_result.values())
    cached_count = sum(1 for v in prices_result.values() if v.get('status') == 'cached')
    fetched_count = sum(1 for v in prices_result.values() if v.get('status') == 'fetched')
    if on_progress:
        on_progress('prices', len(symbols), len(symbols),
                     f'✅ 价格数据完成: {total_price_records}天 '
                     f'({cached_count}只命中缓存, {fetched_count}只在线获取)')

    # Phase 2: Index history
    logger.info('[HistData] Phase 2: Fetching index history')
    if on_progress:
        on_progress('indices', 0, 0,
                     f'══════ 阶段 2/{phases_to_run}: 大盘指数 ══════')
    result['phases']['indices'] = fetch_and_store_index_history(
        db, start_date, end_date, _phase_progress('indices')
    )

    # Phase 3: Macro data
    logger.info('[HistData] Phase 3: Fetching macro data')
    if on_progress:
        on_progress('macro', 0, 0,
                     f'══════ 阶段 3/{phases_to_run}: 宏观经济指标 ══════')
    result['phases']['macro'] = fetch_and_store_macro_data(
        db, start_date, end_date, _phase_progress('macro')
    )

    # Phase 4: Intel backfill
    if not skip_intel:
        logger.info('[HistData] Phase 4: Backfilling historical intel')
        if on_progress:
            on_progress('intel', 0, 0,
                         f'══════ 阶段 4/{phases_to_run}: 新闻情报 ══════')
        result['phases']['intel'] = backfill_historical_intel(
            db, start_date, end_date,
            search_fn=search_fn,
            progress_cb=_phase_progress('intel'),
        )
    else:
        result['phases']['intel'] = {'status': 'skipped'}
        if on_progress:
            on_progress('intel', 1, 1, '⏭️ 已跳过新闻情报抓取（用户选择）')

    elapsed = round(time.time() - start_time, 1)
    result['duration_seconds'] = elapsed

    # ── Final summary ──
    if on_progress:
        intel_result = result['phases'].get('intel', {})
        cached_info = ''
        if isinstance(intel_result, dict) and intel_result.get('total_cached', 0) > 0:
            cached_info = f'（情报缓存命中 {intel_result["total_cached"]} 次查询）'
        on_progress('intel', 1, 1,
                     f'🎉 全部数据抓取完成！耗时 {elapsed} 秒{cached_info}')

    logger.info('[HistData] Full fetch completed in %.1fs', elapsed)
    return result


# ═══════════════════════════════════════════════════════════
#  6. Coverage Report
# ═══════════════════════════════════════════════════════════

def get_data_coverage_report(
    db: Any,
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Report what historical data is available for the given period.

    Returns per-category coverage statistics.
    """
    _ensure_sim_tables(db)

    report = {
        'date_range': {'start': start_date, 'end': end_date},
        'prices': {},
        'indices': {},
        'macro': {},
        'intel': {},
    }

    # Price coverage — use the first symbol's actual count as the
    # ground-truth number of trading days (avoids weekday-only estimate
    # that ignores Chinese holidays like CNY/National Day).
    _actual_trading_days = 0

    for sym in symbols:
        row = db.execute(
            'SELECT COUNT(*) as cnt, MIN(date) as mn, MAX(date) as mx '
            'FROM trading_sim_prices WHERE symbol=? AND date>=? AND date<=?',
            (sym, start_date, end_date)
        ).fetchone()
        cnt = row['cnt'] if row else 0
        if cnt > _actual_trading_days:
            _actual_trading_days = cnt
        report['prices'][sym] = {
            'count': cnt,
            'first': row['mn'] if row else '',
            'last': row['mx'] if row else '',
        }

    # Index coverage
    for secid, name in _INDEX_KLINE_CODES:
        row = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_sim_indices '
            'WHERE secid=? AND date>=? AND date<=?',
            (secid, start_date, end_date)
        ).fetchone()
        report['indices'][name] = {
            'count': row['cnt'] if row else 0,
        }

    # Macro coverage
    for key in ['cpi', 'ppi', 'pmi', 'm2']:
        row = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_sim_macro '
            'WHERE indicator=? AND date>=? AND date<=?',
            (key, start_date, end_date)
        ).fetchone()
        report['macro'][key] = {'count': row['cnt'] if row else 0}

    # Intel coverage
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date>=? AND published_date<=? AND published_date != ''",
        (start_date, end_date)
    ).fetchone()
    intel_count = row['cnt'] if row else 0

    # Per-date distribution
    date_rows = db.execute(
        "SELECT published_date, COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date>=? AND published_date<=? AND published_date != '' "
        "GROUP BY published_date ORDER BY published_date",
        (start_date, end_date)
    ).fetchall()

    total_cal_days = (datetime.strptime(end_date, '%Y-%m-%d') -
                      datetime.strptime(start_date, '%Y-%m-%d')).days + 1
    dates_with_intel = len(date_rows)

    report['intel'] = {
        'total_items': intel_count,
        'dates_covered': dates_with_intel,
        'total_days': total_cal_days,
        'coverage_pct': round(dates_with_intel / max(total_cal_days, 1) * 100, 1),
        'avg_items_per_day': round(intel_count / max(dates_with_intel, 1), 1),
    }

    return report


# ═══════════════════════════════════════════════════════════
#  Query Helpers (for simulator to retrieve stored data)
# ═══════════════════════════════════════════════════════════

def get_price_at(db: Any, symbol: str, as_of: str) -> dict | None:
    """Get the most recent price for a symbol on or before as_of."""
    row = db.execute(
        'SELECT * FROM trading_sim_prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1',
        (symbol, as_of)
    ).fetchone()
    return dict(row) if row else None


def get_prices_range(db: Any, symbol: str, start: str, end: str) -> list[dict]:
    """Get all prices for a symbol within a date range."""
    rows = db.execute(
        'SELECT * FROM trading_sim_prices WHERE symbol=? AND date>=? AND date<=? ORDER BY date',
        (symbol, start, end)
    ).fetchall()
    return [dict(r) for r in rows]


def get_index_at(db: Any, secid: str, as_of: str) -> dict | None:
    """Get index data on or before as_of."""
    row = db.execute(
        'SELECT * FROM trading_sim_indices WHERE secid=? AND date<=? ORDER BY date DESC LIMIT 1',
        (secid, as_of)
    ).fetchone()
    return dict(row) if row else None


def get_macro_at(db: Any, indicator: str, as_of: str) -> dict | None:
    """Get latest macro indicator value on or before as_of."""
    row = db.execute(
        'SELECT * FROM trading_sim_macro WHERE indicator=? AND date<=? ORDER BY date DESC LIMIT 1',
        (indicator, as_of)
    ).fetchone()
    return dict(row) if row else None


def build_market_snapshot(db: Any, as_of: str) -> str:
    """Build a text summary of market conditions at a point in time.

    This replaces the real-time market.py functions for historical simulation.
    """
    lines = ['## 市场概况']

    # Major indices
    index_lines = []
    for secid, name in _INDEX_KLINE_CODES:
        idx = get_index_at(db, secid, as_of)
        if idx:
            pct = idx.get('change_pct', 0)
            marker = '📈' if pct > 0 else '📉' if pct < 0 else '➡️'
            index_lines.append(
                f'  {marker} {name}: {idx["close"]:.2f} ({pct:+.2f}%)'
            )
    if index_lines:
        lines.append('### 主要指数')
        lines.extend(index_lines)

    # Macro indicators
    macro_lines = []
    for key, label in [('cpi', 'CPI'), ('ppi', 'PPI'), ('pmi', 'PMI'), ('m2', 'M2')]:
        m = get_macro_at(db, key, as_of)
        if m:
            macro_lines.append(f'  - {label}: {m["value"]:.1f} (截至 {m["date"]})')
    if macro_lines:
        lines.append('### 宏观指标')
        lines.extend(macro_lines)

    return '\n'.join(lines) if len(lines) > 1 else ''


# ═══════════════════════════════════════════════════════════
#  Database Table Setup
# ═══════════════════════════════════════════════════════════

def _ensure_sim_tables(db: Any):
    """Create simulation data tables if they don't exist."""
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_prices (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT '',
            nav REAL NOT NULL DEFAULT 0,
            acc_nav REAL NOT NULL DEFAULT 0,
            change_pct REAL NOT NULL DEFAULT 0,
            open REAL NOT NULL DEFAULT 0,
            high REAL NOT NULL DEFAULT 0,
            low REAL NOT NULL DEFAULT 0,
            close REAL NOT NULL DEFAULT 0,
            volume REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            UNIQUE(symbol, date)
        )
    ''')
    # Migration: add OHLCV columns if table exists but columns don't.
    # Use _column_exists() check instead of try-ALTER-except-rollback to avoid
    # ~30s FUSE WAL fsync per failed ALTER statement.
    from lib.database import _column_exists as _col_exists
    for col in ('open', 'high', 'low', 'close', 'volume', 'amount'):
        if not _col_exists(db, 'trading_sim_prices', col):
            db.execute(f'ALTER TABLE trading_sim_prices ADD COLUMN {col} REAL NOT NULL DEFAULT 0')
            db.commit()
            logger.info('[HistData] Added column %s to trading_sim_prices', col)
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_indices (
            id SERIAL PRIMARY KEY,
            secid TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT '',
            open REAL NOT NULL DEFAULT 0,
            close REAL NOT NULL DEFAULT 0,
            high REAL NOT NULL DEFAULT 0,
            low REAL NOT NULL DEFAULT 0,
            volume REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            change_pct REAL NOT NULL DEFAULT 0,
            UNIQUE(secid, date)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_macro (
            id SERIAL PRIMARY KEY,
            indicator TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL DEFAULT 0,
            UNIQUE(indicator, date)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_sessions (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            initial_capital REAL NOT NULL DEFAULT 100000,
            current_cash REAL NOT NULL DEFAULT 100000,
            symbols TEXT NOT NULL DEFAULT '[]',
            start_date TEXT NOT NULL DEFAULT '',
            end_date TEXT NOT NULL DEFAULT '',
            step_days INT NOT NULL DEFAULT 5,
            current_sim_date TEXT NOT NULL DEFAULT '',
            total_steps INT NOT NULL DEFAULT 0,
            completed_steps INT NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0,
            total_trades INT NOT NULL DEFAULT 0,
            winning_trades INT NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_positions (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL DEFAULT '',
            asset_name TEXT NOT NULL DEFAULT '',
            shares REAL NOT NULL DEFAULT 0,
            buy_price REAL NOT NULL DEFAULT 0,
            buy_date TEXT NOT NULL DEFAULT '',
            current_price REAL NOT NULL DEFAULT 0,
            stop_loss REAL NOT NULL DEFAULT 5,
            take_profit REAL NOT NULL DEFAULT 10,
            status TEXT NOT NULL DEFAULT 'open',
            close_price REAL NOT NULL DEFAULT 0,
            close_date TEXT NOT NULL DEFAULT '',
            pnl REAL NOT NULL DEFAULT 0,
            pnl_pct REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS trading_sim_journal (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            sim_date TEXT NOT NULL DEFAULT '',
            entry_type TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            reasoning TEXT NOT NULL DEFAULT '',
            signals_json TEXT NOT NULL DEFAULT '{}',
            confidence INT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT ''
        )
    ''')
    try:
        db.commit()
    except Exception as _commit_err:
        logger.debug('[HistData] Final schema commit failed (non-fatal): %s', _commit_err)


# ═══════════════════════════════════════════════════════════
#  Private Helpers
# ═══════════════════════════════════════════════════════════

def _trading_days_between(start: str, end: str) -> int:
    """Count weekdays between two dates (approximate trading days).

    Iterates day-by-day to count Mon–Fri days.  Still approximate
    because Chinese public holidays are not excluded, but much more
    accurate than the old ``int(cal_days * 5/7)`` one-liner.
    Used **only** for progress-bar display — never for cache-validity
    decisions (those use date-range coverage checks instead).
    """
    try:
        sd = datetime.strptime(start, '%Y-%m-%d')
        ed = datetime.strptime(end, '%Y-%m-%d')
        count = 0
        current = sd
        while current <= ed:
            if current.weekday() < 5:          # Mon-Fri
                count += 1
            current += timedelta(days=1)
        return count
    except Exception as _e:
        logger.debug('[HistData] _trading_days_between parse failed, defaulting to 120: %s', _e)
        return 120


def _days_between(d1: str, d2: str) -> int:
    """Absolute calendar-day distance between two YYYY-MM-DD strings."""
    try:
        return abs((datetime.strptime(d2, '%Y-%m-%d') -
                    datetime.strptime(d1, '%Y-%m-%d')).days)
    except Exception as _e:
        logger.debug('[HistData] _days_between parse failed, defaulting to 999: %s', _e)
        return 999


def _split_into_months(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split a date range into monthly chunks."""
    chunks = []
    current = start.replace(day=1)
    while current <= end:
        month_end = (current.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        chunk_start = max(current, start)
        chunk_end = min(month_end, end)
        if chunk_start <= chunk_end:
            chunks.append((chunk_start, chunk_end))
        current = month_end + timedelta(days=1)
    return chunks


def _normalize_date(dt_str: str) -> str:
    """Normalize various date formats to YYYY-MM-DD."""
    if not dt_str:
        return ''
    # Already in correct format
    if re.match(r'^\d{4}-\d{2}-\d{2}$', dt_str):
        return dt_str
    # YYYYMMDD
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', dt_str)
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    # YYYY/MM/DD
    m = re.match(r'^(\d{4})/(\d{1,2})/(\d{1,2})$', dt_str)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    # YYYY年MM月
    m = re.match(r'^(\d{4})年(\d{1,2})月', dt_str)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}-01'
    # YYYY.M or YYYY.MM (M2 supply_of_money format)
    m = re.match(r'^(\d{4})\.(\d{1,2})$', dt_str)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}-01'
    return dt_str[:10]
