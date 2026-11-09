"""lib/search/search_bridge.py — Wire Tofu host behavior into tofu-search's seams.

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
HTTP and task callers supply identity with :func:`bind_search_browser` before
execution; this module never reads an ambient HTTP authentication context.
"""

import os
import re
import threading
import time
from contextlib import contextmanager
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

from .browser_scope import (
    BrowserFileHandoffReady,
    bind_search_browser,
    browser_file_handoff_boundary,
    browser_file_handoff_enabled,
    claim_bound_browser_file,
    current_browser_identity as _bind_browser_identity,
    fetch_bound_browser_file,
    remember_bound_browser_file as _remember_bound_browser_file,
    require_bound_browser_file,
)

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
    model" behavior without re-raising). An elapsed read budget is raised as
    the builtin ``TimeoutError`` that tofu-search treats as budget expiry.
    """
    from lib.llm import ContentFilterError
    from lib.llm.llm_errors import ResponseReadTimeoutError
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
            **({'owner_user_id': kwargs['owner_user_id']} if kwargs.get('owner_user_id') is not None else {}),
            **({'model_budget_admit': kwargs['model_budget_admit']} if kwargs.get('model_budget_admit') is not None else {}),
            **output_budget,
        )
        return content or ''
    except ResponseReadTimeoutError as expiry:
        raise TimeoutError(str(expiry)) from expiry
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
def bind_search_reranking(reranker, *, cancel_event=None):
    """Borrow an explicit local scorer in this request's existing config scope.

    The caller owns the scorer and joins searches before closing it. No global
    configuration, provider authority or model storage is changed by this port.
    """
    from dataclasses import replace
    from tofu_search.config import bind_call_config, get_config
    config = get_config()
    if not hasattr(config, 'local_reranker'):
        raise RuntimeError('Local search ranking requires tofu-search >=0.12.23')
    if reranker is not None and not callable(getattr(reranker, 'score', None)):
        raise TypeError('local reranker must provide score()')
    with bind_call_config(replace(config, local_reranker=reranker,
                                 local_rerank_cancel_event=cancel_event)):
        yield


class _BoundSearchBrowser:
    """One captured device/task decision shared by both browser transports."""

    def __init__(self, *, user_id='', client_id='', profile='', bound=False,
                 knowledge_scope=None):
        self.user_id = str(user_id or '')
        self.client_id = str(client_id or '')
        self.profile = str(profile or '')
        self._bound = bool(bound)
        self.knowledge_scope = knowledge_scope

    def bind(self):
        if self._bound:
            return self
        user_id, client_id, profile = _bind_browser_identity()
        from lib.site_doctor.ownership import current_knowledge_scope
        scope = current_knowledge_scope()
        if scope is not None and not scope.matches_browser(user_id, client_id, profile):
            scope = None
        return type(self)(user_id=user_id, client_id=client_id,
                          profile=profile, bound=True,
                          knowledge_scope=scope)

    def _route(self) -> dict:
        return {
            'client_id': self.client_id,
            'owner_user_id': self.user_id,
        } if self._bound and self.client_id and self.user_id else {}


class _ChatuiBrowserProvider(_BoundSearchBrowser, tofu_search.BrowserProvider):
    """Routes tofu-search browser fallbacks through chatui's extension."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._learned_rule_selected = False

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
            handoff_enabled = browser_file_handoff_enabled()

            def remember_file(source_url, transfer_id):
                accepted = _remember_bound_browser_file(
                    source_url, transfer_id)
                if accepted:
                    accepted_handoff.append((source_url, transfer_id))
                return accepted

            result = fetch_url_via_browser(
                url,
                max_chars=max_chars or 50000,
                max_bytes=tofu_search.get_config().fetch_max_bytes,
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
               timeout=20, scrolls=0, require_script_grant=False):
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
        cancel_event = (self.knowledge_scope.task.get('abort_event')
                        if self.knowledge_scope is not None else None)
        command_route = {**self._route(), **({'cancel_event': cancel_event}
                         if cancel_event is not None else {})}
        script_access = 'write' if require_script_grant or self._learned_rule_selected else 'read'
        from lib.browser.access import require_access, require_existing_write_access
        def authorize_script(target):
            if script_access == 'write':
                return require_existing_write_access(self.user_id, target,
                    client_id=self.client_id, profile=self.profile)
            return require_access(self.user_id, target, access='read',
                client_id=self.client_id, profile=self.profile)
        tab_id = None
        try:
            if self._bound:
                authorize_script(url)
            res, err = send_browser_command(
                'create_tab', {'url': url, 'active': False},
                timeout=max(timeout, 25), **command_route)
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
                    timeout=max(timeout, 15) + 10, **command_route)
                if werr or not (isinstance(wres, dict) and wres.get('found')):
                    # Slow/partial renders may still carry the data — extract anyway.
                    logger.info('[Bridge] scrape selector %r not confirmed for %s — '
                                'extracting anyway', wait_selector,
                                url_for_log(url))
            if self._bound:
                tabs, terr = send_browser_command(
                    'list_tabs', {}, timeout=8, **command_route)
                current = next((row for row in (tabs or [])
                                if str(row.get('id')) == str(tab_id)), None) \
                    if not terr and isinstance(tabs, list) else None
                if current is None:
                    logger.warning('[Bridge] scrape cannot verify final tab URL for %s',
                                   url_for_log(url))
                    return None
                authorize_script(current.get('url') or '')
            for _ in range(max(0, int(scrolls))):
                send_browser_command(
                    'scroll_page',
                    {'tabId': tab_id, 'direction': 'bottom', 'pixels': 3000},
                    timeout=10, **command_route)
                if cancel_event is not None:
                    if cancel_event.wait(1.2):
                        return None
                else:
                    time.sleep(1.2)   # Existing lazy-load pause.
            res, err = send_browser_command(
                'execute_js', {'tabId': tab_id, 'code': extractor_js},
                timeout=max(timeout, 15), **command_route)
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


class _ChatuiSiteSearchProvider(_BoundSearchBrowser, _SiteSearchBase):
    """Expose ready read adapters to tofu-search without a host back-edge."""

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
            from lib.browser.adapters import (
                AdapterExecutionError, AdapterValidationError,
                get_adapter, invoke_adapter,
            )
            from lib.browser.protocol import BrowserUpgradeRequired
            if not self._bound or not self.client_id:
                return None
            adapter = get_adapter(source_id)
            if adapter is None:
                return None
            task_ports = {}
            if self.knowledge_scope is not None:
                task = self.knowledge_scope.task
                task_ports = {'task_id': task.get('id', ''),
                              'cancel_event': task.get('abort_event')}
            try:
                result = invoke_adapter(
                    adapter.id, 'search',
                    {'query': query, 'limit': max_results, 'pages': 1},
                    owner_user_id=self.user_id, client_id=self.client_id,
                    **task_ports)
            except (AdapterExecutionError, AdapterValidationError,
                    BrowserUpgradeRequired):
                # A recognized adapter owns pacing and account safety. Never
                # turn its refusal into permission for another transport.
                raise tofu_search.SiteSearchTerminalError() from None
            return result.get('result') if result.get('ok') else None
        except tofu_search.SiteSearchTerminalError:
            raise
        except Exception as exc:
            logger.warning('[Bridge] site-search %s failed: %s', source_id, exc)
            return None


# ═══════════════════════════════════════════════════════
#  Auth-source seam — lib.auth_sources
# ═══════════════════════════════════════════════════════

class _ChatuiAuthSourceProvider(tofu_search.AuthSourceProvider):
    """Routes tofu-search authenticated fetch through chatui's auth store."""

    def bind(self):
        """This stateless adapter rereads the configured registry on each lookup.

        The optional hook also works with the published 0.12.25 base class,
        which has no bind method. It does not cache credentials or grant access.
        """
        return self

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
        except Exception as exc:
            logger.debug('km-doc url parse failed: %s', type(exc).__name__)
            return False
        host = parts.netloc.lower().split('@')[-1].split(':')[0]
        return host == 'km.internal.example.com' and bool(self._PATH_RE.match(parts.path))

    def read(self, url, *, max_chars=None, timeout=15):
        try:
            from lib.mcp import get_bridge
            owner_user_id = _bind_browser_identity()[0]
            bridge = get_bridge(owner_user_id)
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


class _ChatuiConnectorDocReader(_SiteReaderBase):
    """Answer connector-only document links (腾讯文档, 金山文档, …) with guidance.

    An anonymous fetch of these links returns a JavaScript shell or a login
    redirect, never the document. The catalog's ``document_url_prefixes``
    name them; this reader returns the connector's setup steps while the
    owner has not installed it and a pointer to its tools once they have, so
    the model can tell the user how to connect instead of reporting an empty
    page. A host the owner connected as an auth source keeps that path.
    """

    name = 'mcp-connector-doc'

    def matches(self, url):
        from lib.mcp.connector_guidance import connector_for_document_url
        return connector_for_document_url(url) is not None

    def read(self, url, *, max_chars=None, timeout=15):
        try:
            from lib.auth_sources import match_source
            if match_source(url):
                return None
        except Exception as exc:
            logger.debug('[ConnectorDocReader] auth-source lookup failed: %s',
                         type(exc).__name__)
        from lib.mcp.connector_guidance import document_link_guidance
        return document_link_guidance(_bind_browser_identity()[0], url)


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
            from tofu_search.providers import get_browser_provider
            browser = get_browser_provider()
            scope = getattr(browser, 'knowledge_scope', None)
            entry = get_knowledge(domain, repository=scope.repository) if scope is not None else None
            if entry is not None:
                from lib.browser.access import require_existing_write_access
                require_existing_write_access(scope.repository.owner_id, domain,
                               client_id=scope.binding.client_id, profile=scope.binding.profile)
                browser._learned_rule_selected = True
            return entry
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
    """Push the host's providers, network and access policy into tofu-search.

    Result counts, page sizes, timeouts and download limits are tofu-search's
    own defaults (``SearchConfig``); the host does not re-declare them.
    """
    # HTTP and anonymous browser contexts resolve the current host policy at
    # call time. Do not probe a canary or retain a second proxy failover chain.
    proxy_url, proxy_failover = '', []

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
        search_deadline_secs=search_deadline_secs,
        fetch_url_deadline_secs=fetch_url_deadline_secs,
        skip_domains=set(_lib.SKIP_DOMAINS),
        fetch_skip_domains=set(_lib.FETCH_SKIP_DOMAINS),
        # The per-page LLM relevance gate stays off: measured verdicts dropped
        # the official docs a fetch asked for (5 of 7 judged irrelevant), and
        # each judged page cost a model call plus 1.5-3.6 s. Search output is
        # already lexically ranked and cut to query-focused excerpts.
        filter_enabled=False,
        proxy_dual_attempt=proxy_dual_attempt,
        prefetch_gate_enabled=prefetch_gate_enabled,
        prefetch_gate_min_query_terms=prefetch_gate_min_query_terms,
        prefetch_gate_min_fetch=prefetch_gate_min_fetch,
        allow_private_hosts=allow_private_hosts,
        block_private_addresses=block_private_addresses,
        allow_insecure_ssl_fallback=allow_insecure_ssl_fallback,
        min_request_interval_ms=min_request_interval_ms,
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
    # Keyed search gateway (tofu-search gateway engine). Always pass every
    # field: configure() is cumulative, so a disabled gateway must clear them.
    gateway_cfg = {}
    if 'gateway_url' in _ts_fields:
        from lib.search.search_gateway import engine_settings
        gateway_cfg = engine_settings((getattr(_lib, '_SAVED_CONFIG', None) or {}).get('search'))
        _cfg.update(gateway_cfg)

    from lib.search.network_transport import (
        requests_transport, httpx_client_factory, browser_proxy_resolver)
    _cfg['requests_transport'] = requests_transport
    _cfg['httpx_client_factory'] = httpx_client_factory
    _cfg['browser_proxy_resolver'] = browser_proxy_resolver
    tofu_search.configure(**_cfg)
    effective = tofu_search.get_config()
    logger.info('[Bridge] tofu-search config synced: top_n=%d timeout=%ds '
                'deadline(call=%ds url=%ds) '
                'max_chars(search=%d direct=%d pdf=%d) proxy=%s failover=%d '
                'dual_attempt=%s prefetch_gate=%s(terms>=%d,floor=%d) '
                'ssrf_guard=%s allow_private_hosts=%s insecure_ssl=%s '
                'throttle=%dms searxng=%s gateway=%s',
                effective.fetch_top_n, effective.fetch_timeout,
                search_deadline_secs, fetch_url_deadline_secs,
                effective.fetch_max_chars_search, effective.fetch_max_chars_direct,
                effective.fetch_max_chars_pdf,
                'set' if proxy_url else 'env/none',
                len(proxy_failover),
                'on' if proxy_dual_attempt else 'off',
                'on' if prefetch_gate_enabled else 'off',
                prefetch_gate_min_query_terms, prefetch_gate_min_fetch,
                'on' if block_private_addresses else 'OFF',
                ','.join(sorted(allow_private_hosts)) or 'none',
                'ALLOWED' if allow_insecure_ssl_fallback else 'off',
                min_request_interval_ms,
                ('%d override' % len(searxng_instances)) if searxng_instances else 'library',
                ('%d sources, free_engines=%s, full_content=%s' % (
                    len(gateway_cfg['gateway_sources']), gateway_cfg['free_engines'],
                    gateway_cfg['gateway_full_content']))
                if gateway_cfg.get('gateway_url') else 'off')


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
            tofu_search.register_reader(_ChatuiConnectorDocReader())
        # tofu-search >=0.7.1: doctor-pinned selector knowledge + the drift
        # signal that feeds the autofix loop (lib/site_doctor/). Older
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
