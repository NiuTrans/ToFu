"""lib/search/vertical.py — Vertical domain search via free public APIs.

Detects structured identifiers in search queries (stock tickers, CVE IDs,
DOIs, arXiv IDs, package names, GitHub repos, IP addresses) and queries
specialized free APIs to provide structured data alongside regular web search.

All APIs used are free and require no API keys for basic usage.
"""

import re
import time

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['detect_vertical_intent', 'search_vertical']

_TIMEOUT = 10
_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'}


# ═══════════════════════════════════════════════════════
#  Ticker blocklist — common words that look like tickers
# ═══════════════════════════════════════════════════════

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
    'FIND', 'COME', 'TAKE', 'KNOW', 'MAKE', 'YEAR', 'TIME', 'LIFE',
    'WORK', 'GOOD', 'BEST', 'FREE', 'STOP', 'HOME', 'HELP', 'LOVE',
    'WHAT', 'THAT', 'THIS', 'WILL', 'FROM', 'THEM', 'THEN', 'THAN',
    'WITH', 'YOUR', 'BEEN', 'HAVE', 'WERE', 'DOES', 'THEY', 'SAID',
    'VERY', 'WHEN', 'ONLY', 'OVER', 'LIKE', 'INTO', 'MOST', 'MORE',
})


# ═══════════════════════════════════════════════════════
#  Intent Detection
# ═══════════════════════════════════════════════════════

def _detect_stock_ticker(query):
    """Detect stock ticker in query. Returns ticker string or None."""
    q = query.strip()
    # $AAPL
    m = re.match(r'^\$([A-Z]{1,5})$', q)
    if m:
        return m.group(1)
    # "stock AAPL", "AAPL stock", "AAPL price", etc.
    m = re.match(r'^(?:stock|ticker|price|quote|shares?)[:\s]+([A-Z]{1,5})$', q, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.match(r'^([A-Z]{1,5})\s+(?:stock|price|quote|chart|shares?)$', q, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Plain 2-5 uppercase chars, not in blocklist
    if re.match(r'^[A-Z]{2,5}$', q) and q not in _TICKER_BLOCKLIST:
        return q
    return None


def detect_vertical_intent(query):
    """Detect if a query matches a vertical domain pattern.

    Returns:
        (domain, identifier, params) tuple, or None.
    """
    q = query.strip()
    if not q or len(q) > 200:
        return None

    # CVE (highest priority — very specific pattern)
    m = re.search(r'(CVE-\d{4}-\d{4,7})', q, re.IGNORECASE)
    if m:
        return ('cve', m.group(1).upper(), {})

    # DOI
    m = re.search(r'(?:^|doi[:\s]+)(10\.\d{4,}/\S+)', q, re.IGNORECASE)
    if m:
        doi = m.group(1).rstrip('.,;:)>]\'"')
        return ('doi', doi, {})

    # arXiv
    m = re.search(r'(?:^|arxiv[:\s]+)(\d{4}\.\d{4,5}(?:v\d+)?)', q, re.IGNORECASE)
    if m:
        return ('arxiv', m.group(1), {})

    # PyPI (explicit: "pypi X" or "pip install X")
    m = re.match(r'^(?:pypi[:\s]+|pip\s+install\s+)([a-zA-Z][a-zA-Z0-9._-]*)', q, re.IGNORECASE)
    if m:
        return ('pypi', m.group(1).lower(), {})

    # npm (explicit: "npm X", "npm:X", "npm install X", "npx X")
    m = re.match(r'^(?:npm[:\s]+(?:install\s+|i\s+|info\s+)?|npx\s+)([a-zA-Z@][a-zA-Z0-9._@/-]*)', q, re.IGNORECASE)
    if m:
        return ('npm', m.group(1), {})

    # GitHub repo (explicit prefix: "github:user/repo" or "gh:user/repo")
    m = re.match(r'^(?:github[:\s]+|gh[:\s]+)([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)', q, re.IGNORECASE)
    if m:
        return ('github', m.group(1), {})

    # Bare owner/repo (conservative: must look like a repo, not a file path)
    m = re.match(r'^([a-zA-Z][a-zA-Z0-9_-]+)/([a-zA-Z][a-zA-Z0-9_.-]+)$', q)
    if m:
        repo_part = m.group(2)
        _FILE_EXTS = ('.py', '.js', '.ts', '.go', '.rs', '.rb', '.java', '.c', '.h',
                      '.txt', '.md', '.yml', '.yaml', '.toml', '.cfg', '.sh', '.css', '.html')
        if not any(repo_part.endswith(ext) for ext in _FILE_EXTS):
            return ('github', q, {})

    # IP address
    m = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$', q)
    if m:
        octets = q.split('.')
        if all(0 <= int(o) <= 255 for o in octets):
            return ('ip', q, {})

    # Stock ticker (most conservative — last in priority)
    ticker = _detect_stock_ticker(q)
    if ticker:
        return ('stock', ticker, {})

    return None


# ═══════════════════════════════════════════════════════
#  Vertical Search Handlers
# ═══════════════════════════════════════════════════════

def _search_cve(identifier, params):
    """Query NVD (NIST) for CVE details."""
    try:
        resp = requests.get(
            'https://services.nvd.nist.gov/rest/json/cves/2.0',
            params={'cveId': identifier},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.warning('[Vertical] NVD returned %d for %s', resp.status_code, identifier)
            return None
        data = resp.json()
        vulns = data.get('vulnerabilities', [])
        if not vulns:
            return None

        cve = vulns[0].get('cve', {})
        desc_list = cve.get('descriptions', [])
        desc = next((d['value'] for d in desc_list if d.get('lang') == 'en'), '')

        cvss_score, severity = '', ''
        for vk in ('cvssMetricV31', 'cvssMetricV30', 'cvssMetricV2'):
            metrics_list = cve.get('metrics', {}).get(vk, [])
            if metrics_list:
                cd = metrics_list[0].get('cvssData', {})
                cvss_score = str(cd.get('baseScore', ''))
                severity = cd.get('baseSeverity', '')
                break

        refs = [r['url'] for r in cve.get('references', [])[:5]]
        published = cve.get('published', '')[:10]
        modified = cve.get('lastModified', '')[:10]

        parts = [f'## {identifier}']
        if severity and cvss_score:
            parts.append(f'**CVSS Score**: {cvss_score} ({severity})')
        if published:
            parts.append(f'**Published**: {published}  |  **Modified**: {modified}')
        parts.append(f'\n**Description**: {desc}')
        if refs:
            parts.append('\n**References**:\n' + '\n'.join(f'- {u}' for u in refs))

        return {'domain': 'security', 'type': 'cve', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'NVD (NIST)'}
    except Exception as e:
        logger.warning('[Vertical] CVE lookup failed for %s: %s', identifier, e)
        return None


def _search_arxiv(identifier, params):
    """Query arXiv API for paper details."""
    try:
        import xml.etree.ElementTree as ET
        resp = requests.get(
            'http://export.arxiv.org/api/query',
            params={'id_list': identifier, 'max_results': '1'},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            return None

        root = ET.fromstring(resp.text)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        entry = root.find('a:entry', ns)
        if entry is None:
            return None

        title = (entry.findtext('a:title', '', ns) or '').strip().replace('\n', ' ')
        summary = (entry.findtext('a:summary', '', ns) or '').strip()
        published = (entry.findtext('a:published', '', ns) or '')[:10]
        authors = [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)]
        pdf_links = [l.get('href') for l in entry.findall('a:link', ns)
                     if l.get('type') == 'application/pdf']
        categories = [c.get('term') for c in entry.findall('a:category', ns) if c.get('term')]

        parts = [f'## {title}', f'**arXiv**: {identifier}']
        if published:
            parts.append(f'**Published**: {published}')
        if authors:
            parts.append(f'**Authors**: {", ".join(authors[:10])}')
        if categories:
            parts.append(f'**Categories**: {", ".join(categories[:5])}')
        if pdf_links:
            parts.append(f'**PDF**: {pdf_links[0]}')
        parts.append(f'\n**Abstract**: {summary}')

        return {'domain': 'academic', 'type': 'arxiv', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'arXiv'}
    except Exception as e:
        logger.warning('[Vertical] arXiv lookup failed for %s: %s', identifier, e)
        return None


def _search_doi(identifier, params):
    """Look up a DOI via CrossRef."""
    try:
        resp = requests.get(
            f'https://api.crossref.org/works/{identifier}',
            headers={**_HEADERS, 'Accept': 'application/json'},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return None
        msg = resp.json().get('message', {})

        title = (msg.get('title', [''])[0] or identifier)
        authors = []
        for a in msg.get('author', [])[:10]:
            name = f"{a.get('given', '')} {a.get('family', '')}".strip()
            if name:
                authors.append(name)

        container = (msg.get('container-title', ['']) or [''])[0]
        pub_parts = (msg.get('published-print') or msg.get('published-online') or {}).get('date-parts', [[]])
        published = '-'.join(str(x) for x in pub_parts[0]) if pub_parts and pub_parts[0] else ''
        abstract = re.sub(r'<[^>]+>', '', msg.get('abstract', '')).strip()
        url = msg.get('URL', f'https://doi.org/{identifier}')

        parts = [f'## {title}', f'**DOI**: {identifier}']
        if authors:
            parts.append(f'**Authors**: {", ".join(authors)}')
        if container:
            parts.append(f'**Journal**: {container}')
        if published:
            parts.append(f'**Published**: {published}')
        parts.append(f'**URL**: {url}')
        if abstract:
            parts.append(f'\n**Abstract**: {abstract[:1500]}')

        return {'domain': 'academic', 'type': 'doi', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'CrossRef'}
    except Exception as e:
        logger.warning('[Vertical] DOI lookup failed for %s: %s', identifier, e)
        return None


def _search_pypi(identifier, params):
    """Look up a Python package on PyPI."""
    try:
        resp = requests.get(f'https://pypi.org/pypi/{identifier}/json',
                            headers=_HEADERS, timeout=_TIMEOUT)
        if not resp.ok:
            return None
        info = resp.json().get('info', {})

        name = info.get('name', identifier)
        version = info.get('version', '')
        summary = info.get('summary', '')
        author = info.get('author', '') or info.get('author_email', '')
        license_ = info.get('license', '')
        requires_python = info.get('requires_python', '')
        project_urls = info.get('project_urls') or {}
        description = info.get('description', '')

        parts = [f'## {name} {version}']
        if summary:
            parts.append(f'**Summary**: {summary}')
        if author:
            parts.append(f'**Author**: {author}')
        if license_:
            parts.append(f'**License**: {license_[:100]}')
        if requires_python:
            parts.append(f'**Python**: {requires_python}')
        parts.append(f'**PyPI**: https://pypi.org/project/{name}/')
        for label, url in list(project_urls.items())[:4]:
            parts.append(f'**{label}**: {url}')
        if description:
            parts.append(f'\n{description[:2000]}')

        return {'domain': 'code', 'type': 'pypi', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'PyPI'}
    except Exception as e:
        logger.warning('[Vertical] PyPI lookup failed for %s: %s', identifier, e)
        return None


def _search_npm(identifier, params):
    """Look up an npm package on the registry."""
    try:
        resp = requests.get(f'https://registry.npmjs.org/{identifier}',
                            headers=_HEADERS, timeout=8)
        if not resp.ok:
            return None
        data = resp.json()

        name = data.get('name', identifier)
        description = data.get('description', '')
        latest_ver = (data.get('dist-tags') or {}).get('latest', '')
        license_ = data.get('license', '')
        if isinstance(license_, dict):
            license_ = license_.get('type', '')
        homepage = data.get('homepage', '')
        repo = data.get('repository', {})
        repo_url = repo.get('url', '') if isinstance(repo, dict) else str(repo)
        readme = data.get('readme', '')

        maintainers = data.get('maintainers', [])
        maint_names = [m.get('name', '') for m in maintainers[:5] if isinstance(m, dict)]

        parts = [f'## {name} {latest_ver}']
        if description:
            parts.append(f'**Description**: {description}')
        if license_:
            parts.append(f'**License**: {license_}')
        if maint_names:
            parts.append(f'**Maintainers**: {", ".join(maint_names)}')
        parts.append(f'**npm**: https://www.npmjs.com/package/{name}')
        if homepage:
            parts.append(f'**Homepage**: {homepage}')
        if repo_url:
            clean = repo_url.replace('git+', '').replace('git://', 'https://').rstrip('.git')
            parts.append(f'**Repository**: {clean}')
        if readme and readme != 'ERROR: No README data found!':
            parts.append(f'\n{readme[:2000]}')

        return {'domain': 'code', 'type': 'npm', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'npm'}
    except Exception as e:
        logger.warning('[Vertical] npm lookup failed for %s: %s', identifier, e)
        return None


def _search_github(identifier, params):
    """Look up a GitHub repository."""
    try:
        resp = requests.get(
            f'https://api.github.com/repos/{identifier}',
            headers={**_HEADERS, 'Accept': 'application/vnd.github.v3+json'},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return None
        d = resp.json()

        full_name = d.get('full_name', identifier)
        description = d.get('description', '')
        stars = d.get('stargazers_count', 0)
        forks = d.get('forks_count', 0)
        language = d.get('language', '')
        lic = (d.get('license') or {})
        license_name = lic.get('spdx_id', '') or lic.get('name', '') if isinstance(lic, dict) else ''
        topics = d.get('topics', [])
        created = d.get('created_at', '')[:10]
        updated = d.get('updated_at', '')[:10]
        html_url = d.get('html_url', f'https://github.com/{identifier}')
        open_issues = d.get('open_issues_count', 0)

        parts = [f'## {full_name}']
        if description:
            parts.append(f'**Description**: {description}')
        parts.append(f'**Stars**: {stars:,}  |  **Forks**: {forks:,}  |  **Issues**: {open_issues:,}')
        if language:
            parts.append(f'**Language**: {language}')
        if license_name:
            parts.append(f'**License**: {license_name}')
        if topics:
            parts.append(f'**Topics**: {", ".join(topics[:10])}')
        parts.append(f'**Created**: {created}  |  **Updated**: {updated}')
        parts.append(f'**URL**: {html_url}')

        # Fetch README (separate request, best-effort)
        try:
            import base64
            r2 = requests.get(
                f'https://api.github.com/repos/{identifier}/readme',
                headers={**_HEADERS, 'Accept': 'application/vnd.github.v3+json'},
                timeout=8,
            )
            if r2.ok:
                content = base64.b64decode(r2.json().get('content', '')).decode('utf-8', errors='replace')
                if content:
                    parts.append(f'\n--- README ---\n{content[:2500]}')
        except (requests.RequestException, ValueError) as e:
            # ValueError covers JSON decode + base64 decode failures.
            logger.debug('[Vertical] GitHub README fetch skipped for %s: %s',
                         identifier, e)

        return {'domain': 'code', 'type': 'github', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'GitHub'}
    except Exception as e:
        logger.warning('[Vertical] GitHub lookup failed for %s: %s', identifier, e)
        return None


def _search_stock_fallback(identifier):
    """Fallback stock lookup via simple web scrape of basic quote info."""
    try:
        resp = requests.get(
            f'https://www.google.com/finance/quote/{identifier}:NASDAQ',
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            resp = requests.get(
                f'https://www.google.com/finance/quote/{identifier}:NYSE',
                headers=_HEADERS, timeout=_TIMEOUT,
            )
        if not resp.ok:
            return None

        import re as _re
        html = resp.text
        # Extract price from data-last-price attribute
        price_m = _re.search(r'data-last-price="([\d.]+)"', html)
        change_m = _re.search(r'data-last-normal-market-change="([^"]+)"', html)
        pct_m = _re.search(r'data-last-normal-market-change-percent="([^"]+)"', html)

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
        return {'domain': 'finance', 'type': 'stock', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'Google Finance'}
    except Exception as e:
        logger.debug('[Vertical] Stock fallback failed for %s: %s', identifier, e)
        return None


def _search_stock(identifier, params):
    """Look up stock data via Yahoo Finance (with fallback)."""
    try:
        resp = requests.get(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{identifier}',
            params={'range': '5d', 'interval': '1d', 'includePrePost': 'false'},
            headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.debug('[Vertical] Yahoo Finance HTTP %d for %s, trying fallback',
                         resp.status_code, identifier)
            return _search_stock_fallback(identifier)
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
            from datetime import datetime, timezone
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

        return {'domain': 'finance', 'type': 'stock', 'identifier': symbol,
                'content': '\n'.join(parts), 'source': 'Yahoo Finance'}
    except Exception as e:
        logger.warning('[Vertical] Stock lookup failed for %s: %s', identifier, e)
        return None


def _search_ip(identifier, params):
    """Look up IP address information via ipinfo.io."""
    try:
        resp = requests.get(f'https://ipinfo.io/{identifier}/json',
                            headers=_HEADERS, timeout=_TIMEOUT)
        if not resp.ok:
            return None
        d = resp.json()

        parts = [f'## IP: {identifier}']
        if d.get('hostname'):
            parts.append(f'**Hostname**: {d["hostname"]}')
        loc = [p for p in [d.get('city'), d.get('region'), d.get('country')] if p]
        if loc:
            parts.append(f'**Location**: {", ".join(loc)}')
        if d.get('loc'):
            parts.append(f'**Coordinates**: {d["loc"]}')
        if d.get('org'):
            parts.append(f'**Organization**: {d["org"]}')
        if d.get('timezone'):
            parts.append(f'**Timezone**: {d["timezone"]}')

        return {'domain': 'network', 'type': 'ip', 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'ipinfo.io'}
    except Exception as e:
        logger.warning('[Vertical] IP lookup failed for %s: %s', identifier, e)
        return None


# ═══════════════════════════════════════════════════════
#  Dispatch
# ═══════════════════════════════════════════════════════

_VERTICAL_HANDLERS = {
    'cve': _search_cve,
    'arxiv': _search_arxiv,
    'doi': _search_doi,
    'pypi': _search_pypi,
    'npm': _search_npm,
    'github': _search_github,
    'stock': _search_stock,
    'ip': _search_ip,
}


def search_vertical(domain, identifier, params=None):
    """Execute a vertical domain search.

    Returns:
        Dict with keys (domain, type, identifier, content, source) or None.
    """
    handler = _VERTICAL_HANDLERS.get(domain)
    if not handler:
        logger.warning('[Vertical] Unknown domain: %s', domain)
        return None

    t0 = time.time()
    result = handler(identifier, params or {})
    elapsed = time.time() - t0

    if result:
        logger.info('[Vertical] %s/%s OK in %.1fs (%d chars)',
                    domain, identifier, elapsed, len(result.get('content', '')))
    else:
        logger.info('[Vertical] %s/%s no data in %.1fs', domain, identifier, elapsed)
    return result
