"""Stock vertical — Yahoo Finance (with Google Finance fallback)."""

import re
from datetime import datetime, timezone

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _HEADERS, _TIMEOUT, logger

TYPE = 'stock'
DOMAIN = 'finance'

# Common words that look like tickers but almost never are (acronyms, English
# stopwords). A bare uppercase token in this set is NOT routed to the stock
# vertical; an explicit cue ($AAPL / "AAPL stock") still wins.
_TICKER_BLOCKLIST = frozenset({
    'AM', 'AN', 'AS', 'AT', 'BE', 'BY', 'DO', 'GO', 'IF', 'IN',
    'IS', 'IT', 'ME', 'MY', 'NO', 'OF', 'OK', 'ON', 'OR', 'SO',
    'TO', 'UP', 'US', 'WE', 'ALL', 'AND', 'ANY', 'ARE', 'ASK',
    'BAD', 'BIG', 'BUT', 'BUY', 'CAN', 'DID', 'END', 'FAR', 'FEW',
    'FOR', 'GET', 'GOD', 'GOT', 'HAD', 'HAS', 'HER', 'HIM', 'HIS',
    'HOT', 'HOW', 'ITS', 'LET', 'MAY', 'MEN', 'NEW', 'NOR', 'NOT',
    'NOW', 'OFF', 'OLD', 'ONE', 'OUR', 'OUT', 'OWN', 'PUT', 'RAN',
    'RUN', 'SAD', 'SAY', 'SET', 'SHE', 'THE', 'TOO', 'TRY', 'TWO',
    'USE', 'WAY', 'WHO', 'WHY', 'WON', 'YET', 'YOU',
    'API', 'APP', 'CPU', 'CSS', 'DNS', 'GPU', 'GUI', 'URL', 'USB',
    'HTML', 'HTTP', 'HTTPS', 'JSON', 'REST', 'SQL', 'SSH', 'SSL', 'TCP', 'TLS',
    'NULL', 'VOID', 'YAML', 'TODO', 'WIFI', 'RGBA', 'SMTP', 'IMAP',
    'CRUD', 'GREP', 'BASH', 'CURL', 'WGET', 'MAKE', 'DIFF', 'EXEC',
    'EVAL', 'ENUM', 'BOOL', 'CHAR', 'SELF', 'INIT', 'MAIN', 'TEST',
    'SPEC', 'MOCK', 'STUB', 'SEED', 'HASH', 'TREE', 'NODE', 'LIST',
    'PUSH', 'PULL', 'READ', 'SEND', 'LOAD', 'SAVE', 'COPY', 'SORT',
    'EDIT', 'FILE', 'CODE', 'TEXT', 'PAGE', 'LINK', 'PATH', 'PIPE',
    'LOCK', 'LOOP', 'REPO', 'TASK', 'ITEM', 'MENU', 'CHAT', 'MAIL',
    'TOOL', 'RATE', 'SIZE', 'DATA', 'TYPE', 'ROLE', 'GAME', 'SITE',
    'RISK', 'BILL', 'DEAL', 'LOSS', 'CARE', 'WALL', 'PLAN', 'TEAM',
    'TERM', 'FOOD', 'HALF', 'CITY', 'ROAD', 'TRUE', 'WORD', 'BODY',
    'AREA', 'BOOK', 'HEAD', 'IDEA', 'LINE', 'FORM', 'FACE', 'CASE',
    'NAME', 'FACT', 'SIDE', 'PART', 'HIGH', 'HAND', 'MANY', 'SOME',
    'EVEN', 'ALSO', 'BACK', 'LONG', 'LAST', 'MUCH', 'JUST', 'NEXT',
    'EACH', 'SAME', 'DONE', 'PLAY', 'NEED', 'SHOW', 'SEEM', 'WANT',
    'LIVE', 'MOVE', 'TURN', 'KEEP', 'LOOK', 'CALL', 'TELL', 'GIVE',
    'FIND', 'COME', 'TAKE', 'KNOW', 'YEAR', 'TIME', 'LIFE',
    'WORK', 'GOOD', 'BEST', 'FREE', 'STOP', 'HOME', 'HELP', 'LOVE',
    'WHAT', 'THAT', 'THIS', 'WILL', 'FROM', 'THEM', 'THEN', 'THAN',
    'WITH', 'YOUR', 'BEEN', 'HAVE', 'WERE', 'DOES', 'THEY', 'SAID',
    'VERY', 'WHEN', 'ONLY', 'OVER', 'LIKE', 'INTO', 'MOST', 'MORE',
})


def detect(q):
    """Detect a stock ticker. Returns (TYPE, ticker, {}) or None.

    Most conservative vertical — runs last. A bare uppercase token is only
    treated as a ticker when it is NOT in the blocklist; explicit cues
    ($AAPL / 'stock AAPL' / 'AAPL price') always win.
    """
    # $AAPL
    m = re.match(r'^\$([A-Z]{1,5})$', q)
    if m:
        return (TYPE, m.group(1), {})
    # "stock AAPL", "ticker AAPL", "price AAPL", ...
    m = re.match(r'^(?:stock|ticker|price|quote|shares?)[:\s]+([A-Z]{1,5})$', q, re.IGNORECASE)
    if m:
        return (TYPE, m.group(1).upper(), {})
    m = re.match(r'^([A-Z]{1,5})\s+(?:stock|price|quote|chart|shares?)$', q, re.IGNORECASE)
    if m:
        return (TYPE, m.group(1).upper(), {})
    # Plain 2-5 uppercase chars, not in blocklist.
    if re.match(r'^[A-Z]{2,5}$', q) and q not in _TICKER_BLOCKLIST:
        return (TYPE, q, {})
    return None


def _search_fallback(identifier):
    """Fallback stock lookup via a simple scrape of Google Finance quote info."""
    try:
        resp = base.http_get(
            f'https://www.google.com/finance/quote/{identifier}:NASDAQ',
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            resp = base.http_get(
                f'https://www.google.com/finance/quote/{identifier}:NYSE',
                headers=_HEADERS, timeout=_TIMEOUT,
            )
        if not resp.ok:
            return None

        html = resp.text
        price_m = re.search(r'data-last-price="([\d.]+)"', html)
        change_m = re.search(r'data-last-normal-market-change="([^"]+)"', html)
        pct_m = re.search(r'data-last-normal-market-change-percent="([^"]+)"', html)

        if not price_m:
            return None

        price = float(price_m.group(1))
        parts = [f'## {identifier}', f'**Price**: ${price:.2f}']
        if change_m and pct_m:
            change = float(change_m.group(1))
            pct = float(pct_m.group(1))
            arrow = '📈' if change >= 0 else '📉'
            parts.append(f'**Change**: {arrow} {change:+.2f} ({pct:+.2f}%)')

        parts.append('\n_Data from Google Finance (limited — Yahoo rate-limited)_')
        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'Google Finance'}
    except Exception as e:
        logger.debug('[Vertical] Stock fallback failed for %s: %s', identifier, e)
        return None


def search(identifier, params):
    """Look up stock data via Yahoo Finance (with Google Finance fallback)."""
    try:
        resp = base.http_get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{identifier}',
            params={'range': '5d', 'interval': '1d', 'includePrePost': 'false'},
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.debug('[Vertical] Yahoo Finance HTTP %d for %s, trying fallback',
                         resp.status_code, identifier)
            return _search_fallback(identifier)
        chart_result = (resp.json().get('chart') or {}).get('result', [])
        if not chart_result:
            return None

        meta = chart_result[0].get('meta', {})
        symbol = meta.get('symbol', identifier)
        currency = meta.get('currency', 'USD')
        exchange = meta.get('exchangeName', '')
        full_name = meta.get('longName') or meta.get('shortName') or symbol
        price = meta.get('regularMarketPrice', 0)
        prev_close = meta.get('previousClose') or meta.get('chartPreviousClose', 0)

        if not price:
            return None

        parts = [f'## {full_name} ({symbol})',
                 f'**Exchange**: {exchange}  |  **Currency**: {currency}',
                 f'**Price**: {price:.2f} {currency}']

        if prev_close:
            change = price - prev_close
            pct = (change / prev_close * 100)
            arrow = '📈' if change >= 0 else '📉'
            parts.append(f'**Change**: {arrow} {change:+.2f} ({pct:+.2f}%)')
            parts.append(f'**Previous Close**: {prev_close:.2f}')

        # 5-day price history
        indicators = chart_result[0].get('indicators', {})
        quotes = (indicators.get('quote') or [{}])[0]
        timestamps = chart_result[0].get('timestamp', [])
        closes = quotes.get('close', [])

        if timestamps and closes:
            parts.append('\n**Recent Prices** (5 trading days):')
            for i, ts in enumerate(timestamps):
                if i < len(closes) and closes[i] is not None:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
                    o = (quotes.get('open') or [])[i] if i < len(quotes.get('open') or []) else None
                    h = (quotes.get('high') or [])[i] if i < len(quotes.get('high') or []) else None
                    lo = (quotes.get('low') or [])[i] if i < len(quotes.get('low') or []) else None
                    v = (quotes.get('volume') or [])[i] if i < len(quotes.get('volume') or []) else None
                    line = f'  {dt}: Close={closes[i]:.2f}'
                    if o is not None:
                        line += f'  Open={o:.2f}'
                    if h is not None and lo is not None:
                        line += f'  H/L={h:.2f}/{lo:.2f}'
                    if v is not None:
                        line += f'  Vol={v:,.0f}'
                    parts.append(line)

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': symbol,
                'content': '\n'.join(parts), 'source': 'Yahoo Finance'}
    except Exception as e:
        logger.warning('[Vertical] Stock lookup failed for %s: %s', identifier, e)
        return None
