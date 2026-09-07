"""lib/trading/info.py — Fund info fetching, search, and fee calculation."""

import json
import re

from lib.log import get_logger
from tofu_trading.trading._common import (
    _get_default_client,
    classify_asset_code,
    stock_secid,
)
from tofu_trading.trading.nav import (
    _nav_from_db,
    _nav_from_holdings,
    _nav_from_memory,
    _nav_to_db,
    _nav_to_memory,
    get_latest_price,
)

logger = get_logger(__name__)

__all__ = [
    'fetch_asset_info',
    'search_asset',
    'search_asset_universal',
    'fetch_trading_fee_info',
    'fetch_trading_fees',
    'estimate_trade_fee',
    'calc_buy_fee',
    'calc_sell_fee',
]


# ═══════════════════════════════════════════════════════════
#  Fund Info Fetching
# ═══════════════════════════════════════════════════════════

def fetch_asset_info(code, *, client=None):
    """Fetch basic asset info with 3-layer cache: memory → DB → API (fast-fail).
    Never blocks more than 1.5s even if network is down.

    Args:
        code:   Fund code.
        client: Optional ``TradingClient`` instance for dependency injection.
                Defaults to the lazily-initialised module-level singleton.
    """
    if client is None:
        client = _get_default_client()
    # L1: memory cache (sub-millisecond)
    mem = _nav_from_memory(code)
    if mem:
        return {
            'code': code, 'name': mem.get('name', ''),
            'nav': str(mem['nav']), 'nav_date': mem.get('date', ''),
            'est_nav': '', 'est_change': '', 'est_time': '',
            '_source': mem.get('source', 'mem_cache'),
        }
    # L2: DB cache (< 1ms)
    db_hit = _nav_from_db(code)
    if db_hit:
        return {
            'code': code, 'name': db_hit.get('asset_name', ''),
            'nav': str(db_hit['nav']), 'nav_date': db_hit.get('nav_date', ''),
            'est_nav': '', 'est_change': '', 'est_time': '',
            '_source': 'db_cache',
        }
    # L3: try network only if external is reachable
    if client.check_network():
        info = _fetch_asset_info_remote(code, client=client)
        if info:
            # Populate both cache layers
            nav_val = info.get('nav', '')
            if nav_val:
                try:
                    _nav_to_memory(code, float(nav_val), info.get('nav_date', ''), info.get('name', ''), 'api')
                    _nav_to_db(code, float(nav_val), info.get('nav_date', ''), info.get('name', ''))
                except (ValueError, TypeError):
                    logger.debug('[FundInfo] Failed to cache NAV for %s: nav_val=%r', code, nav_val, exc_info=True)
            return info
    # L4: holdings cost fallback (always available)
    hld = _nav_from_holdings(code)
    if hld:
        return {
            'code': code, 'name': hld.get('asset_name', ''),
            'nav': str(hld['nav']), 'nav_date': hld.get('nav_date', ''),
            'est_nav': '', 'est_change': '', 'est_time': '',
            '_source': 'holdings_cost',
        }
    return None


def _fetch_stock_quote_remote(code, *, client=None):
    """Fetch real-time stock/ETF quote from eastmoney push2 API.

    Works for all exchange-traded securities: A-share stocks, ETFs, bonds.
    Uses push2delay.eastmoney.com (HTTP, no redirect issues).

    Args:
        code:   Stock/ETF code (e.g. '600519', '510300').
        client: Optional TradingClient for DI.

    Returns:
        Dict with code, name, nav (=price), nav_date, etc. or None on failure.
    """
    if client is None:
        client = _get_default_client()
    secid = stock_secid(code)
    url = (
        f'http://push2delay.eastmoney.com/api/qt/stock/get?'
        f'secid={secid}&ut=fa5fd1943c7b386f172d6893dbfba10b'
        f'&fields=f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f116,f117,f162,f167,f170'
        f'&fltt=2'  # float format — returns actual prices, not cents
        f'&cb=jQuery'
    )
    try:
        r = client.session.get(url, timeout=5, headers={
            **client.headers,
            'Referer': 'https://quote.eastmoney.com/',
        })
        text = r.text
        m = re.search(r'jQuery\((.*)\)', text, re.S)
        if not m:
            logger.debug('[StockQuote] No JSONP wrapper for %s', code)
            return None
        data = json.loads(m.group(1))
        d = data.get('data')
        if not d:
            logger.debug('[StockQuote] Empty data for %s', code)
            return None
        # f43=latest price (*1000 for some, but push2delay returns plain float)
        # f44=high, f45=low, f46=open, f47=volume, f48=amount
        # f51=high_limit, f52=low_limit, f55=turnover_rate
        # f57=code, f58=name, f116=total_mv, f117=circ_mv
        # f162=PE(dynamic), f167=PB, f170=change_pct
        price = d.get('f43')
        name = d.get('f58', '')
        change_pct = d.get('f170')
        pe = d.get('f162')
        pb = d.get('f167')
        total_mv = d.get('f116', 0)
        turnover = d.get('f55')

        if price is None or price == '-':
            logger.debug('[StockQuote] No price data for %s (possibly suspended)', code)
            return None

        price_f = float(price)

        from datetime import datetime as _dt
        today = _dt.now().strftime('%Y-%m-%d')

        return {
            'code': code,
            'name': name,
            'nav': str(round(price_f, 3)),
            'nav_date': today,
            'est_nav': '',
            'est_change': str(change_pct) if change_pct and change_pct != '-' else '',
            'est_time': '',
            'asset_type': classify_asset_code(code),
            'pe': float(pe) if pe and pe != '-' else None,
            'pb': float(pb) if pb and pb != '-' else None,
            'total_mv': float(total_mv) if total_mv and total_mv != '-' else None,
            'turnover': float(turnover) if turnover and turnover != '-' else None,
        }
    except Exception as e:
        logger.warning('[StockQuote] push2 quote failed for %s: %s', code, e, exc_info=True)
        return None


def _fetch_asset_info_remote(code, *, client=None):
    """Actually hit the network — only called when external network is confirmed reachable.

    For stocks/ETFs: uses push2delay real-time quote API.
    For open-end funds: tries eastmoney lsjz API, 1702.com fallback, pingzhongdata.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()

    # ── For stocks and ETFs, use push2 quote API (fast, reliable) ──
    asset_type = classify_asset_code(code)
    if asset_type in ('stock', 'etf', 'bond'):
        result = _fetch_stock_quote_remote(code, client=client)
        if result:
            return result
        # Fall through to fund APIs as last resort (ETFs can sometimes
        # have fund NAV data too)
        if asset_type == 'stock':
            return None  # stocks won't have fund NAV data

    # ── Strategy 1: eastmoney lsjz API (HTTPS, proxy-safe, most reliable) ──
    try:
        url_em = (f'https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery'
                  f'&fundCode={code}&pageIndex=1&pageSize=1')
        r = client.session.get(url_em, timeout=3, headers={
            **client.headers,
            'Referer': f'https://fundf10.eastmoney.com/jjjz_{code}.html',
        })
        if r.status_code == 200:
            text = r.text
            m = re.search(r'jQuery\((.*)\)', text, re.S)
            if m:
                try:
                    d = json.loads(m.group(1))
                except (json.JSONDecodeError, ValueError):
                    logger.warning('[Trading] Malformed JSONP in eastmoney API for code=%s', code, exc_info=True)
                    d = {}
                items = d.get('Data', {}).get('LSJZList', [])
                if items:
                    item = items[0]
                    nav_val = item.get('DWJZ', '')
                    nav_date = item.get('FSRQ', '')
                    # Also need fund name — try pingzhongdata
                    asset_name = ''
                    try:
                        url_pz = f'https://fund.eastmoney.com/pingzhongdata/{code}.js'
                        r_pz = client.session.get(url_pz, timeout=2)
                        if r_pz.status_code == 200:
                            nm = re.search(r'fS_name\s*=\s*"([^"]*)"', r_pz.text)
                            if nm:
                                asset_name = nm.group(1)
                    except Exception as e:
                        logger.debug('[Trading] pingzhongdata name lookup failed for %s: %s', code, e, exc_info=True)
                    return {
                        'code': code, 'name': asset_name,
                        'nav': nav_val, 'nav_date': nav_date,
                        'est_nav': '', 'est_change': '', 'est_time': '',
                    }
    except Exception as e:
        logger.warning('eastmoney API failed for %s: %s', code, e, exc_info=True)

    # ── Strategy 2: 1702.com (HTTP, may be blocked by proxy) ──
    try:
        url = f'http://fundgz.1702.com/js/{code}.js'
        r = client.session.get(url, timeout=1.5)
        if r.status_code == 200 and 'jsonpgz' in r.text:
            m = re.search(r'jsonpgz\((.*?)\)', r.text, re.S)
            if m:
                d = json.loads(m.group(1))
                return {
                    'code': d.get('fundcode', code),
                    'name': d.get('name', ''),
                    'nav': d.get('dwjz', ''),
                    'nav_date': d.get('jzrq', ''),
                    'est_nav': d.get('gsz', ''),
                    'est_change': d.get('gszzl', ''),
                    'est_time': d.get('gztime', ''),
                }
    except Exception as e:
        logger.warning('[Trading] 1702.com fallback failed for %s: %s', code, e, exc_info=True)

    # ── Strategy 3: pingzhongdata (name only, no NAV) ──
    try:
        url2 = f'https://fund.eastmoney.com/pingzhongdata/{code}.js'
        r2 = client.session.get(url2, timeout=2)
        if r2.status_code == 200:
            name_m = re.search(r'fS_name\s*=\s*"([^"]*)"', r2.text)
            code_m = re.search(r'fS_code\s*=\s*"([^"]*)"', r2.text)
            return {
                'code': code_m.group(1) if code_m else code,
                'name': name_m.group(1) if name_m else '',
                'nav': '', 'nav_date': '', 'est_nav': '', 'est_change': '', 'est_time': '',
            }
    except Exception as e:
        logger.warning('[Trading] pingzhongdata fallback failed for %s: %s', code, e, exc_info=True)
    return None


# ═══════════════════════════════════════════════════════════
#  Fund Search
# ═══════════════════════════════════════════════════════════

def search_asset(keyword, *, client=None):
    """Search assets by keyword/code. Fast-fails if network unreachable.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    if not client.check_network():
        # Return from DB holdings as fallback
        try:
            from tofu_trading.storage import DOMAIN_TRADING, get_thread_db
            from tofu_trading.identity import DEFAULT_OWNER_ID

            db = get_thread_db(
                DOMAIN_TRADING, owner_user_id=DEFAULT_OWNER_ID)
            # Offline fallback only: mines the default owner's holdings for
            # names when the market API is unreachable. Not a per-user read --
            # this returns SYMBOLS (public market data), never positions.
            rows = db.execute(
                "SELECT DISTINCT symbol, asset_name FROM trading_holdings "
                "WHERE user_id=? AND (symbol LIKE ? OR asset_name LIKE ?) LIMIT 20",
                (DEFAULT_OWNER_ID, f'%{keyword}%', f'%{keyword}%')
            ).fetchall()
            return [{'code': r['symbol'], 'name': r.get('asset_name', ''), 'type': ''} for r in rows]
        except Exception as e:
            logger.warning('[Trading] DB holdings fallback search failed for keyword=%s: %s', keyword, e, exc_info=True)
            return []
    try:
        url = f'https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?callback=&m=1&key={keyword}'
        r = client.session.get(url, timeout=3)
        text = r.text.strip()
        if text.startswith('('):
            text = text[1:]
        if text.endswith(')'):
            text = text[:-1]
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning('[Trading] search API returned malformed JSON for keyword=%s', keyword, exc_info=True)
            return []
        results = []
        for item in data.get('Datas', []):
            results.append({
                'code': item.get('CODE', ''),
                'name': item.get('NAME', ''),
                'type': item.get('FundBaseInfo', {}).get('FTYPE', '') if isinstance(item.get('FundBaseInfo'), dict) else '',
            })
        return results[:20]
    except Exception as e:
        logger.warning('[Trading] eastmoney search API failed for keyword=%s: %s', keyword, e, exc_info=True)
        return []


def search_asset_universal(keyword, *, client=None):
    """Search BOTH stocks and funds by keyword/code.

    Uses eastmoney's universal search suggest API which covers:
      - A-shares (Shanghai/Shenzhen stocks)
      - ETFs
      - Open-end funds
      - Index funds
      - Bonds

    Falls back to fund-only search if universal API fails.

    Args:
        keyword: Search keyword (code, name, pinyin abbreviation).
        client: Optional TradingClient instance.

    Returns:
        List of dicts: [{code, name, type, market}, ...]
    """
    if not keyword or not keyword.strip():
        return []
    keyword = keyword.strip()
    if client is None:
        client = _get_default_client()

    results = []

    # ── Strategy 1: EastMoney stock search (searchapi) ──
    try:
        url = ('http://searchapi.eastmoney.com/api/suggest/get'
               f'?input={keyword}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=20')
        r = client.session.get(url, timeout=3)
        data = r.json()
        quote_list = data.get('QuotationCodeTable', {}).get('Data') or []
        seen_codes = set()
        for item in quote_list:
            code = item.get('Code', '')
            name = item.get('Name', '')
            # Determine asset type from SecurityTypeName or market code
            sec_type = item.get('SecurityTypeName', '')
            market_id = str(item.get('MktNum', '') or item.get('Mkt', ''))
            # Filter to A-shares, ETFs, and funds only (skip HK, US, futures, etc.)
            # Market: 0=SZ, 1=SH for A-shares
            if market_id not in ('0', '1'):
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            # Classify type
            asset_type = ''
            if 'ETF' in name.upper() or 'ETF' in sec_type.upper():
                asset_type = 'ETF'
            elif sec_type in ('沪A', '深A', 'A股', '') or re.match(r'^(60|00|30|68)\d{4}$', code):
                asset_type = '股票'
            elif re.match(r'^(51|15|16)\d{4}$', code):
                asset_type = 'ETF'
            elif re.match(r'^(11|12)\d{4}$', code):
                asset_type = '债券'
            else:
                asset_type = sec_type or '其他'
            results.append({
                'code': code,
                'name': name,
                'type': asset_type,
                'market': 'SH' if market_id == '1' else 'SZ',
            })
    except Exception as e:
        logger.debug('[Trading] stock search API failed for keyword=%s: %s', keyword, e)

    # ── Strategy 2: EastMoney fund search (for open-end funds not in stock search) ──
    try:
        url = f'https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?callback=&m=1&key={keyword}'
        r = client.session.get(url, timeout=3)
        text = r.text.strip()
        if text.startswith('('):
            text = text[1:]
        if text.endswith(')'):
            text = text[:-1]
        fund_data = json.loads(text)
        seen_codes = {r['code'] for r in results}
        for item in (fund_data.get('Datas') or []):
            code = item.get('CODE', '')
            if code in seen_codes:
                continue
            seen_codes.add(code)
            fund_type = ''
            if isinstance(item.get('FundBaseInfo'), dict):
                fund_type = item['FundBaseInfo'].get('FTYPE', '')
            results.append({
                'code': code,
                'name': item.get('NAME', ''),
                'type': fund_type or '基金',
                'market': '',
            })
    except Exception as e:
        logger.debug('[Trading] fund search API failed for keyword=%s: %s', keyword, e)

    return results[:30]


# ═══════════════════════════════════════════════════════════
#  Fee Calculation
# ═══════════════════════════════════════════════════════════

def fetch_trading_fee_info(code, *, client=None):
    """Fetch trading fee information.

    ⚠ NOT FOR USE INSIDE A BACKTEST OR SIMULATION LOOP.
    ---------------------------------------------------
    The open-end fund branch below SCRAPES THE VENDOR over the network.
    Measured on this host: ``003003`` took **1068 ms** and ``110011`` 295 ms,
    versus 0.0 ms for exchange-traded codes. Calling this per simulated trade
    does two unacceptable things:

      1. Destroys determinism — the same historical inputs produce different
         results depending on whether the network answered. A backtest whose
         output depends on connectivity is not evidence of anything.
      2. Costs seconds-to-minutes per run (hundreds of decision points).

    Worse, the failure is SILENT: the scrape is wrapped in check_network() +
    try/except, so an unreachable vendor degrades to a default instead of
    raising. Nothing looks wrong; the numbers are just quietly different.

    **Decision loops MUST go through** ``tofu_trading.trading.fee_book``:
    ``FeeBook.prewarm()`` performs any I/O once, up front, and
    ``compute_fee`` / ``FeeBook.fee_for`` are then pure. This function remains
    for interactive/UI paths (a single quote for one asset), where a one-off
    fetch is exactly what the caller wants.

    For stocks: uses A-share commission + stamp tax structure.
    For funds: scrapes eastmoney for subscription/redemption fees.

    Args:
        code:   Asset code.
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()

    # ── Stock fee structure (A-share) ──
    asset_type = classify_asset_code(code)
    if asset_type == 'stock':
        return {
            'asset_type': 'stock',
            'buy_fee_rate': 0.00025,       # Commission ~0.025% (min ¥5)
            'sell_fee_rules': [
                {'days': 0, 'rate': 0.00075},  # Commission 0.025% + Stamp tax 0.05%
            ],
            'commission_rate': 0.00025,     # Broker commission (negotiable, ~万2.5)
            'stamp_tax_rate': 0.0005,       # Stamp tax 0.05% (sell only)
            'transfer_fee_rate': 0.00001,   # Transfer fee 0.001% (Shanghai only)
            'management_fee': 0,            # No management fee for stocks
            'custody_fee': 0,               # No custody fee for stocks
            'min_commission': 5.0,          # Minimum commission ¥5
        }

    # ── ETF fee structure ──
    if asset_type == 'etf':
        return {
            'asset_type': 'etf',
            'buy_fee_rate': 0.00025,       # Commission ~0.025%
            'sell_fee_rules': [
                {'days': 0, 'rate': 0.00025},  # Commission only, no stamp tax
            ],
            'commission_rate': 0.00025,
            'stamp_tax_rate': 0,            # No stamp tax for ETFs
            'management_fee': 0.005,        # ~0.5%/year typical
            'custody_fee': 0.001,           # ~0.1%/year typical
            'min_commission': 0.1,          # Some brokers waive minimum for ETFs
        }

    # ── Fund fee structure (open-end) ──
    default = {
        'asset_type': 'fund',
        'buy_fee_rate': 0.0015,
        'sell_fee_rules': [
            {'days': 7, 'rate': 0.015},
            {'days': 30, 'rate': 0.005},
            {'days': 365, 'rate': 0.0025},
            {'days': 730, 'rate': 0},
        ],
        'management_fee': 0.012,
        'custody_fee': 0.002,
    }
    if not client.check_network():
        return default
    try:
        url = f'https://fundf10.eastmoney.com/jjfl_{code}.html'
        r = client.session.get(url, timeout=3)
        if r.status_code == 200:
            text = r.text
            buy_m = re.search(r'申购费率.*?(\d+\.?\d*)%', text)
            mgmt_m = re.search(r'管理费率.*?(\d+\.?\d*)%', text)
            cust_m = re.search(r'托管费率.*?(\d+\.?\d*)%', text)
            if buy_m:
                default['buy_fee_rate'] = float(buy_m.group(1)) / 100
            if mgmt_m:
                default['management_fee'] = float(mgmt_m.group(1)) / 100
            if cust_m:
                default['custody_fee'] = float(cust_m.group(1)) / 100
    except Exception as e:
        logger.debug('[Trading] fee info scraping failed for %s: %s', code, e, exc_info=True)
    return default


def estimate_trade_fee(code, amount, action='buy', holding_days=0, *, client=None):
    """Estimate trade fee for buy/sell.

    Handles different fee structures for stocks, ETFs, and funds.

    Args:
        code:         Asset code.
        amount:       Trade amount in CNY.
        action:       'buy' or 'sell'.
        holding_days: Days held (for fund redemption fee tiers).
        client:       Optional ``TradingClient`` instance for dependency injection.
    """
    fees = fetch_trading_fee_info(code, client=client)
    asset_type = fees.get('asset_type', 'fund')

    # ── Stock/ETF fee calculation ──
    if asset_type in ('stock', 'etf'):
        commission_rate = fees.get('commission_rate', 0.00025)
        min_commission = fees.get('min_commission', 5.0)
        commission = max(amount * commission_rate, min_commission)
        stamp_tax = 0
        transfer_fee = 0

        if action == 'sell':
            stamp_tax_rate = fees.get('stamp_tax_rate', 0.0005 if asset_type == 'stock' else 0)
            stamp_tax = amount * stamp_tax_rate
            # Transfer fee (Shanghai stocks only — approximated for all here)
            transfer_fee = amount * fees.get('transfer_fee_rate', 0)

        total_fee = commission + stamp_tax + transfer_fee
        total_rate = total_fee / amount if amount > 0 else 0
        return {
            'fee_rate': round(total_rate, 6),
            'fee_amount': round(total_fee, 2),
            'net_amount': round(amount - total_fee, 2),
            'commission': round(commission, 2),
            'stamp_tax': round(stamp_tax, 2),
            'transfer_fee': round(transfer_fee, 2),
            'asset_type': asset_type,
        }

    # ── Fund fee calculation ──
    if action == 'buy':
        rate = fees['buy_fee_rate']
        fee = amount * rate
        return {
            'fee_rate': rate,
            'fee_amount': round(fee, 2),
            'net_amount': round(amount - fee, 2),
            'asset_type': 'fund',
        }
    else:  # sell
        rate = 0
        for rule in sorted(fees['sell_fee_rules'], key=lambda x: x['days']):
            if holding_days < rule['days']:
                rate = rule['rate']
                break
        fee = amount * rate
        return {
            'fee_rate': rate,
            'fee_amount': round(fee, 2),
            'net_amount': round(amount - fee, 2),
            'asset_type': 'fund',
        }


def fetch_trading_fees(code, *, client=None):
    """Return structured fee info for an asset with human-readable summary.

    Handles stocks (commission + stamp tax), ETFs, and funds (subscription + redemption).

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    raw = fetch_trading_fee_info(code, client=client)
    asset_type = raw.get('asset_type', 'fund')

    # ── Stock/ETF summary ──
    if asset_type in ('stock', 'etf'):
        comm_rate = raw.get('commission_rate', 0.00025)
        stamp_tax = raw.get('stamp_tax_rate', 0)
        summary_parts = [f"佣金 {comm_rate*100:.3f}%"]
        if stamp_tax > 0:
            summary_parts.append(f"印花税 {stamp_tax*100:.2f}%(卖出)")
        if raw.get('min_commission', 0) > 0:
            summary_parts.append(f"最低佣金 ¥{raw['min_commission']:.0f}")
        summary_parts.append('无管理费/托管费')
        return {
            'symbol': code,
            'asset_type': asset_type,
            'buy_fee_rate': raw['buy_fee_rate'],
            'buy_rate': raw['buy_fee_rate'],
            'sell_fee_rules': raw['sell_fee_rules'],
            'commission_rate': comm_rate,
            'stamp_tax_rate': stamp_tax,
            'management_fee': raw.get('management_fee', 0),
            'custody_fee': raw.get('custody_fee', 0),
            'summary': ' | '.join(summary_parts),
        }

    # ── Fund summary ──
    sell_rules_str = []
    for rule in raw.get('sell_fee_rules', []):
        d, r = rule['days'], rule['rate']
        if r > 0:
            sell_rules_str.append(f"持有<{d}天: {r*100:.2f}%")
        else:
            sell_rules_str.append(f"持有≥{d}天: 免费")
    return {
        'symbol': code,
        'asset_type': 'fund',
        'buy_fee_rate': raw['buy_fee_rate'],
        'buy_rate': raw['buy_fee_rate'],  # alias
        'sell_fee_rules': raw['sell_fee_rules'],
        'management_fee': raw['management_fee'],
        'custody_fee': raw['custody_fee'],
        'summary': (
            f"申购费率 {raw['buy_fee_rate']*100:.2f}% | "
            f"管理费 {raw['management_fee']*100:.1f}%/年 | "
            f"托管费 {raw['custody_fee']*100:.2f}%/年\n"
            f"赎回: {'; '.join(sell_rules_str)}"
        ),
    }


def calc_buy_fee(code, amount, *, client=None):
    """Calculate buy (subscription) fee for a given amount.
    Returns {fee_rate, fee_amount, net_amount}.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    info = fetch_trading_fee_info(code, client=client)
    rate = info['buy_fee_rate']
    fee = round(amount * rate, 2)
    return {
        'fee_rate': rate,
        'fee_amount': fee,
        'net_amount': round(amount - fee, 2),
    }


def calc_sell_fee(holding_dict, *, client=None):
    """Calculate sell (redemption) fee for an existing holding.
    holding_dict must have: symbol, shares, buy_price, buy_date.
    Returns {fee_rate, fee_amount, holding_days, gross_amount}.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    from datetime import datetime
    code = holding_dict.get('symbol', '')
    shares = float(holding_dict.get('shares', 0))
    buy_price = float(holding_dict.get('buy_price', 0))
    buy_date_str = holding_dict.get('buy_date', '')

    # Calculate holding days
    holding_days = 0
    if buy_date_str:
        try:
            buy_dt = datetime.strptime(buy_date_str[:10], '%Y-%m-%d')
            holding_days = (datetime.now() - buy_dt).days
        except (ValueError, TypeError):
            logger.debug('[FundInfo] Failed to parse buy_date %r for holding days', buy_date_str, exc_info=True)

    info = fetch_trading_fee_info(code, client=client)
    sell_rate = 0
    for rule in sorted(info.get('sell_fee_rules', []), key=lambda r: r['days']):
        if holding_days < rule['days']:
            sell_rate = rule['rate']
            break

    # Estimate current value using latest price or buy_price as fallback
    nav, _ = get_latest_price(code, client=client)
    current_nav = nav if nav else buy_price
    gross_amount = round(shares * current_nav, 2)
    fee = round(gross_amount * sell_rate, 2)

    return {
        'fee_rate': sell_rate,
        'fee_amount': fee,
        'holding_days': holding_days,
        'gross_amount': gross_amount,
        'net_amount': round(gross_amount - fee, 2),
    }
