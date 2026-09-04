"""tofu_search.fetch.core — Main public API: fetch_page_content, batch fetching, URL utilities.

Orchestrates the full fetch pipeline:
  cache → authenticated (cookie) fetch → HTTP request → SSL retry →
  Playwright fallback → host-browser fallback.

Config comes from :mod:`tofu_search.config`; the auth-source and browser
fallbacks are optional and routed through :mod:`tofu_search.providers`
(no-ops when no host provider is registered).
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from tofu_search.config import get_config
from tofu_search.fetch.html_extract import extract_html_publish_date
from tofu_search.fetch.html_extract import extract_html_text as _extract_html_text
from tofu_search.fetch.http import HttpError as _HttpError
from tofu_search.fetch.http import do_request as _do_request
from tofu_search.fetch.http import try_authenticated_fetch as _try_authenticated_fetch
from tofu_search.fetch.http import try_browser_fetch as _try_browser_fetch
from tofu_search.fetch.http import try_playwright_fallback as _try_playwright_fallback
from tofu_search.fetch.pdf_extract import extract_pdf_text as _extract_pdf_text
from tofu_search.fetch.readers import get_reader as _get_reader
from tofu_search.fetch.utils import (
    _CACHE_EXTRACT_LIMIT,
    _HAS_LEGACY_SSL,
    _URL_RE,
    _circuit,
    _decode_bytes,
    _fetch_cache,
    _host_is_safe,
    _html_head_cache,
    _is_bot_extracted_text,
    _is_bot_protection,
    _is_known_spa,
    _is_text_asset_ct,
    _looks_like_login_wall,
    _looks_like_spa_shell,
    _normalize_code_hosting_url,
    _session,
    _should_fetch,
)
from tofu_search.log import get_logger
from tofu_search.providers import (
    get_auth_source_provider,
    submit_with_provider_context,
    with_bound_optional_providers,
)

logger = get_logger(__name__)

__all__ = [
    'fetch_page_content',
    'fetch_url_bytes',
    'get_publish_date_from_url',
    'fetch_contents_for_results',
    'fetch_urls',
    'extract_urls_from_text',
    'get_fetch_cache_stats',
]


# ═══════════════════════════════════════════════════════
#  Core fetch
# ═══════════════════════════════════════════════════════

def _mk_deadline_browser(budget_blown):
    """Wrap the module browser fallback so it's skipped once the URL budget is blown."""
    def _wrapped(url, max_chars, reason='unknown'):
        if budget_blown():
            logger.info('[Fetch] skip browser fallback (per-URL deadline) — %s', url[:80])
            return None
        return _try_browser_fetch(url, max_chars, reason=reason)
    return _wrapped


def _mk_deadline_playwright(budget_blown):
    """Wrap the module Playwright fallback so it's skipped once the URL budget is blown."""
    def _wrapped(url, max_chars, timeout):
        if budget_blown():
            logger.info('[Fetch] skip Playwright fallback (per-URL deadline) — %s', url[:80])
            return None
        return _try_playwright_fallback(url, max_chars, timeout)
    return _wrapped


@with_bound_optional_providers
def fetch_page_content(url, max_chars=None, pdf_max_chars=None, timeout=None,
                       deadline_secs=None, diag=None):
    """Fetch ``url`` and return extracted text, or ``None`` on failure.

    ``diag``: an optional dict the caller passes IN to learn why a ``None``
    came back. The pipeline knows the cause (SSRF-blocked, skip-domain,
    circuit-open, HTTP status, timeout, SPA shell, login wall, deadline) but the
    ``str | None`` return threw it away, so every distinct failure surfaced to
    the model as one indistinguishable "Failed to fetch". On return it carries
    ``reason`` (a stable machine token) and ``detail`` (a human sentence).
    Passing nothing preserves the historical signature exactly.
    """
    def _diag(reason, detail):
        """Record the failure cause; always returns None so callers can `return _diag(...)`."""
        if diag is not None:
            diag['reason'] = reason
            diag['detail'] = detail
        return None

    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_search
    if pdf_max_chars is None: pdf_max_chars = cfg.fetch_max_chars_pdf
    if timeout is None: timeout = cfg.fetch_timeout

    # ── Per-URL total-time cap (0 = disabled) ──
    # Bounds the WHOLE fallback chain for one URL — the primary HTTP
    # body-download (via deadline_ts below) PLUS the browser and Playwright
    # fallbacks — so a single dead/slow host can't stack per-hop timeouts
    # (body ≈ timeout*3, +browser ≈ 15-25s, +Playwright ≈ 15s) into 60s+.
    # Once the budget is blown we skip any REMAINING fallback hop rather than
    # hard-killing an in-flight one, so worst case ≈ deadline + one hop.
    if deadline_secs is None:
        deadline_secs = getattr(cfg, 'fetch_url_deadline_secs', 0) or 0
    _url_t0 = time.time()
    _url_deadline_ts = (_url_t0 + deadline_secs) if deadline_secs > 0 else None

    def _url_budget_blown():
        return _url_deadline_ts is not None and time.time() >= _url_deadline_ts

    # Shadow the two expensive fallback helpers with deadline-aware wrappers.
    # A local assignment makes each name local for the ENTIRE function body, so
    # every call site below is gated by the per-URL budget without editing each
    # one individually. When the budget is blown the hop is skipped (returns
    # None → the caller falls through to its own "give up" path).
    _try_browser_fetch = _mk_deadline_browser(_url_budget_blown)
    _try_playwright_fallback = _mk_deadline_playwright(_url_budget_blown)

    # Rewrite code-hosting blob URLs to raw-content URLs (GitHub, GitLab, Bitbucket)
    url = _normalize_code_hosting_url(url)
    url_is_pdf = url.lower().rstrip('/').endswith('.pdf')
    cached = _fetch_cache.get(url)
    if cached is not None:
        logger.debug('Cache hit (%s chars) — %s', f'{len(cached):,}', url[:80])
        if max_chars and not url_is_pdf and len(cached) > max_chars:
            return cached[:max_chars] + '\n[…truncated]'
        return cached

    domain = urlparse(url).netloc.lower()

    # ── Reader tier (public no-login data endpoints) ──
    # Runs BEFORE the skip-domain gate so a domain that is blunt-blocked as an
    # un-fetchable JS/social app (e.g. x.com) can still yield a clean text
    # block for a *recognized* URL (a tweet/status link) via its public
    # endpoint. A reader that matches but yields nothing (deleted/not-found)
    # falls through to the normal skip/fetch policy — the block still applies
    # to non-recognized URLs on the same domain (a bare x.com/home stays
    # blocked). Readers reuse the shared HTTP transport; no duplicated request
    # logic, and this fires for BOTH web_search result-fetching and direct
    # fetch_url since both enter here.
    _reader = _get_reader(url)
    if _reader is not None:
        reader_text = _reader.read(url, max_chars=max_chars, timeout=timeout)
        if reader_text:
            _fetch_cache.put(url, reader_text)
            return reader_text
        logger.debug('[Fetch] reader %s matched but yielded nothing, '
                     'falling through — %s', getattr(_reader, 'name', '?'), url[:80])

    # ── Authenticated source (login-walled sites) ──
    # If a host has registered an auth-source provider and the user has
    # connected this domain (cookies), replay their logged-in session via
    # Playwright. Runs BEFORE the skip-domain gate too: a connected domain must
    # bypass the block (the anonymous paths only ever return the login wall).
    _auth_src = None
    _auth_provider = get_auth_source_provider()
    if _auth_provider is not None:
        try:
            _auth_src = _auth_provider.match_source(url)
        except Exception as e:
            logger.debug('[Fetch] auth-source lookup failed for %s: %s', url[:80], e)
            _auth_src = None
    if _auth_src:
        strategy = str(_auth_src.get('access_strategy') or 'browser_first').strip()
        if strategy == 'public':
            # The registry says this site needs NO identity — treat the match
            # as advisory and let the anonymous pipeline serve it.
            logger.debug('[Fetch] auth-source domain=%s access_strategy=public '
                         '— skipping identity paths — %s',
                         _auth_src.get('domain'), url[:80])
            _auth_src = None
    if _auth_src:
        logger.debug('[Fetch] auth-source match domain=%s strategy=%s — %s',
                     _auth_src.get('domain'), strategy, url[:80])
        _auth_wall = False
        _browser_reason = ('auth_source_browser_fallback'
                           if strategy == 'cookies_replay'
                           else 'auth_source_browser_first')

        def _browser():
            # Live-session: the host browser rides the user's own logged-in
            # session (native request signing, same IP/fingerprint as the
            # site's trust decisions).
            text = _try_browser_fetch(url, max_chars, reason=_browser_reason)
            if text:
                logger.info('[Fetch] Browser OK for auth-source domain=%s — '
                            '%s (%d chars)', _auth_src.get('domain'),
                            url[:80], len(text))
            return text

        def _replay():
            # Stored-cookie replay: ships the session to a headless server
            # browser on a foreign IP — the classic account risk-control
            # trigger. A NON-EMPTY result is not the same as a SUCCESSFUL
            # one: an SSO login wall is itself a full page, so returning it
            # here reported the replay as a success and made the browser
            # escalation UNREACHABLE. Judge the CONTENT.
            nonlocal _auth_wall
            if not (_auth_src.get('cookies') or []):
                # A browser_first row matches with ZERO stored cookies (the
                # live browser session IS the credential). Replaying nothing
                # would load the login wall in a headless pool — pure waste
                # and bot-shaped traffic.
                logger.debug('[Fetch] auth-source row has no stored cookies '
                             '— replay has nothing to send, skipping — %s',
                             url[:80])
                return None
            text = _try_authenticated_fetch(url, _auth_src, max_chars, timeout)
            if text and _looks_like_login_wall(text):
                logger.info('[Fetch] auth-source replay returned a LOGIN WALL '
                            '(%d chars) — stored cookies were not accepted by '
                            'the site — %s', len(text), url[:80])
                _auth_wall = True
                return None
            return text

        # Path ORDER comes from the registry's access_strategy (P2):
        #   browser_first (default) — live session primary, replay fallback;
        #   cookies_replay — the OLD order (risk-tolerant sites / browser
        #   usually offline): replay primary, browser fallback.
        text = None
        if strategy == 'cookies_replay':
            text = _replay() or _browser()
        else:
            text = _browser() or _replay()
        if text:
            return text
        logger.info('[Fetch] auth-source fetch yielded nothing, falling back '
                    'to anonymous pipeline — %s', url[:80])
        # An unrescued wall here means neither identity path could take over.
        if _auth_wall:
            # The replay hit a wall AND the browser could not take over. Report
            # the actionable cause instead of letting the anonymous pipeline
            # return the same wall as a "successful" fetch.
            return _diag(
                'auth_replay_rejected',
                'Stored credentials were sent but the site did not accept them, and '
                'the host browser is not available to take over. Some sites (e.g. an '
                'SSO whose page JS re-assembles the ticket before sending) never '
                'accept a cookie COPIED from devtools — the stored value is not the '
                'value the browser transmits — so re-pasting cookies cannot fix this. '
                'Connect the Tofu browser extension so the fetch runs inside your '
                'own logged-in session, or use the site\'s API with a long-lived token.')

    # ── Skip-domain / SSRF / binary-media / circuit gate ──
    # After the reader + auth-source bypasses so a connected or reader-handled
    # URL is not blunt-blocked, but before any anonymous network request.
    # `diag` is passed positionally-optional: _should_fetch is a documented
    # monkeypatch seam (tests and hosts substitute a 1-arg predicate), so a
    # replacement that does not accept the out-param must still work — the
    # gate's VERDICT is the contract, its diagnostics are a bonus.
    _gate = {}
    try:
        _allowed = _should_fetch(url, diag=_gate)
    except TypeError:
        _allowed = _should_fetch(url)
    if not _allowed:
        # The SSRF gate protects requests made by this process.  A host browser
        # provider executes inside the user's own browser/network and has its
        # own per-user domain policy, so private-address refusal must not make
        # that path unreachable. Other gates (scheme, download size, explicit
        # skip-domain, circuit) remain server-side refusals.
        if _gate.get('reason') == 'ssrf_blocked':
            browser_text = _try_browser_fetch(
                url, max_chars, reason='server_ssrf_browser_bypass')
            if browser_text:
                return browser_text
        return _diag(_gate.get('reason', 'refused'),
                     _gate.get('detail', 'URL refused before any request was made.'))

    # ── Known SPA domains: skip requests, go straight to Playwright ──
    if _is_known_spa(url):
        logger.debug('[Fetch] Known SPA domain, using Playwright — %s', url[:80])
        pw_text = _try_playwright_fallback(url, max_chars, timeout)
        if pw_text:
            return pw_text
        # Anonymous render got nothing (e.g. a login wall only a logged-in
        # session can pass) — give the host browser a shot before giving up.
        return _try_browser_fetch(url, max_chars, reason='known_spa')

    result = None
    is_pdf = False
    html_for_spa_check = None

    try:
        resp, raw = _do_request(url, timeout, verify=True, deadline_ts=_url_deadline_ts)
    except _HttpError as e:
        # 401/403/404/410/413 = URL-specific (auth/permission/missing/oversize),
        # not a domain fault → don't trip the circuit breaker.
        if e.status_code in (401, 403, 404, 406, 410, 413):
            label = {401: 'unauthorized', 403: 'forbidden', 404: 'not found',
                     406: 'not acceptable', 410: 'gone', 413: 'too large'}.get(e.status_code, '')
            logger.debug('HTTP %d (%s) — %s', e.status_code, label, url[:120])
            if e.status_code in (401, 403, 406):
                browser_text = _try_browser_fetch(url, max_chars, reason='HTTP %d' % e.status_code)
                if browser_text:
                    return browser_text
            return _diag('http_%d' % e.status_code,
                         'Server returned HTTP %d (%s).' % (e.status_code, label))
        else:
            _circuit.record_failure(url)
            logger.warning('HTTP %d — %s', e.status_code, url[:120])
            if e.status_code in (429, 500, 502, 503, 504):
                browser_text = _try_browser_fetch(url, max_chars, reason='HTTP %d' % e.status_code)
                if browser_text:
                    logger.info('[Fetch] Browser fallback OK after HTTP %d — %s (%d chars)',
                                e.status_code, url[:80], len(browser_text))
                    return browser_text
        return _diag('http_%d' % e.status_code,
                     'Server returned HTTP %d.' % e.status_code)
    except requests.exceptions.SSLError as e:
        is_legacy_renegotiation = 'UNSAFE_LEGACY_RENEGOTIATION' in str(e)
        if is_legacy_renegotiation and _HAS_LEGACY_SSL:
            logger.warning('SSL legacy renegotiation error, retrying with legacy adapter — %s', domain, exc_info=True)
            try:
                resp, raw = _do_request(url, timeout, legacy_ssl=True, deadline_ts=_url_deadline_ts)
            except _HttpError as e2:
                if e2.status_code not in (401, 403, 404, 406, 410, 413):
                    _circuit.record_failure(url)
                logger.warning('SSL-legacy-fallback HTTP %d — %s', e2.status_code, url[:120])
                return None
            except Exception as e2:
                _circuit.record_failure(url)
                logger.error('SSL-legacy-fallback also failed — %s: %s', url[:80], e2, exc_info=True)
                return None
        elif not cfg.allow_insecure_ssl_fallback:
            logger.warning('SSL verification failed and insecure fallback is disabled '
                           '(set allow_insecure_ssl_fallback=True to enable) — %s: %s', domain, e)
            return None
        else:
            logger.warning('SSL failed, retrying WITHOUT certificate verification (insecure) — %s: %s',
                           domain, e, exc_info=True)
            try:
                resp, raw = _do_request(url, timeout, verify=False, deadline_ts=_url_deadline_ts)
            except _HttpError as e2:
                if e2.status_code not in (401, 403, 404, 406, 410, 413):
                    _circuit.record_failure(url)
                logger.warning('SSL-fallback HTTP %d — %s', e2.status_code, url[:120])
                return None
            except Exception as e2:
                _circuit.record_failure(url)
                logger.error('SSL-fallback also failed — %s: %s', url[:80], e2, exc_info=True)
                return None
    except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout):
        _circuit.record_failure(url)
        logger.warning('Timeout (%ds) — %s', timeout, url[:80], exc_info=True)
        browser_text = _try_browser_fetch(url, max_chars, reason='timeout')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after timeout — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        return _diag('timeout', 'Host did not respond within %ds.' % timeout)
    except requests.exceptions.ConnectionError as e:
        err_str = str(e).lower()
        if 'pool is closed' in err_str:
            # Benign internal/shutdown race — not a server fault, records no
            # circuit failure, so debug rather than warning.
            logger.debug('ConnectionError (pool closed, not server fault) — %s: %s', url[:80], e)
        elif 'timeout' in err_str or 'timed out' in err_str:
            _circuit.record_failure(url)
            logger.warning('Timeout (ConnectionError) — %s', url[:80], exc_info=True)
        else:
            _circuit.record_failure(url)
            logger.warning('ConnectionError — %s: %s', url[:80], e, exc_info=True)
        browser_text = _try_browser_fetch(url, max_chars, reason='ConnectionError')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after ConnectionError — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        return _diag('connection_error', 'Could not connect to the host: %s' % str(e)[:200])
    except requests.exceptions.ContentDecodingError as e:
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
        browser_text = _try_browser_fetch(url, max_chars, reason=type(e).__name__)
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after %s — %s (%d chars)',
                        type(e).__name__, url[:80], len(browser_text))
            return browser_text
        return None

    # ── Connection succeeded → clear the domain's failure count ──
    _circuit.record_success(url)

    try:
        ct = resp.headers.get('Content-Type', '').lower()
        is_pdf = ('application/pdf' in ct or url.lower().rstrip('/').endswith('.pdf')
                  or raw[:5] == b'%PDF-')
        if is_pdf:
            pdf_lim = pdf_max_chars if pdf_max_chars > 0 else 999999999
            result = _extract_pdf_text(raw, max(pdf_lim, _CACHE_EXTRACT_LIMIT), url)
        elif 'text/plain' in ct:
            text = _decode_bytes(raw, resp.encoding).strip()
            result = (text[:_CACHE_EXTRACT_LIMIT] if len(text) > _CACHE_EXTRACT_LIMIT
                      else text) if len(text) > 30 else None
        elif _is_text_asset_ct(ct):
            # Text-based file asset (SVG, JSON, XML, YAML, CSS, JS, source code).
            # Return the raw source directly — there's nothing to "extract", and
            # the article-oriented bot/SPA/min-length gates below don't apply
            # (a 40-char JSON or tiny SVG is a complete, valid file). Return early.
            text = _decode_bytes(raw, resp.encoding).strip()
            if not text:
                logger.debug('Empty text asset (ct=%s) — %s', ct[:40], url[:80])
                return None
            if len(text) > _CACHE_EXTRACT_LIMIT:
                text = text[:_CACHE_EXTRACT_LIMIT]
            logger.debug('Text asset (ct=%s, %d chars) — %s', ct[:40], len(text), url[:80])
            _fetch_cache.put(url, text)
            if max_chars and len(text) > max_chars:
                return text[:max_chars] + '\n[…truncated]'
            return text
        else:
            html = _decode_bytes(raw, resp.encoding)
            if _is_bot_protection(html):
                logger.debug('[Fetch] Bot protection detected, trying Playwright — %s', url[:80])
                pw_text = _try_playwright_fallback(url, max_chars, timeout)
                if pw_text:
                    return pw_text
                return _try_browser_fetch(url, max_chars, reason='login_wall')
            html_for_spa_check = html
            _html_head_cache.put(url, html[:20480])
            result = _extract_html_text(html, _CACHE_EXTRACT_LIMIT, url=url)
    except Exception as e:
        logger.error('Parse error — %s: %s', url[:80], e, exc_info=True)
        return None

    # ── Post-extraction bot-protection check ──
    if result and _is_bot_extracted_text(result):
        logger.debug('[Fetch] Bot protection in extracted text (%d chars), '
                     'trying Playwright — %s', len(result), url[:80])
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        browser_text = _try_browser_fetch(url, max_chars, reason='login_wall')
        if browser_text:
            return browser_text
        logger.debug('Playwright also failed for bot page — %s', url[:80])
        return _diag('bot_wall',
                     'Blocked by bot protection / a login wall; JS rendering and the '
                     'browser fallback also failed to get past it.')

    # ── SPA-shell detection: HTML present but too little extracted text ──
    if not is_pdf and html_for_spa_check and _looks_like_spa_shell(html_for_spa_check, result):
        logger.debug('SPA shell detected (HTML=%sB, text=%d), trying Playwright — %s',
                     f'{len(html_for_spa_check):,}', len(result) if result else 0, url[:80])
        pw_result = _try_playwright_fallback(url, max_chars, timeout)
        if pw_result:
            return pw_result
        # 200 + SPA shell: the anonymous chain got a shell, not content — the
        # user's browser (with its live sessions) is the last identity path.
        browser_text = _try_browser_fetch(url, max_chars, reason='spa_shell')
        if browser_text:
            logger.info('[Fetch] Browser fallback OK after SPA shell — %s (%d chars)',
                        url[:80], len(browser_text))
            return browser_text
        if result and len(result) > 50:
            _fetch_cache.put(url, result)
            if max_chars and len(result) > max_chars:
                return result[:max_chars] + '\n[…truncated]'
            return result
        return _diag('spa_shell',
                     'Page is a JavaScript shell: the server returned HTTP 200 with no '
                     'readable text, and JS rendering plus the browser fallback both '
                     'yielded nothing (often a login-walled single-page app).')

    if result and len(result) > 50:
        _fetch_cache.put(url, result)
        logger.debug('OK (%s chars%s) — %s', f'{len(result):,}', ', PDF' if is_pdf else '', url[:80])
        if max_chars and not is_pdf and len(result) > max_chars:
            return result[:max_chars] + '\n[…truncated]'
        return result
    logger.debug('Empty result (len=%d) — %s', len(result) if result else 0, url[:80])
    return _diag('empty_extract',
                 'Fetched the page, but text extraction produced %d chars (below the '
                 '50-char floor) — likely a redirect stub or a near-empty page.'
                 % (len(result) if result else 0))


# ═══════════════════════════════════════════════════════
#  Raw byte fetch (for binary file assets)
# ═══════════════════════════════════════════════════════

def fetch_url_bytes(url, timeout=None, max_bytes=None):
    """Download the raw bytes of a URL — for binary file assets.

    Unlike :func:`fetch_page_content` (which extracts *text* and returns
    ``None`` for binary content), this returns the undecoded body so the
    caller can save an image / archive / Office document to disk. Enforces
    the SAME safety policy as the text pipeline:

      * scheme must be http/https,
      * SSRF guard (``block_private_addresses``) rejects internal hosts,
      * size cap (``max_bytes`` → defaults to ``config.fetch_max_bytes``).

    Args:
        url: Target URL.
        timeout: Request timeout in seconds (default ``config.fetch_timeout``).
        max_bytes: Hard cap on download size (default ``config.fetch_max_bytes``).

    Returns:
        ``(raw_bytes, content_type)`` on success, or ``None`` if the URL was
        rejected (bad scheme / SSRF / too large) or the download failed.
    """
    cfg = get_config()
    if timeout is None:
        timeout = cfg.fetch_timeout
    if max_bytes is None:
        max_bytes = cfg.fetch_max_bytes

    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.netloc:
        logger.debug('[Fetch] bytes: rejected non-HTTP URL — %.80s', url)
        return None
    if cfg.block_private_addresses and not _host_is_safe(p.hostname or ''):
        logger.warning('[Fetch] bytes: SSRF guard blocked internal address — %s', url[:80])
        return None

    try:
        resp, raw = _do_request(url, timeout, verify=True)
    except _HttpError as e:
        logger.debug('[Fetch] bytes: HTTP %d — %s', e.status_code, url[:120])
        return None
    except Exception as e:
        logger.warning('[Fetch] bytes: download failed — %s: %s: %s', url[:80], type(e).__name__, e)
        return None

    if not raw:
        return None
    if len(raw) > max_bytes:
        logger.info('[Fetch] bytes: body exceeds cap (%d > %d) — %s',
                    len(raw), max_bytes, url[:80])
        return None
    ct = (resp.headers.get('Content-Type') or '').lower()
    logger.debug('[Fetch] bytes: OK %d bytes ct=%s — %s', len(raw), ct[:40], url[:80])
    return raw, ct


# ═══════════════════════════════════════════════════════
#  Publish date from URL
# ═══════════════════════════════════════════════════════

def get_publish_date_from_url(url, timeout=8):
    """Try to extract publication date from a URL's HTML meta tags.

    Checks the _html_head_cache first (populated by prior fetch_page_content
    calls); on miss, does a lightweight range request for the first 20KB.
    Returns ISO string 'YYYY-MM-DD' (day-level) or ''.
    """
    if not url:
        return ''
    cached_html = _html_head_cache.get(url)
    if cached_html:
        return extract_html_publish_date(cached_html)

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
#  Batch fetching
# ═══════════════════════════════════════════════════════

def fetch_contents_for_results(results, max_fetch=None, max_chars=None, target_ok=None):
    """Fetch page content for search results concurrently.

    Uses a "race to N" strategy: fires all fetches in parallel but stops
    waiting as soon as ``target_ok`` pages have returned usable content.
    """
    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_search
    if not results: return results
    if max_fetch is None:
        max_fetch = len(results)
    if target_ok is None:
        target_ok = cfg.fetch_top_n * 2
    to_fetch = results[:max_fetch]
    logger.info('[Fetch] fetch_contents: starting %d URLs, target_ok=%d, max_chars=%s',
                len(to_fetch), target_ok, max_chars)
    t0 = time.time()
    ok_count = 0
    url_timings = []
    pdf_max = cfg.fetch_max_chars_pdf
    def _do(r):
        url = r['url']
        fetch_t0 = time.time()
        content = fetch_page_content(url, max_chars=max_chars, pdf_max_chars=pdf_max)
        fetch_elapsed = time.time() - fetch_t0
        return r, content, fetch_elapsed
    # NOTE: we DRAIN, not cancel — abandoning a streaming response mid
    # iter_content() while sibling threads GC C extensions has been
    # correlated with native aborts. Stop *using* results past target_ok,
    # but keep consuming completions so each thread closes cleanly.
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = {submit_with_provider_context(pool, _do, r): r for r in to_fetch}
        target_reached_at = None
        try:
            for fut in as_completed(futs, timeout=90):
                try:
                    result, content, fetch_elapsed = fut.result()
                    url = result['url']
                    ok = bool(content and len(content) > 50)
                    chars = len(content) if content else 0
                    if target_reached_at is None:
                        url_timings.append((url, fetch_elapsed, ok, chars))
                        if ok:
                            result['full_content'] = content
                            ok_count += 1
                        if fetch_elapsed > 5:
                            logger.info('[Fetch] SLOW url=%.80s  %.1fs  ok=%s chars=%d',
                                        url, fetch_elapsed, ok, chars)
                except Exception as e:
                    logger.warning('[Fetch] fetch_contents thread error: %s', e, exc_info=True)
                if ok_count >= target_ok and target_reached_at is None:
                    target_reached_at = time.time()
                    elapsed_so_far = target_reached_at - t0
                    in_flight = sum(1 for f in futs if not f.done())
                    logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, '
                                'draining %d in-flight fetches in background',
                                ok_count, len(to_fetch), elapsed_so_far, in_flight)
        except TimeoutError:
            logger.warning('[Fetch] fetch_contents: as_completed timeout (90s)', exc_info=True)
    elapsed = time.time() - t0

    if url_timings:
        url_timings.sort(key=lambda x: -x[1])
        slow_summary = '  '.join(
            f'[{"✓" if ok else "✗"}]{url[:50]}={et:.1f}s'
            for url, et, ok, _chars in url_timings[:8]
        )
        logger.info('[Fetch] fetch_contents done: %d/%d got content in %.1fs  slowest: %s',
                    ok_count, len(to_fetch), elapsed, slow_summary)
    else:
        logger.info('[Fetch] fetch_contents done: %d/%d got content in %.1fs',
                    ok_count, len(to_fetch), elapsed)
    return results


def fetch_urls(urls, max_chars=None, pdf_max_chars=None, timeout=None):
    cfg = get_config()
    if max_chars is None: max_chars = cfg.fetch_max_chars_direct
    if pdf_max_chars is None: pdf_max_chars = cfg.fetch_max_chars_pdf
    if timeout is None: timeout = cfg.fetch_timeout
    logger.debug('fetch_urls: starting %d URL(s), max_chars=%s', len(urls), max_chars)
    t0 = time.time()
    results = {}
    failed_urls = []
    def _do(u):
        return u, fetch_page_content(u, max_chars=max_chars,
                                     pdf_max_chars=pdf_max_chars, timeout=timeout)
    deadline = max(timeout * 4, 120)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {submit_with_provider_context(pool, _do, u): u for u in urls}
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


def get_fetch_cache_stats() -> dict:
    """Return diagnostic stats for all fetch caches."""
    return {
        'fetch_cache': _fetch_cache.stats,
        'html_head_cache': _html_head_cache.stats,
        'circuit_breaker': _circuit.get_status(),
    }
