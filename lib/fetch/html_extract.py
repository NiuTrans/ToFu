"""lib/fetch/html_extract.py — HTML content & metadata extraction.

Contains HTML text extraction (trafilatura + BS4 fallback), link extraction,
publication date extraction from meta tags / JSON-LD / <time> elements,
and specialized code-hosting blob extraction (GitHub / GitLab / Bitbucket).
"""

import json
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import trafilatura

from lib.fetch.utils import _get_bs4
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'extract_html_publish_date',
    'extract_links_from_soup',
    'extract_html_text',
]


# ═══════════════════════════════════════════════════════
#  HTML 发布日期提取 (meta / JSON-LD / <time>)
# ═══════════════════════════════════════════════════════

def extract_html_publish_date(html):
    """Extract publication date from HTML meta tags, JSON-LD, or <time> elements.

    Checks (in priority order):
      1. <meta property="article:published_time">
      2. <meta name="pubdate|publishdate|publish_date|date|DC.date">
      3. JSON-LD "datePublished" / "dateCreated"
      4. <time datetime="..." pubdate> or first <time datetime>
    Returns ISO string 'YYYY-MM-DD' (day-level) or '' if not found.
    """
    if not html:
        return ''

    from dateutil import parser as dateutil_parser

    def _try_parse(raw):
        """Try to parse a date string into a day-level 'YYYY-MM-DD' format."""
        if not raw or not isinstance(raw, str):
            return ''
        raw = raw.strip()
        if not raw:
            return ''
        try:
            dt = dateutil_parser.parse(raw, fuzzy=True)
            # Normalize to naive (strip timezone for comparison)
            if dt.tzinfo is not None:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            # Sanity check: reject dates in the far future or before 2000
            from datetime import datetime as _dt
            if dt.year < 2000 or dt > _dt.now() + timedelta(days=2):
                return ''
            return dt.strftime('%Y-%m-%d')
        except (ValueError, OverflowError):
            logger.debug('[Fetch] Date parse failed for raw=%r', raw, exc_info=True)
            return ''

    try:
        soup = _get_bs4()(html[:50000], 'html.parser')  # limit parsing scope
    except Exception as e:
        logger.debug('[Fetch] BS4 parse failed for publish date extraction: %s', e, exc_info=True)
        return ''

    # ── 1. <meta> OG / standard meta tags ──
    meta_props = [
        'article:published_time', 'article:published',
        'og:article:published_time', 'og:published_time',
    ]
    for prop in meta_props:
        tag = soup.find('meta', attrs={'property': prop})
        if tag and tag.get('content'):
            r = _try_parse(tag['content'])
            if r:
                return r

    meta_names = [
        'pubdate', 'publishdate', 'publish_date', 'date',
        'DC.date', 'DC.date.issued', 'sailthru.date',
        'article.published', 'published_time', 'creation_date',
    ]
    for name in meta_names:
        tag = soup.find('meta', attrs={'name': re.compile(f'^{re.escape(name)}$', re.I)})
        if tag and tag.get('content'):
            r = _try_parse(tag['content'])
            if r:
                return r

    # ── 2. JSON-LD (schema.org) ──
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            raw_ld = (script.string or '').strip()
            if not raw_ld:
                continue
            ld = json.loads(raw_ld)
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ('datePublished', 'dateCreated', 'uploadDate'):
                    val = item.get(key)
                    if val:
                        r = _try_parse(val)
                        if r:
                            return r
                # Check nested @graph
                for g in (item.get('@graph') or []):
                    if isinstance(g, dict):
                        for key in ('datePublished', 'dateCreated'):
                            val = g.get(key)
                            if val:
                                r = _try_parse(val)
                                if r:
                                    return r
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.debug('[Fetch] JSON-LD parse error in publish date extraction', exc_info=True)
            continue

    # ── 3. <time> elements ──
    for time_tag in soup.find_all('time', attrs={'datetime': True}):
        r = _try_parse(time_tag['datetime'])
        if r:
            return r

    return ''


# ═══════════════════════════════════════════════════════
#  Link extraction
# ═══════════════════════════════════════════════════════

def extract_links_from_soup(soup, base_url=''):
    """Extract meaningful links from a parsed soup element, returning a markdown-style link section."""
    links = []
    seen_urls = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        # Resolve relative URLs
        if base_url and not href.startswith(('http://', 'https://')):
            href = urljoin(base_url, href)
        if href in seen_urls:
            continue
        seen_urls.add(href)
        text = a.get_text(strip=True)
        if not text or len(text) < 2:
            continue
        # Skip trivial navigation links
        if text.lower() in ('home', 'back', 'top', 'skip', '↑', '←', '→', '↓', '#'):
            continue
        links.append(f'- [{text}]({href})')
    return links


# ═══════════════════════════════════════════════════════
#  Code-hosting blob extraction (GitHub / GitLab / Bitbucket)
# ═══════════════════════════════════════════════════════

# Regex: GitHub blob URLs  /owner/repo/blob/ref/path.ext
_GITHUB_BLOB_URL_RE = re.compile(
    r'^https?://github\.com/[^/]+/[^/]+/blob/.+\.[a-zA-Z0-9]+$'
)

# GitHub embeds source code as JSON inside a <script type="application/json"
# data-target="react-app.embeddedData"> block, with a "rawLines" array.
_GITHUB_EMBEDDED_RE = re.compile(
    r'<script\s+type="application/json"\s+'
    r'data-target="react-app\.embeddedData"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _find_nested_key(obj, key, depth=0):
    """Recursively search a JSON object for a key, returning its value."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_nested_key(v, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_nested_key(v, key, depth + 1)
            if r is not None:
                return r
    return None


def _try_extract_github_blob(html_text, url):
    """Extract source code from a GitHub blob page's embedded JSON payload.

    GitHub renders code files via React (JavaScript), so a plain HTTP GET
    on a /blob/ URL returns an HTML shell with no visible source code.
    However, the raw code is embedded as a JSON array ``rawLines`` inside a
    ``<script type="application/json" data-target="react-app.embeddedData">``
    block.  This function extracts those lines directly from the HTML,
    avoiding the need for URL rewriting or JavaScript rendering.

    Returns:
        Formatted source code string, or None if extraction fails.
    """
    m = _GITHUB_EMBEDDED_RE.search(html_text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Fetch] GitHub embeddedData JSON parse failed: %s', e)
        return None

    raw_lines = _find_nested_key(payload, 'rawLines')
    if not raw_lines or not isinstance(raw_lines, list):
        return None

    code = '\n'.join(raw_lines)
    if len(code) < 10:
        return None

    # Extract metadata for a nice header
    file_path = _find_nested_key(payload, 'path') or ''
    language = _find_nested_key(payload, 'language') or ''

    # Build formatted output
    parts = []
    if file_path:
        header = f'File: {file_path}'
        if language:
            header += f'  ({language})'
        header += f'  — {len(raw_lines)} lines'
        parts.append(header)
        parts.append('')
    parts.append(code)

    logger.debug('[Fetch] GitHub blob extracted: %s — %d lines, %d chars',
                 file_path or url[:80], len(raw_lines), len(code))
    return '\n'.join(parts)


def _try_extract_code_blob(html_text, url):
    """Try to extract source code from a code-hosting page (GitHub, GitLab, Bitbucket).

    Dispatches to platform-specific extractors based on the URL.
    Returns the extracted source code string, or None if not a code page
    or extraction failed (caller should fall through to normal extraction).
    """
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception as e:
        logger.debug('[HTMLExtract] URL parse failed for %.80s: %s', url, e)
        return None

    # ── GitHub blob pages ──
    if host == 'github.com' and _GITHUB_BLOB_URL_RE.match(url):
        return _try_extract_github_blob(html_text, url)

    # GitLab and Bitbucket use full JS rendering (no embedded code in HTML),
    # so they fall through to the URL-rewrite approach in _normalize_code_hosting_url().
    return None


# ═══════════════════════════════════════════════════════
#  HTML text extraction
# ═══════════════════════════════════════════════════════

def extract_html_text(html, max_chars, url=''):
    """Extract readable text from HTML. Preserves links as a markdown section at the end."""

    # ── Phase 0: Code-hosting blob extraction (GitHub embeds code in JSON) ──
    code = _try_extract_code_blob(html, url)
    if code:
        if max_chars and len(code) > max_chars:
            code = code[:max_chars] + '\n[…truncated]'
        return code

    # ── Phase 1: Try trafilatura for main text extraction ──
    main_text = None
    try:
        main_text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                        no_fallback=False, favor_recall=True, deduplicate=True,
                                        include_links=True)  # ★ include_links!
    except Exception as e:
        logger.warning('Trafilatura failed: %s', e, exc_info=True)

    # ── Phase 2: BeautifulSoup fallback / link extraction ──
    try:
        soup = _get_bs4()(html, 'html.parser')
        for tag in ['script','style','nav','header','footer','aside','iframe',
                     'noscript','form','svg','button','input','select','textarea']:
            for el in soup.find_all(tag): el.decompose()
        for el in soup.find_all(attrs={'style': re.compile(r'display\s*:\s*none', re.I)}):
            el.decompose()
        for el in soup.find_all(attrs={'hidden': True}): el.decompose()
        for el in soup.find_all(class_=re.compile(
                r'\b(cookie|popup|modal|banner|advert|sidebar|menu|nav)\b', re.I)):
            el.decompose()
        # ★ Priority: <main> before <article> — <article> can be a single card/widget
        #   while <main> wraps the full page content (e.g. HuggingFace collections)
        main_el = None
        for c in [soup.find('main'), soup.find(role='main'),
                   soup.find(id=re.compile(r'^(content|main|article)', re.I)),
                   soup.find(class_=re.compile(r'^(post|article|content|main)', re.I)),
                   soup.find('article')]:
            if c and len(c.get_text(strip=True)) > 50: main_el = c; break

        content_el = main_el or soup.body or soup

        # ── Extract links: prioritize content area, then rest of body ──
        body_el = soup.body or soup
        content_links = extract_links_from_soup(content_el, base_url=url)
        content_urls = {l.split('](')[-1].rstrip(')') for l in content_links if '](' in l}
        # Get body links that aren't already in content_links
        body_links = extract_links_from_soup(body_el, base_url=url)
        extra_links = [l for l in body_links
                       if l.split('](')[-1].rstrip(')') not in content_urls]
        links = content_links + extra_links

        if not main_text:
            # Trafilatura failed, use BS fallback
            text = content_el.get_text(separator='\n', strip=True)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            main_text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))
            if not main_text or len(main_text) < 30:
                return None
    except Exception as e:
        logger.warning('BS4 extraction failed: %s', e, exc_info=True)
        if main_text:
            links = []
        else:
            return None

    # ── Phase 3: Append link section (if we have links and main text) ──
    if links and main_text:
        # Relevance scoring: rank links by how related they are to the main text
        text_lower = main_text[:5000].lower() if main_text else ''
        if text_lower:
            def _link_score(link_md):
                score = 0
                # Extract URL and text from "- [text](url)"
                parts = link_md.split('](')
                if len(parts) != 2:
                    return 0
                text_part = parts[0].lstrip('- [')
                url_part = parts[1].rstrip(')')
                # Score based on URL path keywords appearing in main text
                path = url_part.split('/')[-1].replace('-', ' ').replace('_', ' ').lower()
                for word in path.split():
                    if len(word) > 3 and word in text_lower:
                        score += 3
                # Link text that appears in main_text
                for word in text_part.lower().split():
                    if len(word) > 3 and word in text_lower:
                        score += 1
                return score
            scored = [(l, _link_score(l)) for l in links]
            scored.sort(key=lambda x: -x[1])
            selected = [l for l, s in scored[:80]]
        else:
            selected = links[:80]
        link_section = '\n\n--- Page Links ---\n' + '\n'.join(selected)
        combined = main_text + link_section
    else:
        combined = main_text

    if max_chars and len(combined) > max_chars:
        combined = combined[:max_chars] + '\n[…truncated]'
    return combined


# Backward-compatible aliases (these were originally _-prefixed private names)
_extract_links_from_soup = extract_links_from_soup
_extract_html_text = extract_html_text
