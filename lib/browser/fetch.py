"""lib/browser/fetch.py — Fetch a URL using the browser extension."""

import threading
import time
from urllib.parse import urlsplit

from lib.browser.log_safety import text_for_log, url_for_log
from lib.browser.queue import is_extension_connected, send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['fetch_url_via_browser', 'last_browser_fallback']

_last_fallbacks = {}
_last_fallback_lock = threading.Lock()


def _record_fallback(
    url,
    *,
    ok,
    client_id,
    owner_user_id,
    detail='',
):
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError as exc:
        logger.debug('[Browser] fallback URL parse failed: %s', exc)
        host = ''
    with _last_fallback_lock:
        _last_fallbacks[str(owner_user_id or '')] = {
            'at': time.time(), 'host': host, 'ok': bool(ok),
            'client_id': str(client_id or ''),
            'detail': text_for_log(detail, max_chars=160),
        }
        if len(_last_fallbacks) > 64:
            oldest = min(_last_fallbacks,
                         key=lambda key: _last_fallbacks[key].get('at', 0))
            _last_fallbacks.pop(oldest, None)


def last_browser_fallback(*, user_id: str | None = None) -> dict | None:
    with _last_fallback_lock:
        if user_id is not None:
            row = _last_fallbacks.get(str(user_id or ''))
        else:
            row = max(_last_fallbacks.values(),
                      key=lambda item: item.get('at', 0), default=None)
        return dict(row) if row else None


def _abort_opportunistic_transfer(
    store,
    transfer,
    *,
    owner_user_id: str,
    client_id: str,
) -> None:
    if not store or not transfer:
        return
    store.abort(
        transfer['transferId'],
        owner_user_id=owner_user_id,
        client_id=client_id,
        internal=True,
    )


def _send_fetch_attempt(
    url,
    *,
    max_chars,
    max_bytes,
    timeout,
    navigation_timeout_ms,
    client_id,
    owner_user_id,
    profile,
    enable_file_transfer,
):
    """Send one fetch_url command with an optional exact file handoff."""
    transfer_store = None
    transfer = None
    try:
        from lib.browser.protocol import (
            BrowserCapability,
            require_capabilities,
        )
        require_capabilities(client_id, [BrowserCapability.FILE_EXPORT])
    except Exception as exc:
        # Without this capability the installed worker predates the
        # response-classification guard. Sending fetch_url could navigate an
        # extensionless attachment into the device Downloads folder.
        logger.debug(
            '[BrowserFetch] safe-file capability unavailable: %s',
            text_for_log(exc),
        )
        return None, (
            'Browser extension upgrade required for safe file-aware fetch: '
            f'{str(exc)[:200]}'
        ), None

    if enable_file_transfer:
        try:
            from lib.browser.file_transfer import file_transfer_store
            transfer_store = file_transfer_store
            transfer = transfer_store.create(
                owner_user_id=owner_user_id,
                client_id=client_id,
                profile=profile,
                source_url=url,
                max_bytes=max_bytes,
            )
        except Exception as exc:
            # The negotiated worker still has the fail-closed header guard, so
            # temporary capacity failure may safely retain text rendering.
            logger.debug('[BrowserFetch] opportunistic staging unavailable: %s',
                         exc)
            transfer_store = None
            transfer = None

    command_params = {
        'url': url,
        'maxChars': max_chars,
        # Leave room for extension polling, bounded SPA settle and the result
        # poll inside the outer command deadline.
        'timeoutMs': navigation_timeout_ms,
    }
    if transfer:
        command_params['fileTransfer'] = {
            'transferId': transfer['transferId'],
            'transferToken': transfer['transferToken'],
            'maxBytes': transfer['maxBytes'],
            'chunkBytes': transfer['chunkBytes'],
            # fetch_url itself has a 35-second execution ceiling. Preserve a
            # five-second result-settlement margin instead of reusing the
            # shorter page-navigation timeout for the byte stream.
            'timeoutMs': max(
                10_000, min(30_000, (max(1, int(timeout)) - 5) * 1_000)),
        }
    try:
        result, error = send_browser_command(
            'fetch_url', command_params,
            timeout=timeout,
            client_id=client_id,
            owner_user_id=owner_user_id,
        )
    except Exception:
        _abort_opportunistic_transfer(
            transfer_store, transfer,
            owner_user_id=owner_user_id, client_id=client_id)
        raise

    completed = bool(
        transfer
        and isinstance(result, dict)
        and result.get('location') == 'server_staging'
        and str(result.get('transferId') or '') == transfer['transferId']
    )
    if error or not completed:
        _abort_opportunistic_transfer(
            transfer_store, transfer,
            owner_user_id=owner_user_id, client_id=client_id)
        return result, error, None
    return result, None, (transfer_store, transfer)


def _handoff_completed_transfer(
    url,
    completed,
    *,
    owner_user_id: str,
    client_id: str,
    on_file_transfer,
) -> None:
    """Publish an exact transfer ID to the current task, never by URL scan."""
    store, transfer = completed
    transfer_id = transfer['transferId']
    if on_file_transfer is None:
        _abort_opportunistic_transfer(
            store, transfer,
            owner_user_id=owner_user_id, client_id=client_id)
        raise RuntimeError(
            'Browser file response requires an exact task handoff callback')
    else:
        try:
            accepted = bool(on_file_transfer(url, transfer_id))
        except Exception as exc:
            logger.warning('[BrowserFetch] file handoff failed for %s: %s',
                           url_for_log(url), text_for_log(exc))
            accepted = False
        if not accepted:
            _abort_opportunistic_transfer(
                store, transfer,
                owner_user_id=owner_user_id, client_id=client_id)
            raise RuntimeError('Browser file response could not be bound to its task')
    _record_fallback(
        url, ok=True, client_id=client_id, owner_user_id=owner_user_id,
        # The browser only relays completion; size/hash authority remains in
        # the server store and is checked when the task claims the transfer.
        detail='file_handoff')


def fetch_url_via_browser(
    url,
    max_chars=50000,
    max_bytes=20 * 1024 * 1024,
    timeout=35,
    *,
    client_id,
    owner_user_id,
    on_file_transfer=None,
):
    """Fetch a URL through the user's browser session when Chrome permits.

    This is used as a fallback when server-side fetch gets 401/403 — the user
    may be logged in on that site in their browser.  Chrome remains the cookie
    authority; this transport neither extracts nor replays cookies.

    Returns text content on page success. A file response is retained only when
    ``on_file_transfer(url, transfer_id)`` accepts its exact task handoff, then
    returns ``None`` so the binary staging branch can consume it. Ordinary
    unavailable/denied outcomes also return ``None``; transport faults may
    raise for the host adapter to classify.
    """
    _cid = str(client_id or '').strip()
    owner_user_id = str(owner_user_id or '').strip()
    if not is_extension_connected(
            _cid, owner_user_id=owner_user_id):
        _record_fallback(
            url,
            ok=False,
            client_id=_cid,
            owner_user_id=owner_user_id,
            detail='extension_offline',
        )
        return None

    try:
        from lib.browser.access import require_access
        from lib.browser.protocol import client_protocol
        info = client_protocol(_cid)
        require_access(owner_user_id, url, access='read',
                       client_id=_cid, profile=info.get('profile', ''))
    except Exception as exc:
        logger.info('[BrowserFetch] access denied url=%s client=%s: %s',
                    url_for_log(url), (_cid or 'any')[:12], text_for_log(exc))
        _record_fallback(
            url, ok=False, client_id=_cid,
            owner_user_id=owner_user_id, detail='access_denied')
        return None

    navigation_timeout_ms = max(
        1_000, min(20_000, (max(1, int(timeout)) - 10) * 1_000))
    result, error, completed_transfer = _send_fetch_attempt(
        url,
        max_chars=max_chars,
        max_bytes=max_bytes,
        timeout=timeout,
        navigation_timeout_ms=navigation_timeout_ms,
        client_id=_cid,
        owner_user_id=owner_user_id,
        profile=info.get('profile', ''),
        enable_file_transfer=on_file_transfer is not None,
    )

    if error:
        logger.warning('[BrowserFetch] FAILED url=%s client=%s error=%s',
                       url_for_log(url), (_cid or 'any')[:12],
                       text_for_log(error, max_chars=200))
        _record_fallback(
            url, ok=False, client_id=_cid,
            owner_user_id=owner_user_id, detail=str(error))
        return None

    if completed_transfer:
        _handoff_completed_transfer(
            url, completed_transfer,
            owner_user_id=owner_user_id,
            client_id=_cid,
            on_file_transfer=on_file_transfer,
        )
        return None

    if isinstance(result, dict):
        # ── Login-wall gate: a fetch that landed on an SSO/login page is NOT
        # content. Detect it by final URL (netloc-based), engage the cookie-
        # capture chain (probe → login tab → store; auto-approved), and only
        # when a session was captured synchronously retry once inline.
        # Failing that, return None so the caller never mistakes wall text
        # for page content.
        final_url = result.get('url', '') or ''
        if final_url:
            try:
                from lib.browser.access import require_access
                from lib.browser.protocol import client_protocol
                info = client_protocol(_cid)
                require_access(owner_user_id, final_url, access='read',
                               client_id=_cid, profile=info.get('profile', ''))
            except Exception as exc:
                logger.info('[BrowserFetch] final redirect denied url=%s: %s',
                            url_for_log(final_url), text_for_log(exc))
                _record_fallback(
                    final_url, ok=False, client_id=_cid,
                    owner_user_id=owner_user_id,
                    detail='redirect_access_denied')
                return None
            import lib.browser.cookie_capture as cookie_capture
            if cookie_capture.looks_like_login_wall(url, final_url, result.get('title', '') or ''):
                captured = cookie_capture.handle_login_wall(
                    url,
                    final_url=final_url,
                    client_id=_cid,
                    user_id=owner_user_id,
                )
                if not captured:
                    logger.info('[BrowserFetch] login wall for %s (final=%s) — capture '
                                'flow engaged, failing this round',
                                url_for_log(url), url_for_log(final_url))
                    return None
                logger.info('[BrowserFetch] session captured for %s — retrying fetch inline',
                            url_for_log(url))
                result, error, completed_transfer = _send_fetch_attempt(
                    url,
                    max_chars=max_chars,
                    max_bytes=max_bytes,
                    timeout=timeout,
                    navigation_timeout_ms=navigation_timeout_ms,
                    client_id=_cid,
                    owner_user_id=owner_user_id,
                    profile=info.get('profile', ''),
                    enable_file_transfer=on_file_transfer is not None,
                )
                if error or not isinstance(result, dict):
                    logger.warning('[BrowserFetch] post-capture retry FAILED url=%s error=%s',
                                   url_for_log(url),
                                   text_for_log(error, max_chars=200))
                    return None
                if completed_transfer:
                    _handoff_completed_transfer(
                        url, completed_transfer,
                        owner_user_id=owner_user_id,
                        client_id=_cid,
                        on_file_transfer=on_file_transfer,
                    )
                    return None
                retry_final = result.get('url', '') or url
                try:
                    require_access(
                        owner_user_id, retry_final, access='read',
                        client_id=_cid, profile=info.get('profile', ''))
                except Exception as exc:
                    logger.info('[BrowserFetch] post-capture redirect denied '
                                'url=%s: %s', url_for_log(retry_final),
                                text_for_log(exc))
                    return None

        # API bodies captured during navigation are often the only authoritative
        # content on SPA catalogues/dashboards.  Normalize them once at the
        # owner-scoped server boundary: response URLs re-enter read policy and
        # credential-shaped fields are redacted before model context is built.
        try:
            from lib.browser.network_evidence import render_network_evidence
            network_text = render_network_evidence(
                result, owner_user_id=owner_user_id,
                max_chars=max_chars)
        except Exception as exc:
            logger.warning('[BrowserFetch] network evidence normalization failed '
                           'for %s: %s', url_for_log(url), text_for_log(exc))
            network_text = ''

        # ── Prefer server-side extraction from HTML (same pipeline as fetch_page_content) ──
        html = result.get('html', '')
        if html and len(html) > 200:
            try:
                from lib.search_runtime import prepare_search_dependency_import
                prepare_search_dependency_import()
                from tofu_search.fetch.html_extract import extract_html_text
                extracted = extract_html_text(html, max_chars, url=url)
                if extracted and len(extracted) > 50:
                    title = result.get('title', '')
                    logger.debug('Browser fetch OK (HTML→extract %s chars) title="%s" — %s',
                                 f'{len(extracted):,}',
                                 text_for_log(title, max_chars=60),
                                 url_for_log(url))
                    _record_fallback(url, ok=True, client_id=_cid,
                                     owner_user_id=owner_user_id,
                                     detail=f'html:{len(extracted)}')
                    if network_text:
                        from lib.browser.network_evidence import merge_page_and_network
                        extracted = merge_page_and_network(
                            extracted, network_text, max_chars=max_chars)
                    return extracted
            except Exception as e:
                logger.warning('Browser fetch HTML extraction failed, falling back '
                               'to innerText: %s', text_for_log(e))

        # ── Fallback: use raw innerText from extension ──
        text = result.get('text', '')
        if text and len(text) > 50:
            title = result.get('title', '')
            logger.debug('Browser fetch OK (innerText %s chars) title="%s" — %s',
                         f'{len(text):,}', text_for_log(title, max_chars=60),
                         url_for_log(url))
            _record_fallback(url, ok=True, client_id=_cid,
                             owner_user_id=owner_user_id,
                             detail=f'text:{len(text)}')
            if network_text:
                from lib.browser.network_evidence import merge_page_and_network
                text = merge_page_and_network(
                    text, network_text, max_chars=max_chars)
            return text
        if network_text:
            logger.debug('Browser fetch OK (captured API data %s chars) — %s',
                         f'{len(network_text):,}', url_for_log(url))
            _record_fallback(url, ok=True, client_id=_cid,
                             owner_user_id=owner_user_id,
                             detail=f'network:{len(network_text)}')
            return network_text
        err = result.get('error', '')
        logger.debug('Browser fetch empty for %s%s', url_for_log(url),
                     f' ({text_for_log(err)})' if err else '')

    _record_fallback(
        url, ok=False, client_id=_cid,
        owner_user_id=owner_user_id, detail='empty')
    return None
