"""lib/browser/fetch.py — Fetch a URL using the browser extension."""

from lib.browser.queue import _get_active_client, is_extension_connected, send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['fetch_url_via_browser']


def fetch_url_via_browser(url, max_chars=50000, timeout=25, client_id=None):
    """Fetch a URL using the browser extension (inherits user's session/cookies).

    This is used as a fallback when server-side fetch gets 401/403 — the user
    may be logged in on that site in their browser.

    Returns text content (str) on success, None on failure.
    """
    # Use explicit client_id or fall back to thread-local active client
    _cid = client_id or _get_active_client()
    if not is_extension_connected(_cid):
        return None

    result, error = send_browser_command('fetch_url', {
        'url': url,
        'maxChars': max_chars,
        'timeoutMs': min(timeout * 1000, 30000),
    }, timeout=timeout, client_id=_cid)

    if error:
        logger.warning('[BrowserFetch] FAILED url=%s client=%s error=%s',
                       url[:100], (_cid or 'any')[:12], str(error)[:200])
        return None

    if isinstance(result, dict):
        # ── Prefer server-side extraction from HTML (same pipeline as fetch_page_content) ──
        html = result.get('html', '')
        if html and len(html) > 200:
            try:
                from lib.fetch.html_extract import extract_html_text
                extracted = extract_html_text(html, max_chars, url=url)
                if extracted and len(extracted) > 50:
                    title = result.get('title', '')
                    logger.debug('Browser fetch OK (HTML→extract %s chars) title="%s" — %s',
                                 f'{len(extracted):,}', title[:60], url[:80])
                    return extracted
            except Exception as e:
                logger.warning('Browser fetch HTML extraction failed, falling back to innerText: %s', e)

        # ── Fallback: use raw innerText from extension ──
        text = result.get('text', '')
        if text and len(text) > 50:
            title = result.get('title', '')
            logger.debug('Browser fetch OK (innerText %s chars) title="%s" — %s',
                     f'{len(text):,}', title[:60], url[:80])
            return text
        err = result.get('error', '')
        logger.debug('Browser fetch empty for %s%s', url[:80],
                 f' ({err})' if err else '')

    return None
