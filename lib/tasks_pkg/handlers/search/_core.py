# HOT_PATH
"""Search/fetch primitives: single-query search and single-URL fetch.

These are the authoritative, side-effect-owning seams. Orchestrating handlers
depend on this module object, which is also the single test-injection boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import uuid
from urllib.parse import unquote, urlparse

import lib as _lib
from lib.browser.log_safety import text_for_log, url_for_log
from lib.log import get_logger
from lib.search_runtime import ensure_search_runtime

ensure_search_runtime()

from tofu_search import (
    fetch_page_content,
    fetch_url_bytes,
    looks_like_text_asset,
    perform_web_search,
)
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

    try:
        results = perform_web_search(
            query,
            user_question=user_question,
            freshness=freshness,
        )
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


def _stage_binary_asset(
    target_url: str,
    *,
    browser_claim_url: str = '',
):
    """Stage a binary file asset to ``data/fetched/`` for read_files.

    Used only when the text pipeline returned nothing AND the URL is not a
    text asset. All fetching/SSRF/size policy lives in tofu_search's
    ``fetch_url_bytes`` — this function owns only the chatui-specific concern
    of persisting the bytes and crafting the read_files handoff note.

    Returns a dict with ``page_content`` (the note) + ``saved_path`` +
    ``is_asset=True``, or ``None`` if the download was rejected/failed.
    """
    # An extensionless attachment may already have been discovered while the
    # text browser fallback inspected its headers. Claim that SAME streamed
    # response first: signed/one-time download URLs must never be fetched
    # twice merely to distinguish text from a file.
    try:
        from lib.search_bridge import claim_bound_browser_file
        browser_receipt = claim_bound_browser_file(
            browser_claim_url or target_url)
    except Exception as exc:
        logger.info('[Fetch] completed browser file claim failed for %s: %s',
                    url_for_log(target_url), text_for_log(exc))
        browser_receipt = None
    if browser_claim_url and not browser_receipt:
        # The provider already consumed the one authenticated/signed response.
        # A missing server receipt is an integrity failure, never permission to
        # issue a replay/cookie/anonymous second GET for the same capability.
        logger.warning('[Fetch] exact browser file handoff was unavailable; '
                       'failing closed — %s', url_for_log(target_url))
        return None
    got = None if browser_receipt else fetch_url_bytes(target_url)
    if got:
        raw, ct = got
    elif not browser_receipt:
        # The server may be unauthenticated while the selected browser has a
        # valid session.  Stream that browser response back to THIS server;
        # never invoke chrome.downloads (whose destination is the client).
        try:
            from lib.search_bridge import fetch_bound_browser_file
            browser_receipt = fetch_bound_browser_file(
                target_url,
                max_bytes=_lib.FETCH_MAX_BYTES,
                timeout=_lib.FETCH_TIMEOUT,
            )
        except Exception as exc:
            logger.info('[Fetch] browser file-transfer seam failed for %s: %s',
                        url_for_log(target_url), text_for_log(exc))
            browser_receipt = None
        if not browser_receipt:
            return None
        raw = None
        ct = str(browser_receipt.get('contentType') or '')
    else:
        raw = None
        ct = str(browser_receipt.get('contentType') or '')

    # ── Textual bodies are NEVER file assets ──
    # Refuse BEFORE the write: an HTML body reaching here means the text
    # pipeline already declined it (blocked shell, login wall, empty SPA).
    # Staging it wrote a multi-hundred-KB blob to disk on every retry and
    # handed the model "file asset (text/html …), not a readable web page".
    browser_attachment = bool(
        browser_receipt and (
            browser_receipt.get('isAttachment')
            or browser_receipt.get('hasFilename')))
    if _is_textual_content_type(ct) and not browser_attachment:
        logger.info('[Fetch] refusing to stage textual body as asset '
                    '(ct=%s, %d bytes) — %s', ct,
                    len(raw) if raw is not None else int(
                        browser_receipt.get('sizeBytes') or 0),
                    url_for_log(target_url))
        if browser_receipt:
            _delete_browser_staging_receipt(browser_receipt)
        return None

    if browser_receipt:
        dest = _validated_browser_staging_path(browser_receipt)
        if not dest:
            return None
        size_bytes = int(browser_receipt.get('sizeBytes') or 0)
        transport = 'browser_authenticated'
        sha256 = str(browser_receipt.get('sha256') or '')
    else:
        from lib.config_dir import fetched_path
        dest = fetched_path(
            _safe_filename(target_url, _ext_for_asset(target_url, ct)))
        temporary = f'{dest}.{uuid.uuid4().hex}.part'
        try:
            with open(temporary, 'xb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, dest)
        except Exception as e:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            logger.error('[Fetch] failed to stage asset to %s: %s',
                         dest, e, exc_info=True)
            return None
        size_bytes = len(raw)
        transport = 'server_direct'
        sha256 = hashlib.sha256(raw).hexdigest()

    logger.info('[Fetch] staged binary asset %d bytes (ct=%s) → %s',
                size_bytes, ct or '?', dest)
    acquisition = (
        'through the authenticated browser session into server staging'
        if transport == 'browser_authenticated'
        else 'directly into server staging'
    )
    note = (
        f'[fetch_url] This URL is a file asset ({ct or "unknown type"}, '
        f'{size_bytes:,} bytes), not a readable web page. It was transferred '
        f'{acquisition}:\n\n  {dest}\n\n'
        f'Read it with read_files(path="{dest}") — read_files handles images, '
        f'PDFs and Office documents natively.'
    )
    return {'page_content': note, 'raw_chars': size_bytes,
            'filtered_chars': len(note), 'saved_path': dest,
            'is_asset': True, 'location': 'server_staging',
            'transport': transport, 'sha256': sha256,
            'size_bytes': size_bytes}


def _validated_browser_staging_path(receipt: dict) -> str | None:
    """Accept only a server-authored path rooted in data/fetched/."""
    from lib.browser.file_transfer import STAGING_FILENAME_PREFIX
    from lib.config_dir import fetched_path

    root = os.path.realpath(os.path.dirname(fetched_path('path-probe')))
    candidate = os.path.realpath(str(receipt.get('path') or ''))
    try:
        in_root = os.path.commonpath((root, candidate)) == root
    except ValueError:
        in_root = False
    if (not candidate or not in_root
            or not os.path.basename(candidate).startswith(
                STAGING_FILENAME_PREFIX)
            or not os.path.isfile(candidate)):
        logger.error('[Fetch] invalid browser staging receipt '
                     '(transfer=%s)',
                     str(receipt.get('transferId') or '')[:12])
        return None
    try:
        actual_size = os.path.getsize(candidate)
        declared_size = int(receipt.get('sizeBytes'))
    except (OSError, TypeError, ValueError):
        actual_size = -1
        declared_size = -2
    if actual_size != declared_size:
        logger.error('[Fetch] browser staging receipt size mismatch '
                     '(transfer=%s declared=%s actual=%s)',
                     str(receipt.get('transferId') or '')[:12],
                     declared_size, actual_size)
        _delete_browser_staging_receipt(receipt)
        return None
    expected_sha256 = str(receipt.get('sha256') or '').strip().lower()
    try:
        digest = hashlib.sha256()
        with open(candidate, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        actual_sha256 = digest.hexdigest()
    except OSError:
        actual_sha256 = ''
    if (not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or not hmac.compare_digest(expected_sha256, actual_sha256)):
        logger.error('[Fetch] browser staging receipt digest mismatch '
                     '(transfer=%s)',
                     str(receipt.get('transferId') or '')[:12])
        _delete_browser_staging_receipt(receipt)
        return None
    return candidate


def _delete_browser_staging_receipt(receipt: dict) -> None:
    """Delete only a validated fetched-root receipt on semantic rejection."""
    from lib.browser.file_transfer import STAGING_FILENAME_PREFIX
    from lib.config_dir import fetched_path

    root = os.path.realpath(os.path.dirname(fetched_path('path-probe')))
    candidate = os.path.realpath(str(receipt.get('path') or ''))
    try:
        if (candidate
                and os.path.commonpath((root, candidate)) == root
                and os.path.basename(candidate).startswith(
                    STAGING_FILENAME_PREFIX)):
            os.unlink(candidate)
    except (FileNotFoundError, OSError, ValueError):
        pass


def _download_failure(
    code: str,
    message: str,
    *,
    retryable: bool,
    next_action: str,
) -> dict:
    """Return the one typed internal failure shape used by both entry paths."""
    return {
        'location': None,
        'saved_path': None,
        'is_asset': False,
        'error_code': str(code or 'server_download_failed')[:96],
        'error_msg': str(message or 'The URL could not be downloaded.')[:500],
        'retryable': bool(retryable),
        'next_action': str(next_action or 'Stop and report the failure.')[:300],
    }


def _looks_like_html_download(content_type: str, head: bytes = b'') -> bool:
    """Recognize a login/page response that must not masquerade as a file."""
    base = str(content_type or '').split(';', 1)[0].strip().lower()
    if base in {'text/html', 'application/xhtml+xml'}:
        return True
    sample = bytes(head or b'')[:1024].lstrip().lower()
    return sample.startswith((b'<!doctype html', b'<html', b'<head', b'<body'))


def _stage_direct_server_download(
    target_url: str,
    raw: bytes,
    content_type: str,
    *,
    owner_user_id,
) -> dict:
    """Atomically persist one server-fetched response under bounded staging."""
    if len(raw) > int(_lib.FETCH_MAX_BYTES):
        return _download_failure(
            'server_download_too_large',
            'The remote response exceeds the configured server download limit.',
            retryable=False,
            next_action=(
                'Ask the user to raise the Search max-download setting only if '
                'they expect and trust this file size.'),
        )
    try:
        from lib.browser.file_transfer import file_transfer_store
        receipt = file_transfer_store.stage_server_response(
            owner_user_id=owner_user_id,
            source_url=target_url,
            body=raw,
            content_type=content_type,
            suggested_filename=_safe_filename(
                target_url, _ext_for_asset(target_url, content_type)),
        )
    except Exception as exc:
        logger.error('[Download] direct staging failed for %s: %s',
                     url_for_log(target_url), text_for_log(exc))
        code = str(getattr(exc, 'code', '') or 'server_download_staging_failed')
        retryable = code in {
            'server_download_disk_headroom',
            'server_download_staging_capacity',
            'server_download_storage_error',
        }
        return _download_failure(
            code,
            text_for_log(exc, max_chars=300) or (
                'The response was fetched but could not be committed to server staging.'),
            retryable=retryable,
            next_action=(
                'Retry once; if it repeats, report a server staging capacity/storage failure.'
                if retryable else 'Report the server staging rejection.'),
        )
    return {
        'location': 'server_staging',
        'saved_path': receipt['path'],
        'is_asset': True,
        'transport': 'server_direct',
        'content_type': str(receipt.get('contentType') or content_type or ''),
        'size_bytes': int(receipt.get('sizeBytes') or 0),
        'sha256': str(receipt.get('sha256') or ''),
        'error_code': None,
        'error_msg': None,
    }


def _accept_browser_server_download(target_url: str, receipt: dict) -> dict:
    """Validate one server-authored browser receipt and reject login HTML."""
    destination = _validated_browser_staging_path(receipt)
    if not destination:
        return _download_failure(
            'browser_file_transfer_invalid_receipt',
            'The browser transfer completed without a valid server staging receipt.',
            retryable=True,
            next_action='Retry the exact download once; report a transfer integrity failure if it repeats.',
        )

    content_type = str(receipt.get('contentType') or '')
    attachment = bool(receipt.get('isAttachment') or receipt.get('hasFilename'))
    try:
        with open(destination, 'rb') as stream:
            head = stream.read(1024)
    except OSError:
        head = b''
    if not attachment and _looks_like_html_download(content_type, head):
        _delete_browser_staging_receipt(receipt)
        return _download_failure(
            'download_response_not_file',
            'The selected browser received an HTML/login page instead of the requested file.',
            retryable=True,
            next_action=(
                'Open and finish login for this site in the selected browser, '
                'then retry browser_download_url_to_server with the same URL.'),
        )

    size_bytes = int(receipt.get('sizeBytes') or 0)
    logger.info('[Download] browser-authenticated file staged (%d bytes, ct=%s) '
                'for %s', size_bytes, content_type or '?',
                url_for_log(target_url))
    return {
        'location': 'server_staging',
        'saved_path': destination,
        'is_asset': True,
        'transport': 'browser_authenticated',
        'content_type': content_type,
        'size_bytes': size_bytes,
        'sha256': str(receipt.get('sha256') or ''),
        'transfer_id': str(receipt.get('transferId') or ''),
        'error_code': None,
        'error_msg': None,
    }


def download_url_to_server(target_url: str, *, owner_user_id) -> dict:
    """Acquire one exact URL into server staging without exposing cookies.

    Server HTTP is the cheap first transport.  An HTML/login response is not
    accepted as the requested file; the strict request-scoped browser export
    then retries with Chrome's own cookie/network authority.  Both the explicit
    model tool and the cookie-bearing shell-command redirect call this function,
    so transport choice, integrity checks, and recovery errors have one owner.
    """
    scheme = urlparse(target_url).scheme.lower()
    if scheme not in {'http', 'https'}:
        return _download_failure(
            'server_download_invalid_url',
            'browser_download_url_to_server requires an http:// or https:// URL.',
            retryable=False,
            next_action='Use read_files for a local path; otherwise provide the exact remote URL.',
        )

    direct_error = None
    try:
        direct = fetch_url_bytes(target_url)
    except Exception as exc:
        direct = None
        direct_error = text_for_log(exc)
        logger.debug(
            '[Download] direct server transport failed for %s: %s',
            url_for_log(target_url), direct_error,
        )
    if direct:
        raw, content_type = direct
        if not _looks_like_html_download(content_type, raw[:1024]):
            return _stage_direct_server_download(
                target_url, raw, str(content_type or ''),
                owner_user_id=owner_user_id)
        logger.info('[Download] direct response looked like HTML/login; '
                    'trying bound browser for %s', url_for_log(target_url))

    try:
        from lib.search_bridge import require_bound_browser_file
        receipt = require_bound_browser_file(
            target_url,
            max_bytes=_lib.FETCH_MAX_BYTES,
            timeout=_lib.FETCH_TIMEOUT,
        )
    except Exception as exc:
        try:
            from lib.browser.access import BrowserAccessDenied
        except ImportError:  # pragma: no cover - defensive import boundary
            BrowserAccessDenied = ()
        try:
            from lib.browser.file_transfer import BrowserFileTransferError
        except ImportError:  # pragma: no cover - defensive import boundary
            BrowserFileTransferError = ()
        try:
            from lib.browser.protocol import (
                BrowserProtocolRejected,
                BrowserUpgradeRequired,
            )
        except ImportError:  # pragma: no cover - defensive import boundary
            BrowserProtocolRejected = ()
            BrowserUpgradeRequired = ()

        if BrowserFileTransferError and isinstance(exc, BrowserFileTransferError):
            code = exc.code
            retryable = code in {
                'browser_file_transfer_offline',
                'browser_file_transfer_unbound',
                'browser_file_transfer_command_failed',
                'browser_file_transfer_capacity',
                'browser_file_transfer_owner_capacity',
                'browser_file_transfer_staging_capacity',
                'browser_file_transfer_disk_headroom',
                'browser_file_transfer_storage_error',
                'browser_file_transfer_invalid_receipt',
            }
            next_action = (
                'Connect/reload the served browser extension 5.4 or newer and '
                'retry browser_download_url_to_server with the same URL.'
                if code in {
                    'browser_file_transfer_offline',
                    'browser_file_transfer_unbound',
                }
                else 'Retry once; report the browser file-transfer failure if it repeats.'
            )
        elif ((BrowserProtocolRejected and isinstance(
                exc, BrowserProtocolRejected))
              or (BrowserUpgradeRequired and isinstance(
                  exc, BrowserUpgradeRequired))):
            code = 'browser_extension_upgrade_required'
            retryable = False
            next_action = (
                'Install/reload the browser extension served by this Tofu '
                'instance (5.4 or newer), reconnect it, then retry.'
            )
        elif BrowserAccessDenied and isinstance(exc, BrowserAccessDenied):
            code = 'browser_access_denied'
            retryable = False
            next_action = (
                'Grant read access for this site to the selected browser, then retry.'
            )
        else:
            code = 'server_download_failed'
            retryable = False
            next_action = 'Report the download transport failure without trying cookie replay or curl.'
        detail = text_for_log(exc, max_chars=300)
        if direct_error:
            detail = f'server transport: {direct_error}; browser transport: {detail}'
        return _download_failure(
            code,
            detail or 'Neither server HTTP nor the selected browser could fetch the file.',
            retryable=retryable,
            next_action=next_action,
        )

    return _accept_browser_server_download(target_url, receipt)


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
        logger.warning('[Fetch] Rejected non-HTTP scheme=%r: %s', scheme,
                       url_for_log(target_url))
        return {
            'url': target_url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': f'Rejected: {scheme}:// scheme (use read_files for local paths)',
            'saved_path': None, 'is_asset': False, 'reason': 'rejected',
        }

    _fetch_diag = {}
    browser_claim_url = ''
    from lib.search_bridge import (
        BrowserFileHandoffReady,
        browser_file_handoff_boundary,
    )
    try:
        with browser_file_handoff_boundary():
            page_content = fetch_page_content(
                target_url,
                max_chars=_lib.FETCH_MAX_CHARS_DIRECT,
                pdf_max_chars=_lib.FETCH_MAX_CHARS_PDF,
                diag=_fetch_diag,
            )
    except BrowserFileHandoffReady as handoff:
        # The exact authenticated response is already in server staging.
        # Treat this as a typed transport outcome, not a text-fetch failure;
        # most importantly, do not let tofu-search issue another GET.
        page_content = None
        browser_claim_url = handoff.source_url
        _fetch_diag = {
            'reason': 'browser_file_handoff',
            'detail': 'Authenticated browser file response is ready.',
        }
    except Exception as e:
        logger.error('[Executor] fetch_url failed for url=%s: %s',
                     url_for_log(target_url), text_for_log(e))
        page_content = None
        _fetch_diag = {'reason': type(e).__name__,
                       'detail': '%s: %s' % (
                           type(e).__name__, text_for_log(e))}

    is_pdf = (target_url.lower().rstrip('/').endswith('.pdf')
              or (page_content and page_content.startswith('[Page ')))
    raw_chars = len(page_content) if page_content else 0

    # ── Soft block: HTTP 200 carrying an unavailability shell ──
    # Detected BEFORE the content filter so we neither burn an LLM call on a
    # known-dead page nor let its [IRRELEVANT] verdict fall through to asset
    # staging. The verdict names the HOST: the model previously retried three
    # host variants of the same doc path, each re-downloading the same body.
    if page_content and _looks_soft_blocked(page_content):
        host = urlparse(target_url).hostname or '[unknown-host]'
        logger.warning('[Fetch] soft block (HTTP 200 unavailability shell) '
                       'host=%s — %s', host, url_for_log(target_url))
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
    is_text_asset = looks_like_text_asset(target_url)

    irrelevant = False
    if page_content and not is_pdf and not is_text_asset:
        from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL
        filtered = filter_web_content(
            page_content, url=target_url,
            query=fetch_reason, user_question=user_question,
        )
        if filtered == IRRELEVANT_SENTINEL:
            logger.info('[Executor] fetch_url IRRELEVANT: %s',
                        url_for_log(target_url))
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
    asset_location = None
    asset_transport = None
    asset_sha256 = None
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
        asset = _stage_binary_asset(
            target_url, browser_claim_url=browser_claim_url)
        if asset:
            page_content = asset.get('page_content')
            raw_chars = asset.get('raw_chars', raw_chars)
            saved_path = asset.get('saved_path')
            is_asset = bool(asset.get('is_asset'))
            asset_location = asset.get('location')
            asset_transport = asset.get('transport')
            asset_sha256 = asset.get('sha256')
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
        'location': asset_location, 'transport': asset_transport,
        'sha256': asset_sha256,
    }


# Lazy import for content filter (used in _fetch_url_one)
from tofu_search.fetch.content_filter import filter_web_content  # noqa: E402
