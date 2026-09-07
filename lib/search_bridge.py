"""lib/search_bridge.py — Wire Tofu host behavior into tofu-search's seams.

tofu-search is a standalone library with no knowledge of the host's LLM
dispatcher, browser extension, or auth-source store. It exposes three seams
(an LLM callable + two providers) that a host fills in. This module installs
Tofu's implementations so the migrated search/fetch pipeline behaves
*exactly* as the in-tree ``lib/search`` + ``lib/fetch`` did before extraction:

  * **LLM relevance gate** → chatui's ``dispatch_chat`` (model routing, key
    pools, ``capability='cheap'``, ``FETCH_FILTER_MODEL`` override, bounded
    binary verdicts, and the HTTP-450 ``ContentFilterError`` placeholder text).
  * **Browser fallback** → ``lib.browser`` extension (fetch + DDG-HTML search).
  * **Authenticated fetch** → ``lib.auth_sources`` (cookies/proxy lookup).

Call :func:`install_search_bridge` at the first real search/fetch use. It is
idempotent and concurrency-safe; settings reloads call
:func:`sync_search_config` directly after activation.
"""

import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

import lib as _lib
from lib.log import get_logger

logger = get_logger(__name__)

# ``search_runtime.ensure_search_runtime`` normally installs this policy first,
# but config/acceptance callers may import the bridge directly. Keep that path
# bounded too without creating a circular import back through search_runtime.
def _install_search_import_policy() -> bool:
    """Apply the dependency policy, leaving evidence on fail-soft startup."""
    try:
        from runtime_guards import install_pymupdf_classic_policy
        return install_pymupdf_classic_policy()
    except Exception as policy_error:
        logger.warning(
            '[SearchBridge] classic PyMuPDF policy installation failed: %s',
            type(policy_error).__name__,
        )
        return False


_install_search_import_policy()

import tofu_search
from lib.browser.log_safety import text_for_log, url_for_log

__all__ = [
    'BrowserFileHandoffReady', 'bind_search_browser',
    'browser_file_handoff_boundary', 'claim_bound_browser_file',
    'fetch_bound_browser_file', 'require_bound_browser_file',
    'install_search_bridge', 'sync_search_config',
]

# Module-level filter knobs mirror the old lib/fetch/content_filter.py.
_FILTER_MODEL = os.environ.get('FETCH_FILTER_MODEL', '')   # empty ⇒ dispatcher default
_IRRELEVANT_STOP = '§§IRRELEVANT§§'
_GATE_SYSTEM_MARKER = 'web page relevance judge'
_GATE_MAX_OUTPUT_TOKENS = 32
_CONTENT_FILTER_MAX_429_ATTEMPTS = 1

_installed = False
_install_lock = threading.RLock()
_search_browser_binding = ContextVar('tofu_search_browser_binding', default=None)
_search_browser_file_handoffs = ContextVar(
    'tofu_search_browser_file_handoffs', default=None)
_search_browser_current_file = ContextVar(
    'tofu_search_browser_current_file', default=None)
_browser_file_handoff_escape_enabled = ContextVar(
    'tofu_browser_file_handoff_escape_enabled', default=False)
_MAX_TASK_FILE_HANDOFFS = 16


class BrowserFileHandoffReady(BaseException):
    """Non-error escape from tofu-search's legacy text-only provider seam.

    ``tofu-search`` intentionally catches every ordinary provider ``Exception``
    and then tries another transport. A completed one-time file response is
    not a provider failure, so continuing would issue a second GET. This
    ``BaseException`` subclass crosses that legacy catch only while
    :func:`browser_file_handoff_boundary` is active; the owning fetch handler
    catches it immediately and claims the exact transfer ID.
    """

    def __init__(self, source_url: str, transfer_id: str):
        self.source_url = str(source_url or '')
        self.transfer_id = str(transfer_id or '')
        super().__init__('Authenticated browser file response is ready')


@contextmanager
def browser_file_handoff_boundary():
    """Allow a completed browser file to stop the legacy text fallback chain."""
    token = _browser_file_handoff_escape_enabled.set(True)
    try:
        yield
    finally:
        _browser_file_handoff_escape_enabled.reset(token)


class _TaskFileHandoffs:
    """Small thread-safe transfer-ID registry shared across task fan-out."""

    def __init__(self):
        self._lock = threading.Lock()
        self._transfer_ids: dict[str, None] = {}

    def remember(self, transfer_id: str) -> tuple[str, ...]:
        evicted = []
        with self._lock:
            self._transfer_ids.pop(transfer_id, None)
            while len(self._transfer_ids) >= _MAX_TASK_FILE_HANDOFFS:
                oldest = next(iter(self._transfer_ids))
                self._transfer_ids.pop(oldest, None)
                evicted.append(oldest)
            self._transfer_ids[transfer_id] = None
        return tuple(evicted)

    def discard(self, transfer_id: str) -> None:
        with self._lock:
            self._transfer_ids.pop(transfer_id, None)

    def drain(self) -> tuple[str, ...]:
        with self._lock:
            transfer_ids = tuple(self._transfer_ids)
            self._transfer_ids.clear()
        return transfer_ids


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean env var, falling back to ``default`` when unset.

    Truthy tokens: 1/true/yes/on (case-insensitive); everything else is False.
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _store_private_hosts() -> set:
    """Enabled internal-host allowlist entries from the Settings store.

    Returns an empty set when the store is empty OR unreadable — the fail-safe
    direction: an unreadable allowlist blocks internal hosts rather than
    silently widening the network boundary.
    """
    try:
        from lib.private_hosts import enabled_hosts
        return set(enabled_hosts())
    except Exception as e:
        logger.warning('[Bridge] private-hosts store unreadable, '
                       'treating allowlist as empty: %s', e)
        return set()


def _env_csv(key: str) -> list:
    """Parse a comma/whitespace-separated env var into a de-duped list.

    Returns ``[]`` when unset or blank, which callers treat as "leave the
    library default alone" rather than "set it to empty".
    """
    raw = (os.environ.get(key) or '').replace(',', ' ')
    out = []
    for tok in raw.split():
        tok = tok.strip()
        if tok and tok not in out:
            out.append(tok)
    return out


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
    stops = [stop] if isinstance(stop, str) else list(stop or ())
    # ``§§IRRELEVANT§§`` is the filter's semantic verdict. Sending the exact
    # same string as a provider stop makes compliant providers remove it from
    # the returned text, after which tofu-search mistakes the empty completion
    # for an anomaly and serves the irrelevant page. A tiny output ceiling
    # bounds gate generation without consuming its verdict. Preserve any
    # unrelated caller stops for forward compatibility.
    safe_stops = [value for value in stops if value != _IRRELEVANT_STOP]
    if safe_stops:
        extra['stop'] = safe_stops

    is_gate = any(
        message.get('role') == 'system'
        and _GATE_SYSTEM_MARKER in str(message.get('content') or '')
        for message in messages
        if isinstance(message, dict)
    )
    output_budget = (
        {'max_tokens': _GATE_MAX_OUTPUT_TOKENS} if is_gate else {})

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
            max_429_attempts=_CONTENT_FILTER_MAX_429_ATTEMPTS,
            **output_budget,
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
        logger.info('[ContentFilter] SKIP (content policy 450) url=%s — '
                    'placeholder returned', url_for_log(url))
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
    reach the browser *text/tab* fallback. They use server HTTP or the separate
    browser-response → server-staging byte transport instead.
    """
    try:
        path = urlparse(url).path.lower().rstrip('/')
    except Exception as e:
        logger.debug('[Bridge] unrenderable-URL parse failed for %s: %s',
                     url_for_log(url), text_for_log(e))
        return False
    return path.endswith(_BROWSER_UNRENDERABLE_EXTS)


@contextmanager
def bind_search_browser(
    *,
    user_id='',
    client_id='',
    required_capabilities=(),
):
    """Bind one task's exact browser identity across tofu-search executors.

    An explicit client remains authoritative and will surface an upgrade error
    if it lacks a capability. Without an explicit selection, prefer the
    freshest compatible owned device; if none is compatible, retain the
    freshest owned device so the strict caller can distinguish upgrade from
    offline instead of silently borrowing another owner's/global device.
    """
    uid = str(user_id or '')
    try:
        from lib.browser.queue import get_connected_clients
        clients = get_connected_clients(owner_user_id=uid)
        by_id = {str(row.get('client_id') or ''): row for row in clients}
        if client_id:
            selected = by_id.get(str(client_id or ''))
        else:
            required = {str(value) for value in required_capabilities if value}
            compatible = [
                row for row in clients
                if required <= set(row.get('capabilities') or ())
            ]
            candidates = compatible or clients
            selected = (
                max(candidates, key=lambda row: row.get('last_poll', 0))
                if candidates else None)
        binding = (uid, str((selected or {}).get('client_id') or ''),
                   str((selected or {}).get('profile') or ''))
    except Exception as exc:
        logger.debug('[Bridge] task browser binding failed: %s', exc)
        binding = (uid, '', '')
    token = _search_browser_binding.set(binding)
    handoff_registry = _TaskFileHandoffs()
    handoff_token = _search_browser_file_handoffs.set(handoff_registry)
    current_file_token = _search_browser_current_file.set(None)
    try:
        yield binding
    finally:
        # A completed response that the task never claimed has no remaining
        # user-visible value. Reclaim it at the task boundary instead of
        # relying on the registry TTL.
        for transfer_id in handoff_registry.drain():
            _abort_bound_browser_file(transfer_id, binding=binding)
        _search_browser_current_file.reset(current_file_token)
        _search_browser_file_handoffs.reset(handoff_token)
        _search_browser_binding.reset(token)


def _bind_browser_identity() -> tuple[str, str, str]:
    """Capture the request user's freshest browser before worker fan-out."""
    task_binding = _search_browser_binding.get()
    if task_binding is not None:
        return task_binding
    try:
        from routes.api_v1.auth import current_auth
        ctx = current_auth()
        user_id = str(getattr(ctx, 'owner_user_id', '') or '')
    except Exception as exc:
        logger.debug('[Bridge] request identity lookup failed: %s', exc)
        user_id = ''
    try:
        from lib.browser.queue import get_connected_clients
        clients = get_connected_clients(owner_user_id=user_id)
        client = max(clients, key=lambda row: row.get('last_poll', 0)) \
            if clients else {}
        return (user_id, str(client.get('client_id') or ''),
                str(client.get('profile') or ''))
    except Exception as exc:
        logger.debug('[Bridge] browser identity binding failed: %s', exc)
        return user_id, '', ''


def _abort_bound_browser_file(transfer_id: str, *, binding=None) -> None:
    user_id, client_id, _profile = binding or (
        _search_browser_binding.get() or ('', '', ''))
    if not user_id or not client_id or not transfer_id:
        return
    try:
        from lib.browser.file_transfer import file_transfer_store
        file_transfer_store.abort(
            transfer_id,
            owner_user_id=user_id,
            client_id=client_id,
            internal=True,
        )
    except Exception as exc:
        logger.debug('[Bridge] browser file handoff cleanup failed: %s', exc)


def _remember_bound_browser_file(url: str, transfer_id: str) -> bool:
    """Bind an exact completed transfer to this task's fetch invocation."""
    binding = _search_browser_binding.get()
    handoffs = _search_browser_file_handoffs.get()
    if binding is None or handoffs is None:
        return False
    user_id, client_id, _profile = binding
    transfer_id = str(transfer_id or '').strip()
    source = str(url or '')
    if not user_id or not client_id or not source or not transfer_id:
        return False

    previous = _search_browser_current_file.get()
    if previous and previous[1] != transfer_id:
        handoffs.discard(previous[1])
        _abort_bound_browser_file(previous[1], binding=binding)
    for evicted in handoffs.remember(transfer_id):
        _abort_bound_browser_file(evicted, binding=binding)
    _search_browser_current_file.set((source, transfer_id))
    return True


def claim_bound_browser_file(url: str):
    """Claim this task's exact completed file-transfer ID."""
    binding = _search_browser_binding.get()
    handoffs = _search_browser_file_handoffs.get()
    if binding is None or handoffs is None:
        return None
    user_id, client_id, _profile = binding
    if not user_id or not client_id:
        return None
    current = _search_browser_current_file.get()
    if not current or current[0] != str(url or ''):
        return None
    transfer_id = current[1]
    _search_browser_current_file.set(None)
    handoffs.discard(transfer_id)
    try:
        from lib.browser.file_transfer import file_transfer_store
        return file_transfer_store.consume_completed(
            transfer_id,
            owner_user_id=user_id,
            client_id=client_id,
        )
    except Exception as exc:
        _abort_bound_browser_file(transfer_id, binding=binding)
        logger.info('[Bridge] could not claim completed browser file for %s: %s',
                    url_for_log(url), text_for_log(exc))
        return None


def require_bound_browser_file(url: str, *, max_bytes: int, timeout: int):
    """Stage response bytes through this task's exact browser or raise.

    The explicit server-download tool uses this strict form so an offline,
    outdated, denied, or failed browser is visible as its typed recovery
    reason.  It never selects a process-global or merely recent device.
    """
    from lib.browser.file_transfer import (
        BrowserFileTransferError,
        fetch_file_via_browser,
    )

    binding = _search_browser_binding.get()
    if binding is None:
        raise BrowserFileTransferError(
            'browser_file_transfer_unbound',
            'No request-scoped browser identity is bound to this download',
            status=503,
        )
    user_id, client_id, _profile = binding
    if not user_id or not client_id:
        raise BrowserFileTransferError(
            'browser_file_transfer_offline',
            'No compatible browser extension is connected for this user',
            status=503,
        )
    existing = claim_bound_browser_file(url)
    if existing:
        return existing
    return fetch_file_via_browser(
        url,
        max_bytes=max_bytes,
        timeout=timeout,
        client_id=client_id,
        owner_user_id=user_id,
    )


def fetch_bound_browser_file(url: str, *, max_bytes: int, timeout: int):
    """Best-effort browser staging for the legacy text-fetch fallback.

    This is a host-only extension of tofu-search's provider seam: tofu-search
    continues to own server HTTP, while authenticated binary acquisition is
    routed through chatui's owner/device authority.  An unbound worker is
    inert and can never fall back to a globally recent browser.
    """
    try:
        return require_bound_browser_file(
            url, max_bytes=max_bytes, timeout=timeout)
    except Exception as exc:
        binding = _search_browser_binding.get() or ('', '', '')
        user_id, client_id, _profile = binding
        logger.info(
            '[Bridge] authenticated browser file transfer failed for %s '
            '(owner=%s client=%s): %s',
            url_for_log(url), user_id, client_id[:12], text_for_log(exc),
        )
        return None


class _ChatuiBrowserProvider(tofu_search.BrowserProvider):
    """Routes tofu-search browser fallbacks through chatui's extension."""

    def __init__(self, *, user_id='', client_id='', profile='', bound=False):
        self.user_id = str(user_id or '')
        self.client_id = str(client_id or '')
        self.profile = str(profile or '')
        self._bound = bool(bound)

    def bind(self):
        if self._bound:
            return self
        user_id, client_id, profile = _bind_browser_identity()
        return type(self)(user_id=user_id, client_id=client_id,
                          profile=profile, bound=True)

    def _route(self) -> dict:
        return {
            'client_id': self.client_id,
            'owner_user_id': self.user_id,
        } if self._bound and self.client_id and self.user_id else {}

    def is_connected(self) -> bool:
        try:
            from lib.browser.queue import is_extension_connected
            # Global providers are templates, not authority. Public tofu-search
            # entry points call bind() before use; direct/unbound probes must be
            # inert instead of observing another request's freshest browser.
            if not self._bound or not self.client_id:
                return False
            return bool(is_extension_connected(
                self.client_id, owner_user_id=self.user_id))
        except Exception as e:
            logger.debug('[Bridge] is_extension_connected failed: %s', e)
            return False

    def fetch_url(self, url, *, max_chars=None, timeout=15):
        # A PDF/binary URL opened in a real Chrome tab downloads to the user's
        # machine and yields no text — refuse the tab path. If server HTTP also
        # fails, _stage_binary_asset uses the separate file_export transport.
        if _is_browser_unrenderable(url):
            logger.info('[Bridge] browser fetch_url SKIP (binary/PDF, would '
                        'download to client) — %s', url_for_log(url))
            return None
        if not self.is_connected():
            return None
        try:
            from lib.browser.fetch import fetch_url_via_browser
            accepted_handoff = []
            handoff_enabled = bool(
                _browser_file_handoff_escape_enabled.get())

            def remember_file(source_url, transfer_id):
                accepted = _remember_bound_browser_file(
                    source_url, transfer_id)
                if accepted:
                    accepted_handoff.append((source_url, transfer_id))
                return accepted

            result = fetch_url_via_browser(
                url,
                max_chars=max_chars or 50000,
                max_bytes=_lib.FETCH_MAX_BYTES,
                timeout=max(timeout, 35),
                on_file_transfer=remember_file if handoff_enabled else None,
                **self._route(),
            )
            if accepted_handoff and handoff_enabled:
                source_url, transfer_id = accepted_handoff[-1]
                raise BrowserFileHandoffReady(source_url, transfer_id)
            return result
        except Exception as e:
            logger.warning('[Bridge] browser fetch_url failed for %s: %s',
                           url_for_log(url), text_for_log(e))
            return None

    def fetch_html(self, url, *, timeout=20):
        """Return the RAW HTML of ``url`` fetched through the extension.

        tofu-search's ``search_via_browser`` calls this with a DuckDuckGo SERP
        URL and parses the returned HTML with its own engine-grade bs4 parser.
        chatui only owns the transport (the extension WebSocket) — the SERP
        parsing lives in the library, not duplicated here.
        """
        if _is_browser_unrenderable(url):
            logger.info('[Bridge] browser fetch_html SKIP (binary/PDF, would '
                        'download to client) — %s', url_for_log(url))
            return None
        try:
            from lib.browser.queue import send_browser_command
        except Exception as e:
            logger.debug('[Bridge] browser fetch_html import failed: %s', e)
            return None
        if not self.is_connected():
            return None
        try:
            from lib.browser.protocol import (
                BrowserCapability,
                require_capabilities,
            )
            require_capabilities(
                self.client_id, [BrowserCapability.FILE_EXPORT])
            if self._bound:
                from lib.browser.access import require_access
                require_access(self.user_id, url, access='read',
                               client_id=self.client_id, profile=self.profile)
            result, error = send_browser_command('fetch_url', {
                'url': url, 'maxChars': 200000,
                'timeoutMs': max(timeout, 20) * 1000,
            }, timeout=max(timeout, 25), **self._route())
            if error or not isinstance(result, dict):
                logger.warning('[Bridge] browser fetch_html failed for %s: %s',
                               url_for_log(url),
                               text_for_log(error, max_chars=200))
                return None
            final_url = str(result.get('url') or url)
            if self._bound:
                from lib.browser.access import require_access
                require_access(self.user_id, final_url, access='read',
                               client_id=self.client_id, profile=self.profile)
            html = result.get('html', '') or result.get('text', '')
            if not html or len(html) < 100:
                logger.info('[Bridge] browser fetch_html got %d chars (too short) for %s',
                            len(html or ''), url_for_log(url))
                return None
            logger.info('[Bridge] browser fetch_html got %d HTML chars for %s',
                        len(html), url_for_log(url))
            return html
        except Exception as e:
            logger.error('[Bridge] browser fetch_html failed: %s',
                         text_for_log(e))
            return None


    def scrape(self, url, *, wait_selector='', extractor_js='[]',
               timeout=20, scrolls=0):
        """Open ``url`` in a BACKGROUND tab of the user's real Chrome, wait for
        the selector, run the extractor JS in-page, and return its JSON result.

        tofu-search 0.7.0's browser-first engines (XHS search) call this to get
        STRUCTURED data from pages that only render correctly inside the user's
        live session. Composed entirely from existing bridge commands —
        create_tab (background by default) → wait_for_element → scroll_page ×N
        → execute_js → close_tab (always; a leaked tab is a bug on the user's
        machine). Returns None on ANY path failure so the library falls back;
        a [] from the page is returned verbatim (a REAL empty, not a failure).
        """
        if _is_browser_unrenderable(url):
            logger.info('[Bridge] browser scrape SKIP (binary/PDF, would download to client) — %s',
                        url_for_log(url))
            return None
        try:
            from lib.browser.queue import send_browser_command
        except Exception as e:
            logger.debug('[Bridge] browser scrape import failed: %s', e)
            return None
        if not self.is_connected():
            return None
        tab_id = None
        try:
            if self._bound:
                from lib.browser.access import require_access
                require_access(self.user_id, url, access='read',
                               client_id=self.client_id, profile=self.profile)
            res, err = send_browser_command(
                'create_tab', {'url': url, 'active': False},
                timeout=max(timeout, 25), **self._route())
            if err or not isinstance(res, dict) or res.get('id') is None:
                logger.warning('[Bridge] scrape create_tab failed for %s: %s',
                               url_for_log(url),
                               text_for_log(err, max_chars=200))
                return None
            tab_id = res['id']
            if wait_selector:
                wres, werr = send_browser_command(
                    'wait_for_element',
                    {'tabId': tab_id, 'selector': wait_selector,
                     'timeout': max(timeout, 15) * 1000},
                    timeout=max(timeout, 15) + 10, **self._route())
                if werr or not (isinstance(wres, dict) and wres.get('found')):
                    # Slow/partial renders may still carry the data — extract anyway.
                    logger.info('[Bridge] scrape selector %r not confirmed for %s — '
                                'extracting anyway', wait_selector,
                                url_for_log(url))
            if self._bound:
                tabs, terr = send_browser_command(
                    'list_tabs', {}, timeout=8, **self._route())
                current = next((row for row in (tabs or [])
                                if str(row.get('id')) == str(tab_id)), None) \
                    if not terr and isinstance(tabs, list) else None
                if current is None:
                    logger.warning('[Bridge] scrape cannot verify final tab URL for %s',
                                   url_for_log(url))
                    return None
                from lib.browser.access import require_access
                require_access(
                    self.user_id, current.get('url') or '', access='read',
                    client_id=self.client_id, profile=self.profile)
            for _ in range(max(0, int(scrolls))):
                send_browser_command(
                    'scroll_page',
                    {'tabId': tab_id, 'direction': 'bottom', 'pixels': 3000},
                    timeout=10, **self._route())
                time.sleep(1.2)   # human-ish pause; lets the lazy-load fire
            res, err = send_browser_command(
                'execute_js', {'tabId': tab_id, 'code': extractor_js},
                timeout=max(timeout, 15), **self._route())
            if err:
                logger.warning('[Bridge] scrape execute_js failed for %s: %s',
                               url_for_log(url),
                               text_for_log(err, max_chars=200))
                return None
            if isinstance(res, dict) and res.get('__error'):
                logger.warning('[Bridge] scrape extractor raised in-page for %s: %s',
                               url_for_log(url),
                               text_for_log(res.get('message'), max_chars=200))
                return None
            logger.info('[Bridge] scrape OK for %s (%s)', url_for_log(url),
                        '%d items' % len(res) if isinstance(res, list)
                        else type(res).__name__)
            return res
        except Exception as e:
            logger.error('[Bridge] browser scrape failed: %s', text_for_log(e))
            return None
        finally:
            if tab_id is not None:
                try:
                    send_browser_command('close_tab', {'tabId': tab_id},
                                         timeout=5, **self._route())
                except Exception as e:
                    logger.debug('[Bridge] scrape close_tab failed (tab %s may leak): %s',
                                 tab_id, text_for_log(e))


_SiteSearchBase = getattr(tofu_search, 'SiteSearchProvider', object)


class _ChatuiSiteSearchProvider(_SiteSearchBase):
    """Expose ready read adapters to tofu-search without a host back-edge."""

    def __init__(self, *, user_id='', client_id='', profile='', bound=False):
        self.user_id = str(user_id or '')
        self.client_id = str(client_id or '')
        self.profile = str(profile or '')
        self._bound = bool(bound)

    def bind(self):
        if self._bound:
            return self
        user_id, client_id, profile = _bind_browser_identity()
        return type(self)(user_id=user_id, client_id=client_id,
                          profile=profile, bound=True)

    def list_sources(self):
        try:
            from lib.browser.adapters import adapter_health, list_adapters
            # Site adapters execute inside one user's live browser.  Never let
            # an unbound library call fall back to the globally freshest
            # extension: that would make availability order-dependent and,
            # more importantly, could cross a tenant boundary.  The search
            # orchestrator calls bind() before fan-out.
            if not self._bound or not self.client_id:
                return []
            rows = []
            for adapter in list_adapters():
                if not any(cmd.name == 'search' and cmd.access == 'read'
                           for cmd in adapter.commands):
                    continue
                # A future write command may require upload/download support;
                # that must not hide an otherwise healthy read-only search
                # command from tofu-search.
                health = adapter_health(
                    adapter, client_id=self.client_id,
                    command_name='search')
                if not health.get('healthy'):
                    continue
                rows.append({
                    'id': adapter.id, 'name': adapter.name,
                    'aliases': list(adapter.aliases),
                    'domains': list(adapter.domains), 'access': 'read',
                    'metadata': {'adapter_version': adapter.version},
                })
            return rows
        except Exception as exc:
            logger.debug('[Bridge] site-search discovery failed: %s', exc)
            return []

    def search(self, source_id, query, *, max_results=10, freshness=''):
        try:
            from lib.browser.adapters import get_adapter, invoke_adapter
            if not self._bound or not self.client_id:
                return None
            adapter = get_adapter(source_id)
            if adapter is None:
                return None
            result = invoke_adapter(
                adapter.id, 'search',
                {'query': query, 'limit': max_results, 'pages': 1},
                owner_user_id=self.user_id, client_id=self.client_id)
            return result.get('result') if result.get('ok') else None
        except Exception as exc:
            logger.warning('[Bridge] site-search %s failed: %s', source_id, exc)
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
            logger.debug('[Bridge] auth match_source failed for %s: %s',
                         url_for_log(url), text_for_log(e))
            return None

    def get_source(self, domain):
        try:
            from lib.auth_sources import get_source
            return get_source(domain)
        except Exception as e:
            logger.debug('[Bridge] auth get_source failed for %s: %s', domain, e)
            return None


# ═══════════════════════════════════════════════════════
#  Reader tier — km.internal.example.com doc URLs reroute to xuecheng-mcp
# ═══════════════════════════════════════════════════════

# Soft floor: on tofu-search versions without the reader tier the base class
# does not exist; degrading to `object` keeps this module importable and the
# reader simply never gets registered (install guards with hasattr).
_SiteReaderBase = getattr(tofu_search, 'SiteReader', object)


class _ChatuiKmDocReader(_SiteReaderBase):
    """Reroute 学城 (km.internal.example.com) doc URLs through the xuecheng MCP server.

    An anonymous fetch of a KM doc URL only ever returns the SSO login wall
    (~80K chars of JS/config noise) — which poisoned fetch_url results. The
    xuecheng-mcp server reads the same URL under the user's identity. As a
    tofu-search READER this runs before the anonymous pipeline, so BOTH
    direct fetch_url and web_search result-fetching transparently return
    the real document instead of the wall.
    """

    name = 'km-doc'
    _TOOL = 'mcp__xuecheng__read_doc'
    _PATH_RE = re.compile(r'^/(?:collabpage|collaborate|page|docs?|xtable)/\d+')

    def matches(self, url):
        try:
            parts = urlparse(str(url))
        except Exception:
            return False
        host = parts.netloc.lower().split('@')[-1].split(':')[0]
        return host == 'km.internal.example.com' and bool(self._PATH_RE.match(parts.path))

    def read(self, url, *, max_chars=None, timeout=15):
        try:
            from lib.mcp import get_bridge
            bridge = get_bridge()
        except Exception as e:
            logger.debug('[KMReader] MCP bridge unavailable: %s', e)
            return None
        args = {'doc': url}
        if max_chars:
            args['max_chars'] = int(max_chars)
        try:
            text = bridge.call_tool(
                self._TOOL, args,
                timeout_override=max(int(timeout or 15), 30))
        except ValueError as e:
            # Server unconfigured / tool disabled — legacy anonymous path.
            logger.debug('[KMReader] %s', e)
            return None
        except Exception as e:
            # Matched but the reroute failed: an anonymous retry only yields
            # the login wall, so surface the failure instead of falling through.
            logger.info('[KMReader] read_doc reroute failed for %s: %s',
                        url_for_log(url), text_for_log(e))
            return (f'[学城文档自动读取失败] {e} '
                    f'可改用 xuecheng 的 read_doc 工具重试。原始链接: {url}')
        if not text:
            return (f'[学城文档] xuecheng read_doc 未返回内容'
                    f'（文档可能为空）。原始链接: {url}')
        return text


# ═══════════════════════════════════════════════════════
#  Site-knowledge seam — lib.site_knowledge (tofu-search >=0.7.1)
# ═══════════════════════════════════════════════════════

# Soft floor: on tofu-search <0.7.1 the base class does not exist; degrading
# to `object` keeps this module importable and the provider simply never gets
# registered (install guards with hasattr).
_SiteKnowledgeBase = getattr(tofu_search, 'SiteKnowledgeProvider', object)


class _ChatuiSiteKnowledgeProvider(_SiteKnowledgeBase):
    """Routes tofu-search engine knowledge lookups to chatui's per-site store.

    Entries are doctor-pinned OVERRIDES; absent → engine built-ins serve.
    """

    def get_knowledge(self, domain):
        try:
            from lib.site_knowledge import get_knowledge
            return get_knowledge(domain)
        except Exception as e:
            logger.debug('[Bridge] site-knowledge lookup failed for %s: %s',
                         domain, e)
            return None


# ═══════════════════════════════════════════════════════
#  Config sync + install
# ═══════════════════════════════════════════════════════

def _resolve_proxy_url() -> str:
    """Return chatui's effective HTTPS/HTTP proxy URL, or '' when none.

    Prefers the proxy pool's first VERIFIABLY-ALIVE GLOBAL entry, then the
    Settings-resolved legacy value from ``lib.proxy`` (which also mirrors
    the env vars) so tofu-search's adaptive dual-attempt tries the SAME
    proxy chatui itself uses, independent of env-var casing quirks.

    Why ``first_reachable_global_proxy_url`` (TCP + real-HTTPS probe, 60s
    positive cache) and not ``first_global_proxy_url``: pool health is fed
    by real app traffic only, and tofu-search never reports outcomes back
    to the pool — so a DEAD first entry (hk-gw outage, 2026-08-20: every
    engine ProxyError → direct fallback → no direct egress → 0 results
    misreported as "no matches") kept being handed out forever. The probe
    walks the pool in failover order and skips entries that refuse
    connections RIGHT NOW, landing on the next alive one (legacy proxy).
    Blocking cost: at most one ~3s probe per endpoint per 60s window.
    """
    try:
        from lib.proxy import first_reachable_global_proxy_url
        pooled = first_reachable_global_proxy_url()
        if pooled:
            return pooled
    except Exception as e:
        logger.debug('[Bridge] reachable pool proxy resolve failed: %s', e)
    # Every probe failed. That USUALLY means the pool is down — but a
    # blocked canary URL would look identical while the proxies themselves
    # still work, so fall back to the health-trusting pick as a last
    # resort. A genuinely-dead entry handed out here fails fast per engine
    # (ProxyError) and is now correctly DIAGNOSED as a network error
    # (tofu-search engine-failure classification), never as "no matches".
    try:
        from lib.proxy import first_global_proxy_url
        pooled = first_global_proxy_url()
        if pooled:
            return pooled
    except Exception as e:
        logger.debug('[Bridge] pool proxy resolve failed: %s', e)
    try:
        from lib.proxy import get_proxy_config
        cfg = get_proxy_config()
        return (cfg.get('https_proxy') or cfg.get('http_proxy') or '').strip()
    except Exception as e:
        logger.debug('[Bridge] proxy resolve failed: %s', e)
        return ''


def sync_search_config():
    """Push chatui's live FETCH_* settings into tofu-search's global config."""
    filter_enabled = getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True)
    filter_min_chars = max(1000, min(
        100_000, int(os.environ.get('FETCH_FILTER_MIN_CHARS', '6000'))))
    gate_input_max_chars = max(1000, min(
        12_000, int(os.environ.get('FETCH_FILTER_GATE_MAX_CHARS', '6000'))))
    filter_mode = os.environ.get('FETCH_FILTER_MODE', 'gate')
    proxy_url = _resolve_proxy_url()
    # The FULL global-pool failover chain behind the primary: a primary that
    # dies mid-run (hk-gw tunnel-403, 2026-08-20) no longer empties search —
    # tofu-search races the remaining entries + the direct path in parallel.
    proxy_failover = []
    try:
        from lib.proxy import global_proxy_failover_urls
        proxy_failover = [u for u in global_proxy_failover_urls() if u != proxy_url]
    except Exception as e:
        logger.debug('[Bridge] proxy failover list resolve failed: %s', e)

    # ── Pre-fetch relevance gate (tofu-search >=0.3.2) ──
    # These three knobs have NO env-var fallback inside tofu_search.configure(),
    # so unless the bridge passes them explicitly they are un-tunable from
    # chatui and silently run the library defaults. Wire them through here.
    prefetch_gate_enabled = _env_bool('PREFETCH_GATE_ENABLED',
                                      getattr(_lib, 'PREFETCH_GATE_ENABLED', True))
    prefetch_gate_min_query_terms = int(os.environ.get('PREFETCH_GATE_MIN_QUERY_TERMS', '2'))
    prefetch_gate_min_fetch = int(os.environ.get('PREFETCH_GATE_MIN_FETCH', '3'))
    # ── Adaptive dual-path proxy (tofu-search >=0.4.1) ──
    # configure() DOES auto-read TOFU_SEARCH_PROXY_DUAL_ATTEMPT from env, but we
    # pass it explicitly so the effective value is visible in the log line below
    # and stays parity with the other knobs (default on = try proxied↔direct).
    proxy_dual_attempt = _env_bool('TOFU_SEARCH_PROXY_DUAL_ATTEMPT', True)

    # ── Wall-clock deadlines (tofu-search >=0.5) ──
    # configure() auto-reads these from env too, but pass them explicitly so the
    # effective values are visible in the log line below and stay tunable from
    # chatui. Safe defaults match the library (45s whole-call / 25s per-URL);
    # 0 restores the legacy unbounded behaviour.
    search_deadline_secs = int(os.environ.get('TOFU_SEARCH_DEADLINE_SECS', '45'))
    fetch_url_deadline_secs = int(os.environ.get('TOFU_SEARCH_FETCH_URL_DEADLINE_SECS', '25'))

    # ── Security posture (tofu-search >=0.5.3) ──
    # These had NEITHER an env fallback inside configure() NOR a bridge kwarg,
    # so chatui could not express "fetch this internal host" or audit the
    # effective posture without editing library source. Defaults keep the
    # shipped fail-safe behaviour; the allowlist is empty unless asked for.
    #
    # allow_private_hosts is anchored on the HOSTNAME, never a resolved IP:
    # an internal load balancer rotates its address between lookups (one
    # observed host answered as both 10.176.18.71 and 10.192.19.176 minutes
    # apart), so an IP allowlist rots silently while the hostname stays true.
    #
    # SOURCE OF TRUTH is the Settings store (data/config/private_hosts.json).
    # The env var remains only as a bootstrap/CI fallback: a capability that
    # WORKS ONLY via an env var is broken in an exported copy, because export.py
    # does not carry the environment. Never make env the sole source.
    allow_private_hosts = _store_private_hosts()
    if not allow_private_hosts:
        allow_private_hosts = set(
            _env_csv('TOFU_SEARCH_ALLOW_PRIVATE_HOSTS')
            or getattr(_lib, 'SEARCH_ALLOW_PRIVATE_HOSTS', None) or [])
    block_private_addresses = _env_bool('TOFU_SEARCH_BLOCK_PRIVATE_ADDRESSES', True)
    allow_insecure_ssl_fallback = _env_bool('TOFU_SEARCH_ALLOW_INSECURE_SSL', False)
    min_request_interval_ms = int(os.environ.get('TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS', '400'))
    # Public SearXNG instances churn; an empty override leaves the library list.
    searxng_instances = _env_csv('TOFU_SEARCH_SEARXNG_INSTANCES')

    _cfg = dict(
        llm_function=_chatui_llm,
        fetch_top_n=_lib.FETCH_TOP_N,
        fetch_timeout=_lib.FETCH_TIMEOUT,
        search_deadline_secs=search_deadline_secs,
        fetch_url_deadline_secs=fetch_url_deadline_secs,
        fetch_max_chars_search=_lib.FETCH_MAX_CHARS_SEARCH,
        fetch_max_chars_direct=_lib.FETCH_MAX_CHARS_DIRECT,
        fetch_max_chars_pdf=_lib.FETCH_MAX_CHARS_PDF,
        fetch_max_bytes=_lib.FETCH_MAX_BYTES,
        skip_domains=set(_lib.SKIP_DOMAINS),
        filter_enabled=filter_enabled,
        filter_min_chars=filter_min_chars,
        # 45s matches the library default since 0.6.0 (was 300): on timeout the
        # raw text is served — filtering is an enhancement, never a blocker.
        filter_timeout=int(os.environ.get('FETCH_FILTER_TIMEOUT', '45')),
        # tofu-search >=0.6.0: 'gate' (verdict-only, capped input, original text
        # kept — fast) vs 'rewrite' (pre-0.6 full-page regeneration, 10-60s+/page).
        # Passed UNCONDITIONALLY like the other knobs → requirements floor 0.6.0.
        filter_mode=filter_mode,
        gate_input_max_chars=gate_input_max_chars,
        proxy_dual_attempt=proxy_dual_attempt,
        prefetch_gate_enabled=prefetch_gate_enabled,
        prefetch_gate_min_query_terms=prefetch_gate_min_query_terms,
        prefetch_gate_min_fetch=prefetch_gate_min_fetch,
        allow_private_hosts=allow_private_hosts,
        block_private_addresses=block_private_addresses,
        allow_insecure_ssl_fallback=allow_insecure_ssl_fallback,
        min_request_interval_ms=min_request_interval_ms,
        deepen_enabled=bool(getattr(_lib, 'SEARCH_DEEPEN_ENABLED', False)),
    )
    # Pass proxy_url ONLY when we resolved one. configure() applies its env
    # default just for fields ABSENT from kwargs, so an explicit '' would
    # suppress TOFU_SEARCH_PROXY_URL — the opposite of "no proxy configured
    # here, fall back to the environment".
    if proxy_url:
        _cfg['proxy_url'] = proxy_url
    # Multi-proxy failover chain + parallel racing (tofu-search >=0.10.0).
    # SOFT floor: introspect the INSTALLED library — an older tofu-search
    # simply doesn't receive these kwargs (feature inert, nothing crashes),
    # mirroring the min_request_interval_ms precedent.
    _ts_fields = getattr(tofu_search.SearchConfig, '__dataclass_fields__', {})
    if proxy_failover and 'proxy_fallback_urls' in _ts_fields:
        _cfg['proxy_fallback_urls'] = proxy_failover
    if 'proxy_race' in _ts_fields:
        _cfg['proxy_race'] = _env_bool('TOFU_SEARCH_PROXY_RACE', True)
    if searxng_instances:
        _cfg['searxng_instances'] = searxng_instances

    tofu_search.configure(**_cfg)
    logger.info('[Bridge] tofu-search config synced: top_n=%d timeout=%ds '
                'deadline(call=%ds url=%ds) '
                'max_chars(search=%d direct=%d pdf=%d) '
                'filter=%s(mode=%s min=%d gate_chars=%d) model=%r proxy=%s failover=%d '
                'dual_attempt=%s prefetch_gate=%s(terms>=%d,floor=%d) '
                'ssrf_guard=%s allow_private_hosts=%s insecure_ssl=%s '
                'throttle=%dms searxng=%s',
                _lib.FETCH_TOP_N, _lib.FETCH_TIMEOUT,
                search_deadline_secs, fetch_url_deadline_secs,
                _lib.FETCH_MAX_CHARS_SEARCH, _lib.FETCH_MAX_CHARS_DIRECT,
                _lib.FETCH_MAX_CHARS_PDF,
                'on' if filter_enabled else 'off',
                filter_mode, filter_min_chars, gate_input_max_chars,
                _FILTER_MODEL or 'dispatch-default',
                'set' if proxy_url else 'env/none',
                len(proxy_failover),
                'on' if proxy_dual_attempt else 'off',
                'on' if prefetch_gate_enabled else 'off',
                prefetch_gate_min_query_terms, prefetch_gate_min_fetch,
                'on' if block_private_addresses else 'OFF',
                ','.join(sorted(allow_private_hosts)) or 'none',
                'ALLOWED' if allow_insecure_ssl_fallback else 'off',
                min_request_interval_ms,
                ('%d override' % len(searxng_instances)) if searxng_instances else 'library')


def install_search_bridge():
    """Install chatui's LLM + provider implementations into tofu-search.

    Idempotent and concurrency-safe. Configuration reload has a separate
    explicit sync path, so repeat capability calls return without re-reading
    settings or mutating provider globals.
    """
    global _installed
    if _installed:
        return
    with _install_lock:
        if _installed:
            return
        sync_search_config()
        tofu_search.register_browser_provider(_ChatuiBrowserProvider())
        if hasattr(tofu_search, 'register_site_search_provider'):
            tofu_search.register_site_search_provider(_ChatuiSiteSearchProvider())
        tofu_search.register_auth_source_provider(_ChatuiAuthSourceProvider())
        # Reader tier: km.internal.example.com doc URLs reroute to the xuecheng MCP
        # server (user-identity read) instead of the anonymous login wall.
        if hasattr(tofu_search, 'register_reader'):
            tofu_search.register_reader(_ChatuiKmDocReader())
        # tofu-search >=0.7.1: doctor-pinned selector knowledge + the drift
        # signal that feeds the autofix loop (lib/site_doctor.py). Older
        # libraries lack both entry points — the hasattr guards keep the
        # soft-floor contract (feature inert, nothing crashes).
        if hasattr(tofu_search, 'register_site_knowledge_provider'):
            tofu_search.register_site_knowledge_provider(
                _ChatuiSiteKnowledgeProvider())
        if hasattr(tofu_search, 'register_site_drift_listener'):
            from lib import site_doctor
            tofu_search.register_site_drift_listener(site_doctor.on_site_drift)
        _installed = True
        logger.info('[Bridge] tofu-search bridge installed '
                    '(LLM=dispatch_chat, browser=extension, sites=adapters, '
                    'auth=auth_sources)')
