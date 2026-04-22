"""lib/search/_common.py — Shared constants and helpers for search engines.

Exposes:
  HEADERS          — standard User-Agent / Accept-Encoding headers
  clean_text       — HTML-strip + entity-decode + control-char cleanup
  http_search_get  — the shared "timed requests.get + error-handling + elapsed
                     logging" skeleton used by every engine. Accepts a
                     *parser* callable that converts the successful response
                     into a list of result dicts.

Engine modules under ``lib/search/engines/`` use ``http_search_get`` so they
only own their parser and URL quirks — the HTTP envelope is DRY.
"""

import re
import time
import unicodedata
from collections.abc import Callable
from html import unescape

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['HEADERS', 'clean_text', 'http_search_get']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/121.0.0.0 Safari/537.36',
    'Accept-Encoding': 'gzip, deflate',      # avoid brotli decode issues
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(s):
    """Clean a search result string: strip HTML, decode entities, remove junk chars."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = unescape(s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad]', '', s)
    s = unicodedata.normalize('NFC', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def http_search_get(
    *,
    name: str,
    url: str,
    params: dict,
    query: str,
    parser: Callable[[requests.Response], list],
    max_results: int = 6,
    timeout: int = 12,
    headers: dict | None = None,
    on_ratelimit_retry: bool = False,
) -> list:
    """Shared HTTP envelope for scraping search engines.

    Parameters
    ----------
    name : str
        Engine name (``'Bing'``, ``'Brave'``, ``'DDG-HTML'``…) — used in log
        prefixes only.
    url : str
        Full endpoint URL.
    params : dict
        Query-string parameters passed to ``requests.get``.
    query : str
        Original user query — logged for diagnostics only.
    parser : callable
        ``parser(response) -> list[dict]``. Only invoked for a successful
        (``resp.ok``) response. Parser owns all format-specific regex / HTML
        handling.
    max_results : int
        Cap on number of results — enforced after parse, trimming overflow.
    timeout : int
        Per-request timeout in seconds.
    headers : dict, optional
        Override headers. Defaults to module-level ``HEADERS``.
    on_ratelimit_retry : bool
        If ``True`` and HTTP 202 is returned, sleep 0.6 s and retry once
        (DDG rate-limit behavior). Other engines set it to ``False``.

    Returns
    -------
    list
        Up to ``max_results`` parsed result dicts. Empty list on any
        failure — every error path already logs with the correct level.
    """
    tag = f'[Search] {name}'
    t0 = time.time()
    try:
        resp = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)

        # Rate-limit retry (DDG-specific, opt-in)
        if on_ratelimit_retry and resp.status_code == 202:
            logger.info('%s 202 (rate-limited), retry in 0.6s: %s', tag, query[:80])
            time.sleep(0.6)
            resp = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)

        if not resp.ok:
            logger.warning('%s returned HTTP %d for query: %s', tag, resp.status_code, query[:80])
            return []

        results = parser(resp) or []
        if len(results) > max_results:
            results = results[:max_results]

    except requests.Timeout:
        logger.warning('%s timeout for query: %s', tag, query[:80])
        results = []
    except requests.RequestException as e:
        logger.warning('%s request failed for query %r: %s', tag, query[:80], e)
        results = []
    except Exception as e:
        logger.error('%s error: %s', tag, e, exc_info=True)
        results = []

    elapsed = time.time() - t0
    logger.info('%s: %d results in %.1fs  query=%r', tag, len(results), elapsed, query[:60])
    return results
