# HOT_PATH
"""Search/fetch primitives: single-query search and single-URL fetch.

These are the authoritative, side-effect-owning seams. The orchestrating
handlers (in ``_handlers``) resolve ``_web_search_one`` / ``_fetch_url_one``
THROUGH the package facade at call time so package-level monkeypatches
(``patch('lib.tasks_pkg.handlers.search._web_search_one', ...)``) steer them.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from urllib.parse import unquote, urlparse

import lib as _lib
from lib.log import get_logger
from tofu_search import fetch_url_bytes
from tofu_search.search.vertical import (detect_vertical_intent, search_vertical,
                                          search_vertical_domain, list_domains)

logger = get_logger(__name__)


#: Content types that are TEXT, never a downloadable "file asset". A body with
#: one of these types either extracts as prose (handled upstream) or is an
#: error/blocked shell — staging it produces the self-contradictory note
#: "file asset (text/html …), not a readable web page" that made the model
#: distrust the tool and retry sibling hosts.
_TEXTUAL_CT_PREFIXES = ('text/',)
_TEXTUAL_CT_EXACT = frozenset({
    'application/xhtml+xml', 'application/xml',
})

#: Phrases that mark an HTTP-200 body as a soft failure (geo block, region
#: gate, login/robot wall) rather than the requested document. Matched against
#: the EXTRACTED text, lowercased. Kept deliberately narrow and paired with a
#: length ceiling so a long genuine article that merely mentions one phrase is
#: never misclassified.
_SOFT_BLOCK_MARKERS = (
    'only available in certain regions',
    'app unavailable',
    'not available in your country',
    'not available in your region',
    'this content is not available in your',
    'access denied from your location',
    'service is unavailable in your area',
)

#: A block interstitial ANNOUNCES itself at the top of the document ("# App
#: unavailable" is char 0 of the real shell). Matching only the head is what
#: separates it from a genuine long article that merely discusses geo-blocking.
#: A total-length ceiling does NOT work here: the real shell extracts to ~12.7 K
#: chars because the SPA's nav/footer junk comes along with it.
_SOFT_BLOCK_HEAD_CHARS = 1500


def _facade_mod():
    """Return the package facade module (late import breaks the import cycle).

    Callers resolve ``fetch_page_content`` / ``perform_web_search`` THROUGH
    this so package-level monkeypatches steer them.
    """
    from lib.tasks_pkg.handlers import search as _facade
    return _facade


def _is_textual_content_type(ct: str) -> bool:
    """True when *ct* denotes text — i.e. never a stageable binary asset."""
    c = (ct or '').split(';')[0].strip().lower()
    if not c:
        return False
    return c.startswith(_TEXTUAL_CT_PREFIXES) or c in _TEXTUAL_CT_EXACT


def _looks_soft_blocked(text: str) -> bool:
    """True when extracted *text* is an unavailability shell, not the document.

    A soft block is an HTTP **200** whose body says the resource can't be served
    here (region gate, app-unavailable screen). Only the document HEAD is
    matched: an interstitial leads with its notice, whereas an article that
    merely mentions geo-blocking does so in its body.
    """
    if not text:
        return False
    head = text[:_SOFT_BLOCK_HEAD_CHARS].lower()
    return any(m in head for m in _SOFT_BLOCK_MARKERS)


# ══════════════════════════════════════════════════════════
#  Helpers: single-query search and single-URL fetch
# ══════════════════════════════════════════════════════════

def resolve_vertical(query: str, vertical: str = 'auto'):
    """Resolve the vertical search plan for a query.

    Args:
        query: User-facing search query.
        vertical: One of 'auto' / 'off' / a domain name from
            :func:`tofu_search.search.vertical.list_domains`.

    Returns:
        A zero-arg callable that, when invoked, returns either a domain-level
        record (``{'domain', 'sources', 'items', 'content'}``) or a legacy
        type-level record (``{'domain', 'type', 'content', 'source'}``), or
        ``None`` if no vertical applies.
    """
    v = (vertical or 'auto').strip().lower()
    if v == 'off':
        return None
    if v == 'auto':
        intent = detect_vertical_intent(query)
        if not intent:
            return None
        t, identifier, params = intent
        logger.info('[Search] Vertical auto-intent: type=%s ident=%s for query=%r',
                    t, identifier, query[:60])
        return lambda: search_vertical(t, identifier, params)
    if v in list_domains():
        logger.info('[Search] Vertical explicit domain=%s for query=%r', v, query[:60])
        return lambda: search_vertical_domain(v, query)
    logger.warning('[Search] Unknown vertical=%r — falling back to auto', vertical)
    intent = detect_vertical_intent(query)
    if not intent:
        return None
    t, identifier, params = intent
    return lambda: search_vertical(t, identifier, params)


def _web_search_one(query: str, user_question: str, freshness: str = '',
                    vertical: str = 'auto'):
    """Run one web search — returns (results_list, search_diag, engine_breakdown, vertical_result).

    Vertical domain search (when ``vertical`` resolves) runs concurrently
    with the main web pipeline so it adds zero latency. ``vertical='auto'``
    keeps the legacy phrase-detection path; an explicit domain forces a
    fan-out across every sub-source in that domain.
    """
    from concurrent.futures import ThreadPoolExecutor as _TPE

    vertical_result = None
    vertical_future = None
    _vertical_pool = None
    plan = resolve_vertical(query, vertical)
    if plan is not None:
        _vertical_pool = _TPE(max_workers=1)
        vertical_future = _vertical_pool.submit(plan)

    from lib.tasks_pkg.handlers import search as _facade
    try:
        results = _facade.perform_web_search(query, user_question=user_question, freshness=freshness)
    except Exception as e:
        logger.error('[Executor] web_search failed for query=%r: %s', query, e, exc_info=True)
        results = []
        if vertical_future:
            try:
                vertical_result = vertical_future.result(timeout=10)
            except Exception as ve:
                logger.warning('[Search] Vertical query also failed: %s', ve)
            if _vertical_pool:
                _vertical_pool.shutdown(wait=False)
        return (
            results,
            {
                'reason': 'exception',
                'reason_detail': 'Search failed due to an internal error: %s' % str(e)[:200],
                'engine_errors': {}, 'engine_empty': [], 'engine_ok': [],
            },
            None,
            vertical_result,
        )

    if vertical_future:
        try:
            vertical_result = vertical_future.result(timeout=5)
        except Exception as ve:
            logger.warning('[Search] Vertical query failed: %s', ve)
        if _vertical_pool:
            _vertical_pool.shutdown(wait=False)

    return (
        results,
        getattr(results, '_search_diag', None),
        getattr(results, '_engine_breakdown', None),
        vertical_result,
    )


def _ext_for_asset(target_url: str, content_type: str) -> str:
    """Best extension for a staged asset: URL path first, else content type.

    An extensionless URL (``/media/no-ext-here``) otherwise staged an
    extensionless blob that ``read_files`` could not dispatch on.
    """
    try:
        ext = os.path.splitext(unquote(urlparse(target_url).path))[1].lower()
    except Exception as e:
        logger.debug('[Fetch] could not read URL extension: %s', e)
        ext = ''
    if ext:
        return ext
    base_ct = (content_type or '').split(';')[0].strip().lower()
    if not base_ct:
        return ''
    guessed = mimetypes.guess_extension(base_ct) or ''
    if guessed == '.jpe':  # stdlib quirk: image/jpeg → .jpe
        guessed = '.jpg'
    return guessed


def _safe_filename(target_url: str, ext: str) -> str:
    """Build a collision-resistant local filename for a staged asset.

    Uses the URL's basename stem when usable, otherwise just a hash of the
    URL, always suffixed with a short URL hash + the original extension.
    """
    try:
        path = unquote(urlparse(target_url).path)
        stem = os.path.splitext(os.path.basename(path))[0]
    except Exception as e:
        logger.debug('[Fetch] could not derive filename stem: %s', e)
        stem = ''
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', stem).strip('_')[:64]
    digest = hashlib.sha1(target_url.encode('utf-8', 'replace')).hexdigest()[:10]
    base = f'{stem}-{digest}' if stem else digest
    return f'{base}{ext}'


def _stage_binary_asset(target_url: str):
    """Stage a binary file asset to ``data/fetched/`` for read_files.

    Used only when the text pipeline returned nothing AND the URL is not a
    text asset. All fetching/SSRF/size policy lives in tofu_search's
    ``fetch_url_bytes`` — this function owns only the chatui-specific concern
    of persisting the bytes and crafting the read_files handoff note.

    Returns a dict with ``page_content`` (the note) + ``saved_path`` +
    ``is_asset=True``, or ``None`` if the download was rejected/failed.
    """
    got = fetch_url_bytes(target_url)
    if not got:
        return None
    raw, ct = got

    # ── Textual bodies are NEVER file assets ──
    # Refuse BEFORE the write: an HTML body reaching here means the text
    # pipeline already declined it (blocked shell, login wall, empty SPA).
    # Staging it wrote a multi-hundred-KB blob to disk on every retry and
    # handed the model "file asset (text/html …), not a readable web page".
    if _is_textual_content_type(ct):
        logger.info('[Fetch] refusing to stage textual body as asset '
                    '(ct=%s, %d bytes) — %s', ct, len(raw), target_url[:100])
        return None

    from lib.config_dir import fetched_path
    dest = fetched_path(_safe_filename(target_url, _ext_for_asset(target_url, ct)))
    try:
        with open(dest, 'wb') as f:
            f.write(raw)
    except Exception as e:
        logger.error('[Fetch] failed to stage asset to %s: %s', dest, e, exc_info=True)
        return None

    logger.info('[Fetch] staged binary asset %d bytes (ct=%s) → %s',
                len(raw), ct or '?', dest)
    note = (
        f'[fetch_url] This URL is a file asset ({ct or "unknown type"}, '
        f'{len(raw):,} bytes), not a readable web page, so it was downloaded '
        f'to a local staging path instead:\n\n  {dest}\n\n'
        f'Read it with read_files(path="{dest}") — read_files handles images, '
        f'PDFs and Office documents natively.'
    )
    return {'page_content': note, 'raw_chars': len(raw),
            'filtered_chars': len(note), 'saved_path': dest,
            'is_asset': True}


def _fetch_url_one(target_url: str, user_question: str, fetch_reason: str = ''):
    """Fetch one URL; apply content filter; return a dict with all display fields.

    Returns:
        {
          'url': str, 'page_content': str | None, 'is_pdf': bool,
          'raw_chars': int, 'filtered_chars': int, 'error_msg': str | None,
          'saved_path': str | None, 'is_asset': bool, 'reason': str,
        }

        ``reason`` is the TYPED outcome, so callers never have to infer intent
        from an empty ``page_content``:

          * ``extracted_ok``  — real content was extracted.
          * ``irrelevant``    — content filter judged the page off-topic. A
            SEMANTIC verdict, NOT an extraction failure.
          * ``soft_blocked``  — HTTP 200 returning an unavailability shell
            (geo/region gate). The whole HOST is unreachable here.
          * ``asset``         — a genuine binary asset was staged to disk.
          * ``fetch_failed``  — extraction failed and nothing could be staged.
          * ``rejected``      — the URL was refused before any request.
    """
    scheme = urlparse(target_url).scheme.lower()
    if scheme and scheme not in ('http', 'https', ''):
        logger.warning('[Fetch] Rejected non-HTTP scheme=%r: %s', scheme, target_url[:120])
        return {
            'url': target_url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': f'Rejected: {scheme}:// scheme (use read_files for local paths)',
            'saved_path': None, 'is_asset': False, 'reason': 'rejected',
        }

    _facade = _facade_mod()
    _fetch_diag = {}
    try:
        page_content = _facade.fetch_page_content(
            target_url,
            max_chars=_lib.FETCH_MAX_CHARS_DIRECT,
            pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF,
            diag=_fetch_diag,
        )
    except TypeError as e:
        # tofu-search < 0.5.3 has no `diag` out-param. Retry without it rather
        # than reporting a fetch failure that never happened.
        logger.debug('[Executor] fetch_page_content has no diag param (%s) — retrying', e)
        _fetch_diag = {}
        try:
            page_content = _facade.fetch_page_content(
                target_url,
                max_chars=_lib.FETCH_MAX_CHARS_DIRECT,
                pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF,
            )
        except Exception as e2:
            logger.error('[Executor] fetch_url failed for url=%s: %s', target_url, e2, exc_info=True)
            page_content = None
            _fetch_diag = {'reason': type(e2).__name__,
                           'detail': '%s: %s' % (type(e2).__name__, e2)}
    except Exception as e:
        logger.error('[Executor] fetch_url failed for url=%s: %s', target_url, e, exc_info=True)
        page_content = None
        _fetch_diag = {'reason': type(e).__name__,
                       'detail': '%s: %s' % (type(e).__name__, e)}

    is_pdf = (target_url.lower().rstrip('/').endswith('.pdf')
              or (page_content and page_content.startswith('[Page ')))
    raw_chars = len(page_content) if page_content else 0

    # ── Soft block: HTTP 200 carrying an unavailability shell ──
    # Detected BEFORE the content filter so we neither burn an LLM call on a
    # known-dead page nor let its [IRRELEVANT] verdict fall through to asset
    # staging. The verdict names the HOST: the model previously retried three
    # host variants of the same doc path, each re-downloading the same body.
    if page_content and _looks_soft_blocked(page_content):
        host = urlparse(target_url).netloc or target_url
        logger.warning('[Fetch] soft block (HTTP 200 unavailability shell) '
                       'host=%s — %s', host, target_url[:100])
        return {
            'url': target_url, 'page_content': None, 'is_pdf': is_pdf,
            'raw_chars': raw_chars, 'filtered_chars': 0,
            'error_msg': (
                f'Host {host} is unreachable from this deployment: it answered '
                f'HTTP 200 with an "unavailable in your region" page instead of '
                f'the document. This is a HOST-level block — do not retry this '
                f'URL or any other path on {host}. Try a different source.'
            ),
            'saved_path': None, 'is_asset': False, 'reason': 'soft_blocked',
        }

    # Text assets (SVG / source / config files) come back from fetch_page_content
    # verbatim — they're NOT prose, so skip the article relevance/noise filter
    # which would mangle or wrongly drop them.
    is_text_asset = _facade.looks_like_text_asset(target_url)

    irrelevant = False
    if page_content and not is_pdf and not is_text_asset:
        from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL
        filtered = filter_web_content(
            page_content, url=target_url,
            query=fetch_reason, user_question=user_question,
        )
        if filtered == IRRELEVANT_SENTINEL:
            logger.info('[Executor] fetch_url IRRELEVANT: %s', target_url[:100])
            page_content = None
            irrelevant = True
        else:
            page_content = filtered

    # ── Fallback: the text pipeline found nothing. The URL is likely a BINARY
    # file asset (image, archive, font, Office doc) that can't be extracted as
    # text. Stage the bytes to data/fetched/ and hand back the local path so
    # the model can read it with read_files. (Text assets like SVG/source are
    # already returned above by fetch_page_content — no second fetch needed.)
    saved_path = None
    is_asset = False
    error_msg = None
    reason = 'extracted_ok' if page_content else 'fetch_failed'

    if irrelevant:
        # A SEMANTIC verdict, not a failure to extract. Staging bytes here is
        # what produced the bogus "file asset" note for a readable HTML page.
        reason = 'irrelevant'
        error_msg = (
            'Fetched successfully, but the content filter judged this page '
            'irrelevant to the query. The page was readable — re-fetching it '
            'will not change the verdict; refine the query or try another source.'
        )
    elif not page_content:
        asset = _stage_binary_asset(target_url)
        if asset:
            page_content = asset.get('page_content')
            raw_chars = asset.get('raw_chars', raw_chars)
            saved_path = asset.get('saved_path')
            is_asset = bool(asset.get('is_asset'))
            reason = 'asset'
        else:
            # Nothing extracted AND nothing stageable. The pipeline knows WHY;
            # forward it so the model can act on the cause (retry, pick another
            # source, or tell the user an internal host needs allowlisting)
            # instead of seeing an indistinguishable "Failed to fetch".
            _why = (_fetch_diag or {}).get('detail')
            _tok = (_fetch_diag or {}).get('reason')
            if _tok:
                reason = 'fetch_failed:%s' % _tok
            if _why:
                error_msg = _why

    filtered_chars = len(page_content) if page_content else 0
    return {
        'url': target_url, 'page_content': page_content,
        'is_pdf': is_pdf, 'raw_chars': raw_chars,
        'filtered_chars': filtered_chars, 'error_msg': error_msg,
        'saved_path': saved_path, 'is_asset': is_asset, 'reason': reason,
    }


# Lazy import for content filter (used in _fetch_url_one)
from tofu_search.fetch.content_filter import filter_web_content  # noqa: E402
