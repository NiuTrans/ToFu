"""lib/search_bridge.py — Wire chatui behavior into tofu-search's seams.

tofu-search is a standalone library with no knowledge of chatui's LLM
dispatcher, browser extension, or auth-source store. It exposes three seams
(an LLM callable + two providers) that a host fills in. This module installs
chatui's implementations so the migrated search/fetch pipeline behaves
*exactly* as the in-tree ``lib/search`` + ``lib/fetch`` did before extraction:

  * **LLM filter** → chatui's ``dispatch_chat`` (model routing, key pools,
    ``capability='cheap'``, ``FETCH_FILTER_MODEL`` override, the
    ``§§IRRELEVANT§§`` stop token, and the HTTP-450 ``ContentFilterError``
    placeholder text).
  * **Browser fallback** → ``lib.browser`` extension (fetch + DDG-HTML search).
  * **Authenticated fetch** → ``lib.auth_sources`` (cookies/proxy lookup).

Call :func:`install_search_bridge` once at startup (after config load). It is
idempotent and degrades gracefully if any sub-system is unavailable.
"""

import os
import re
from urllib.parse import urlparse

import lib as _lib

import tofu_search
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['install_search_bridge', 'sync_search_config']

# Module-level filter knobs mirror the old lib/fetch/content_filter.py.
_FILTER_MODEL = os.environ.get('FETCH_FILTER_MODEL', '')   # empty ⇒ dispatcher default
_IRRELEVANT_STOP = '§§IRRELEVANT§§'

_installed = False


# ═══════════════════════════════════════════════════════
#  LLM seam — chatui dispatch_chat
# ═══════════════════════════════════════════════════════

def _chatui_llm(messages, **kwargs):
    """tofu-search llm_function adapter backed by chatui's dispatch_chat.

    Receives OpenAI-format messages + kwargs (``stop``, ``temperature``,
    ``timeout``) from tofu-search's content filter. Returns the assistant
    text. On an HTTP-450 content-policy rejection we return the SAME
    placeholder the old filter produced (the filter treats a returned string
    as success, so this preserves the "don't re-feed 450 text to the main
    model" behavior without re-raising).
    """
    from lib.llm import ContentFilterError
    from lib.llm_dispatch.api import dispatch_chat

    extra = {}
    stop = kwargs.get('stop')
    if stop:
        extra['stop'] = stop

    try:
        content, _usage = dispatch_chat(
            messages,
            temperature=kwargs.get('temperature', 0),
            thinking_enabled=False,
            capability='cheap',
            prefer_model=_FILTER_MODEL or None,
            max_retries=2,
            log_prefix='[ContentFilter]',
            timeout=kwargs.get('timeout'),
            extra=extra or None,
        )
        return content or ''
    except ContentFilterError:
        # The raw page text itself tripped the gateway's content policy.
        # Returning it verbatim would re-trigger 450 in the main chat call,
        # so emit a short placeholder instead (matches legacy behavior).
        url = ''
        for m in messages:
            mc = m.get('content') or ''
            hit = re.search(r'Source URL:\s*(\S+)', mc)
            if hit:
                url = hit.group(1)
                break
        logger.info('[ContentFilter] SKIP (content policy 450) url=%s — placeholder returned', url[:100])
        return (f'[Page content from {url} was filtered by content policy. '
                f'The page could not be processed by the LLM content filter.]')


# ═══════════════════════════════════════════════════════
#  Browser seam — lib.browser extension
# ═══════════════════════════════════════════════════════

# Extensions the browser extension MUST NOT be handed: it fetches by opening a
# real Chrome tab and scraping innerText/outerHTML, so a binary URL (PDF,
# archive, media, Office doc) yields no extractable text AND makes Chrome's
# download manager grab the file onto the USER's machine (the source-paper PDFs
# that mysteriously appeared in Downloads). These are handled server-side
# instead — PDFs via _extract_pdf_text, others simply reported as unfetchable.
# NOTE: `.svg` is deliberately absent (it's text — extractable in-tab).
_BROWSER_UNRENDERABLE_EXTS = (
    '.pdf',
    '.zip', '.tar', '.gz', '.tgz', '.rar', '.7z', '.bz2', '.xz',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico',
    '.mp4', '.mp3', '.wav', '.avi', '.mov', '.webm', '.mkv', '.flac', '.ogg',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.exe', '.dmg', '.iso', '.apk', '.bin',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
)


def _is_browser_unrenderable(url: str) -> bool:
    """True when ``url`` points at a binary asset the extension can't render.

    Opening such a URL in a browser tab downloads it to the user's machine
    (Chrome's download manager) and returns no text, so these URLs must never
    reach the browser fallback — they're fetched/parsed server-side instead.
    """
    try:
        path = urlparse(url).path.lower().rstrip('/')
    except Exception as e:
        logger.debug('[Bridge] unrenderable-URL parse failed for %s: %s', url[:80], e)
        return False
    return path.endswith(_BROWSER_UNRENDERABLE_EXTS)


class _ChatuiBrowserProvider(tofu_search.BrowserProvider):
    """Routes tofu-search browser fallbacks through chatui's extension."""

    def is_connected(self) -> bool:
        try:
            from lib.browser import is_extension_connected
            return bool(is_extension_connected())
        except Exception as e:
            logger.debug('[Bridge] is_extension_connected failed: %s', e)
            return False

    def fetch_url(self, url, *, max_chars=None, timeout=15):
        # A PDF/binary URL opened in a real Chrome tab downloads to the user's
        # machine and yields no text — refuse it so the fetch is reported as a
        # plain failure (PDFs are parsed server-side, not via the extension).
        if _is_browser_unrenderable(url):
            logger.info('[Bridge] browser fetch_url SKIP (binary/PDF, would download to client) — %s', url[:100])
            return None
        try:
            from lib.browser import fetch_url_via_browser
            return fetch_url_via_browser(url, max_chars=max_chars or 50000,
                                         timeout=max(timeout, 25))
        except Exception as e:
            logger.warning('[Bridge] browser fetch_url failed for %s: %s', url[:80], e)
            return None

    def fetch_html(self, url, *, timeout=20):
        """Return the RAW HTML of ``url`` fetched through the extension.

        tofu-search's ``search_via_browser`` calls this with a DuckDuckGo SERP
        URL and parses the returned HTML with its own engine-grade bs4 parser.
        chatui only owns the transport (the extension WebSocket) — the SERP
        parsing lives in the library, not duplicated here.
        """
        if _is_browser_unrenderable(url):
            logger.info('[Bridge] browser fetch_html SKIP (binary/PDF, would download to client) — %s', url[:100])
            return None
        try:
            from lib.browser import is_extension_connected, send_browser_command
        except Exception as e:
            logger.debug('[Bridge] browser fetch_html import failed: %s', e)
            return None
        if not is_extension_connected():
            return None
        try:
            result, error = send_browser_command('fetch_url', {
                'url': url, 'maxChars': 200000,
                'timeoutMs': max(timeout, 20) * 1000,
            }, timeout=max(timeout, 25))
            if error or not isinstance(result, dict):
                logger.warning('[Bridge] browser fetch_html failed for %s: %s',
                               url[:80], str(error)[:200])
                return None
            html = result.get('html', '') or result.get('text', '')
            if not html or len(html) < 100:
                logger.info('[Bridge] browser fetch_html got %d chars (too short) for %s',
                            len(html or ''), url[:80])
                return None
            logger.info('[Bridge] browser fetch_html got %d HTML chars for %s',
                        len(html), url[:80])
            return html
        except Exception as e:
            logger.error('[Bridge] browser fetch_html failed: %s', e, exc_info=True)
            return None


# ═══════════════════════════════════════════════════════
#  Auth-source seam — lib.auth_sources
# ═══════════════════════════════════════════════════════

class _ChatuiAuthSourceProvider(tofu_search.AuthSourceProvider):
    """Routes tofu-search authenticated fetch through chatui's auth store."""

    def match_source(self, url):
        try:
            from lib.auth_sources import match_source
            return match_source(url)
        except Exception as e:
            logger.debug('[Bridge] auth match_source failed for %s: %s', url[:80], e)
            return None

    def get_source(self, domain):
        try:
            from lib.auth_sources import get_source
            return get_source(domain)
        except Exception as e:
            logger.debug('[Bridge] auth get_source failed for %s: %s', domain, e)
            return None


# ═══════════════════════════════════════════════════════
#  Config sync + install
# ═══════════════════════════════════════════════════════

def sync_search_config():
    """Push chatui's live FETCH_* settings into tofu-search's global config."""
    filter_enabled = getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True)
    tofu_search.configure(
        llm_function=_chatui_llm,
        fetch_top_n=_lib.FETCH_TOP_N,
        fetch_timeout=_lib.FETCH_TIMEOUT,
        fetch_max_chars_search=_lib.FETCH_MAX_CHARS_SEARCH,
        fetch_max_chars_direct=_lib.FETCH_MAX_CHARS_DIRECT,
        fetch_max_chars_pdf=_lib.FETCH_MAX_CHARS_PDF,
        fetch_max_bytes=_lib.FETCH_MAX_BYTES,
        skip_domains=set(_lib.SKIP_DOMAINS),
        filter_enabled=filter_enabled,
        filter_min_chars=int(os.environ.get('FETCH_FILTER_MIN_CHARS', '3000')),
        filter_timeout=int(os.environ.get('FETCH_FILTER_TIMEOUT', '300')),
    )
    logger.info('[Bridge] tofu-search config synced: top_n=%d timeout=%ds '
                'max_chars(search=%d direct=%d pdf=%d) filter=%s model=%r',
                _lib.FETCH_TOP_N, _lib.FETCH_TIMEOUT,
                _lib.FETCH_MAX_CHARS_SEARCH, _lib.FETCH_MAX_CHARS_DIRECT,
                _lib.FETCH_MAX_CHARS_PDF,
                'on' if filter_enabled else 'off',
                _FILTER_MODEL or 'dispatch-default')


def install_search_bridge():
    """Install chatui's LLM + provider implementations into tofu-search.

    Idempotent — safe to call multiple times (e.g. on config reload).
    """
    global _installed
    sync_search_config()
    if not _installed:
        tofu_search.register_browser_provider(_ChatuiBrowserProvider())
        tofu_search.register_auth_source_provider(_ChatuiAuthSourceProvider())
        _installed = True
        logger.info('[Bridge] tofu-search bridge installed '
                    '(LLM=dispatch_chat, browser=extension, auth=auth_sources)')
    else:
        logger.debug('[Bridge] tofu-search config re-synced')
