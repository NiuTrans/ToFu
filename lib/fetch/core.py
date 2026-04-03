"""lib/fetch/core.py — Main public API: fetch_page_content, batch fetching, URL utilities.

This module orchestrates the full fetch pipeline:
  cache check → HTTP request → SSL retry → Playwright fallback → browser extension fallback.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)
from lib.fetch.html_extract import (
    extract_html_publish_date,
)
from lib.fetch.html_extract import (
    extract_html_text as _extract_html_text,
)
from lib.fetch.http import (
    HttpError as _HttpError,
)
from lib.fetch.http import (
    do_request as _do_request,
)
from lib.fetch.http import (
    try_browser_fetch as _try_browser_fetch,  # NOTE: now accepts reason= kwarg
)
from lib.fetch.http import (
    try_playwright_fallback as _try_playwright_fallback,
)
from lib.fetch.utils import (
    _CACHE_EXTRACT_LIMIT,
    _HAS_LEGACY_SSL,
    _URL_RE,
    _circuit,
    _decode_bytes,
    _fetch_cache,
    _html_head_cache,
    _is_bot_extracted_text,
    _is_bot_protection,
    _is_known_spa,
    _looks_like_spa_shell,
    _normalize_code_hosting_url,
    _session,
    _should_fetch,
)
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'fetch_page_content',
    'get_publish_date_from_url',
    'fetch_contents_for_results',
    'fetch_urls',
    'extract_urls_from_text',
]


# ═══════════════════════════════════════════════════════
#  核心抓取
# ═══════════════════════════════════════════════════════

def fetch_page_content(url, max_chars=None,
                       pdf_max_chars=None, timeout=None):
    if max_chars is None: max_chars = _lib.FETCH_MAX_CHARS_SEARCH
    if pdf_max_chars is None: pdf_max_chars = _lib.FETCH_MAX_CHARS_PDF
    if timeout is None: timeout = _lib.FETCH_TIMEOUT
    # Rewrite code-hosting blob URLs to raw-content URLs (GitHub, GitLab, Bitbucket)
    url = _normalize_code_hosting_url(url)
    if not _should_fetch(url): return None
    url_is_pdf = url.lower().rstrip('/').endswith('.pdf')
    cached = _fetch_cache.get(url)
    if cached is not None:
        logger.debug('Cache hit (%s chars) — %s', f'{len(cached):,}', url[:80])
        if max_chars and not url_is_pdf and len(cached) > max_chars:
            return cached[:max_chars] + '\n[…truncated]'
        return cached

    domain = urlparse(url).netloc.lower()

    # ── 已知 SPA 域名: 跳过 requests，直接走 Playwright ──
    if _is_known_spa(url):
        logger.debug('🎭 Known SPA domain, using Playwright — %s', url[:80])
        return _try_playwright_fallback(url, max_chars, timeout)

    result = None
    is_pdf = False
    html_for_spa_check = None   # 保留原始 HTML 供 SPA 空壳检测

    try:
        resp, raw = _do_request(url, timeout, verify=True)
    except _HttpError as e:
        # 401/403/404/410/413 = URL 本身的问题 (鉴权/权限/不存在/超限), 不算域名故障, 不触发熔断
        if e.status_code in (401, 403, 404, 410, 413):
            label = {401: 'unauthorized', 403: 'forbidden',
                     404: 'not found', 410: 'gone', 413: 'too large'}.get(e.status_code, '')
            logger.debug('HTTP %d (%s) — %s', e.status_code, label, url[:120])
            # 401/403 → 尝试通过浏览器扩展获取 (用户可能已登录)
            if e.status_code in (401, 403):
                browser_text = _try_browser_fetch(url, max_chars, reason='HTTP %d' % e.status_code)
                if browser_text:
                    return browser_text
        else:
            # 429/5xx 等 → 记为域名故障
            _circuit.record_failure(url)
            logger.warning('HTTP %d — %s', e.status_code, url[:120], exc_info=True)
            # ── Browser fallback for 429/5xx (server may be rate-limited but user browser isn't) ──
            if e.status_code in (429, 500, 502, 503, 504):
                browser_text = _try_browser_fetch(url, max_chars, reason='HTTP %d' % e.status_code)
                if browser_text:
                    logger.info('[Fetch] Browser fallback OK after HTTP %d — %s (%d chars)',
                                e.status_code, url[:80], len(browser_text))
                    return browser_text
        return None
    except requests.exceptions.SSLError as e:
        # ── SSL 失败: 降级重试 ──
        is_legacy_renegotiation = 'UNSAFE_LEGACY_RENEGOTIATION' in str(e)
        if is_legacy_renegotiation and _HAS_LEGACY_SSL:
            logger.warning('SSL legacy renegotiation error, retrying with legacy adapter — %s', domain, exc_info=True)
            try:
                resp, raw = _do_request(url, timeout, legacy_ssl=True)
            except _HttpError as e2:
                if e2.status_code not in (401, 403, 404, 410, 413):
                    _circuit.record_failure(url)
                logger.warning('SSL-legacy-fallback HTTP %d — %s', e2.status_code, url[:120], exc_info=True)
                return None
            except Exception as e2:
                _circuit.record_failure(url)
                logger.error('SSL-legacy-fallback also failed — %s: %s', url[:80], e2, exc_info=True)
                return None
        else:
            logger.warning('SSL failed, retrying without verify — %s: %s', domain, e, exc_info=True)
            try:
                resp, raw = _do_request(url, timeout, verify=False)
            except _HttpError as e2:
                if e2.status_code not in (401, 403, 404, 410, 413):
                    _circuit.record_failure(url)
                logger.warning('SSL-fallback HTTP %d — %s', e2.status_code, url[:120], exc_info=True)
                return None
            except Exception as e2:
                _circuit.record_failure(url)
                logger.error('SSL-fallback also failed — %s: %s', url[:80], e2, exc_info=True)
                return None
    except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout):
        _circuit.record_failure(url)
        logger.warning('Timeout (%ds) — %s', timeout, url[:80], exc_info=True)
        # ── Browser fallback for timeout (server network may be slow) ──
        browser_text = _try_browser_fetch(url, max_chars, reason='timeout')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after timeout — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        return None
    except requests.exceptions.ConnectionError as e:
        _circuit.record_failure(url)
        # 区分 timeout-in-disguise 和真正的连接错误
        err_str = str(e).lower()
        if 'timeout' in err_str or 'timed out' in err_str:
            logger.warning('Timeout (ConnectionError) — %s', url[:80], exc_info=True)
        else:
            logger.warning('ConnectionError — %s: %s', url[:80], e, exc_info=True)
        # ── Browser fallback for connection errors ──
        browser_text = _try_browser_fetch(url, max_chars, reason='ConnectionError')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after ConnectionError — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        return None
    except requests.exceptions.ContentDecodingError as e:
        # Brotli / gzip decode failure — already retried in do_request,
        # so this means both attempts failed. Try browser fallback.
        _circuit.record_failure(url)
        logger.warning('ContentDecodingError (both attempts failed) — %s: %s', url[:80], e)
        browser_text = _try_browser_fetch(url, max_chars, reason='ContentDecodingError')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after ContentDecodingError — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        return None
    except Exception as e:
        _circuit.record_failure(url)
        logger.warning('%s — %s: %s', type(e).__name__, url[:80], e, exc_info=True)
        # ── Browser fallback for other network errors ──
        browser_text = _try_browser_fetch(url, max_chars, reason=type(e).__name__)
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after %s — %s (%d chars)',
                        type(e).__name__, url[:80], len(browser_text))
            return browser_text
        return None

    # ── 连接成功，清除该域名的失败计数 ──
    _circuit.record_success(url)

    try:
        ct = resp.headers.get('Content-Type', '').lower()
        is_pdf = ('application/pdf' in ct or url.lower().rstrip('/').endswith('.pdf')
                  or raw[:5] == b'%PDF-')
        if is_pdf:
            pdf_lim = pdf_max_chars if pdf_max_chars > 0 else 999999999
            from lib.pdf_parser import extract_pdf_text as _unified_extract_pdf_text
            result = _unified_extract_pdf_text(raw, max(pdf_lim, _CACHE_EXTRACT_LIMIT), url)
        elif 'text/plain' in ct:
            text = _decode_bytes(raw, resp.encoding).strip()
            result = (text[:_CACHE_EXTRACT_LIMIT] if len(text) > _CACHE_EXTRACT_LIMIT
                      else text) if len(text) > 30 else None
        else:
            html = _decode_bytes(raw, resp.encoding)
            if _is_bot_protection(html):
                # 反爬页面 → 尝试 Playwright 绕过
                logger.debug('🛡️ Bot protection detected, trying Playwright — %s', url[:80])
                return _try_playwright_fallback(url, max_chars, timeout)
            html_for_spa_check = html   # 保留供下面 SPA 检测
            # Cache first 20KB of HTML for publish-date extraction
            _html_head_cache.put(url, html[:20480])
            result = _extract_html_text(html, _CACHE_EXTRACT_LIMIT, url=url)
    except Exception as e:
        logger.error('Parse error — %s: %s', url[:80], e, exc_info=True)
        return None

    # ── Post-extraction bot-protection check ──
    # Catches Cloudflare/Akamai/DDoS-Guard pages that slipped past the
    # HTML-level _is_bot_protection check (e.g. HTML was too large, or a
    # new challenge variant).  Detected from the short extracted text.
    if result and _is_bot_extracted_text(result):
        logger.debug('🛡️ Bot protection in extracted text (%d chars), '
                     'trying Playwright — %s', len(result), url[:80])
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        # Playwright also failed — discard the bot text entirely
        logger.debug('Playwright also failed for bot page — %s', url[:80])
        return None

    # ── SPA 空壳检测: HTML 有内容但提取文本太少 → Playwright fallback ──
    if not is_pdf and html_for_spa_check and _looks_like_spa_shell(html_for_spa_check, result):
        logger.debug('SPA shell detected (HTML=%sB, '
              'text=%d), trying Playwright — %s',
              f'{len(html_for_spa_check):,}', len(result) if result else 0, url[:80])
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        # Playwright 也失败了, 如果 requests 至少有点内容就用它
        if result and len(result) > 50:
            _fetch_cache.put(url, result)
            if max_chars and len(result) > max_chars:
                return result[:max_chars] + '\n[…truncated]'
            return result
        return None

    if result and len(result) > 50:
        _fetch_cache.put(url, result)
        logger.debug('OK (%s chars%s) — %s', f'{len(result):,}', ', PDF' if is_pdf else '', url[:80])
        if max_chars and not is_pdf and len(result) > max_chars:
            return result[:max_chars] + '\n[…truncated]'
        return result
    logger.debug('Empty result (len=%d) — %s', len(result) if result else 0, url[:80])
    return None


# ═══════════════════════════════════════════════════════
#  Publish date from URL
# ═══════════════════════════════════════════════════════

def get_publish_date_from_url(url, timeout=8):
    """Try to extract publication date from a URL's HTML meta tags.

    First checks the _html_head_cache (populated by prior fetch_page_content
    calls). If cache miss, performs a lightweight HEAD-range request to grab
    only the first 20KB of HTML for meta extraction.
    Returns ISO string 'YYYY-MM-DD' (day-level) or ''.
    """
    if not url:
        return ''
    # 1) Check cache — fetch_page_content may have already cached it
    cached_html = _html_head_cache.get(url)
    if cached_html:
        return extract_html_publish_date(cached_html)

    # 2) Lightweight fetch — only first 20KB (enough for <head>)
    try:
        sess = _session
        resp = sess.get(url, timeout=(5, timeout), stream=True,
                        allow_redirects=True, verify=True,
                        headers={'Range': 'bytes=0-20479'})
        try:
            if not resp.ok and resp.status_code != 206:
                return ''
            ct = resp.headers.get('Content-Type', '').lower()
            if 'html' not in ct and 'text' not in ct:
                return ''
            # Read up to 20KB
            chunks = []
            total = 0
            for chunk in resp.iter_content(4096):
                chunks.append(chunk)
                total += len(chunk)
                if total >= 20480:
                    break
            html_head = b''.join(chunks).decode('utf-8', errors='replace')
        finally:
            resp.close()
        _html_head_cache.put(url, html_head)
        return extract_html_publish_date(html_head)
    except Exception as e:
        logger.debug('[Fetch] publish date HEAD request failed for %s: %s', url[:80], e, exc_info=True)
        return ''


# ═══════════════════════════════════════════════════════
#  批量抓取
# ═══════════════════════════════════════════════════════

def fetch_contents_for_results(results, max_fetch=None,
                               max_chars=None, target_ok=None):
    """Fetch page content for search results concurrently.

    Uses a "race to N" strategy: fires all fetches in parallel but stops
    waiting as soon as ``target_ok`` pages have returned usable content.
    This means fast sites are never blocked by slow/broken ones.

    Args:
        results: List of search result dicts to fetch.
        max_fetch: Maximum number of URLs to attempt (default: all).
        max_chars: Max chars of extracted text per page.
        target_ok: Stop early once this many pages have content.
                   Default: FETCH_TOP_N × 2 (generous target for reranking).
    """
    if max_chars is None: max_chars = _lib.FETCH_MAX_CHARS_SEARCH
    if not results: return results
    if max_fetch is None:
        max_fetch = len(results)        # attempt all candidates
    if target_ok is None:
        target_ok = _lib.FETCH_TOP_N * 2  # enough for good reranking
    to_fetch = results[:max_fetch]
    logger.info('[Fetch] fetch_contents: starting %d URLs, target_ok=%d, max_chars=%s',
                len(to_fetch), target_ok, max_chars)
    t0 = time.time()
    ok_count = 0
    def _do(r):
        return r, fetch_page_content(r['url'], max_chars=max_chars,
                                     pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_do, r): r for r in to_fetch}
        pending = set(futs.keys())
        try:
            for fut in as_completed(futs, timeout=90):
                pending.discard(fut)
                try:
                    result, content = fut.result()
                    if content and len(content) > 50:
                        result['full_content'] = content
                        ok_count += 1
                except Exception as e:
                    logger.warning('[Fetch] fetch_contents thread error: %s', e, exc_info=True)
                # Race-to-N: once we have enough content, stop waiting
                if ok_count >= target_ok and pending:
                    elapsed_so_far = time.time() - t0
                    logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, '
                                'cancelling %d slow fetches',
                                ok_count, len(to_fetch), elapsed_so_far,
                                len(pending))
                    for p in pending:
                        p.cancel()
                    break
        except TimeoutError:
            logger.warning('[Fetch] fetch_contents: as_completed timeout (90s)', exc_info=True)
    elapsed = time.time() - t0
    logger.info('[Fetch] fetch_contents done: %d/%d got content in %.1fs', ok_count, len(to_fetch), elapsed)
    return results


def fetch_urls(urls, max_chars=None,
               pdf_max_chars=None, timeout=None):
    if max_chars is None: max_chars = _lib.FETCH_MAX_CHARS_DIRECT
    if pdf_max_chars is None: pdf_max_chars = _lib.FETCH_MAX_CHARS_PDF
    if timeout is None: timeout = _lib.FETCH_TIMEOUT
    logger.debug('fetch_urls: starting %d URL(s), max_chars=%s', len(urls), max_chars)
    t0 = time.time()
    results = {}
    failed_urls = []
    def _do(u):
        return u, fetch_page_content(u, max_chars=max_chars,
                                     pdf_max_chars=pdf_max_chars, timeout=timeout)
    # Total deadline = per-request timeout + generous buffer for download + parsing
    deadline = max(timeout * 4, 120)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_do, u): u for u in urls}
        done_count = 0
        try:
            for fut in as_completed(futs, timeout=deadline):
                try:
                    url, content = fut.result()
                    if content and len(content) > 50:
                        results[url] = content
                    else:
                        failed_urls.append(url)
                except Exception as e:
                    logger.warning('[Fetch] fetch_urls thread error: %s', e, exc_info=True)
                    failed_urls.append(futs.get(fut, '?'))
                done_count += 1
        except TimeoutError:
            logger.warning('as_completed timeout: %d/%d done after %ss', done_count, len(futs), deadline, exc_info=True)
    elapsed = time.time() - t0
    logger.debug('fetch_urls done: %d/%d succeeded in %.1fs', len(results), len(urls), elapsed)
    if failed_urls:
        failed_summary = ', '.join(u[:60] for u in failed_urls[:5])
        logger.warning('fetch_urls failed: %s', failed_summary)
    return results


def extract_urls_from_text(text):
    if not text: return []
    urls = _URL_RE.findall(text)
    seen, unique = set(), []
    for u in urls:
        u = u.rstrip('.,;:!?')
        if u not in seen and len(u) > 10: seen.add(u); unique.append(u)
    return unique[:5]
