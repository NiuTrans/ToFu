"""Resolve one page element to an exact browser-owned download URL.

This module is the narrow bridge between an LLM-facing ``text``/``selector``
target and the existing authenticated browser-to-server file transport.  It
only reads a bounded set of link-like DOM attributes with a server-authored
script; it never clicks, submits a form, parses page JavaScript, exposes cookie
values, or grants a URL from a different owner/device.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from lib.browser._resolve import resolve_element, resolve_work_tab
from lib.browser.access import BrowserAccessDenied, browser_tool_access
from lib.browser.tool_runtime import BrowserToolRuntime


@dataclass(frozen=True)
class BrowserDownloadTargetError(RuntimeError):
    """Typed, model-actionable failure while resolving a page link."""

    code: str
    message: str
    retryable: bool
    next_action: str

    def __str__(self) -> str:
        return self.message


def _owned_client(owner_user_id: str, requested_client_id: str) -> str:
    from lib.browser.queue import get_connected_clients

    clients = get_connected_clients(owner_user_id=owner_user_id)
    by_id = {str(row.get('client_id') or ''): row for row in clients}
    if requested_client_id:
        if requested_client_id not in by_id:
            raise BrowserDownloadTargetError(
                'browser_download_client_unavailable',
                'The selected browser is not connected for this user.',
                True,
                'Reconnect that browser or choose a connected browser, then retry.',
            )
        return requested_client_id
    if not clients:
        raise BrowserDownloadTargetError(
            'browser_download_browser_offline',
            'No browser extension is connected for this user.',
            True,
            'Connect the browser extension, keep the download page open, and retry.',
        )
    compatible = [
        row for row in clients
        if 'file_export' in set(row.get('capabilities') or ())
    ]
    selected = max(compatible or clients, key=lambda row: row.get('last_poll', 0))
    return str(selected.get('client_id') or '')


def _link_reader_script(selector: str) -> str:
    """Return a fixed read-only DOM projection with one data argument."""
    selector_json = json.dumps(selector, ensure_ascii=False)
    return f"""(() => {{
      const root = document.querySelector({selector_json});
      if (!root) return {{error: 'element_not_found'}};
      const link = root.matches('a[href], area[href]')
        ? root
        : (root.closest('a[href], area[href]') || root.querySelector('a[href], area[href]'));
      const candidates = [
        link && link.href,
        root.getAttribute('data-download-url'),
        root.getAttribute('data-href'),
        root.getAttribute('data-url')
      ].filter(Boolean);
      if (!candidates.length) return {{error: 'no_download_url'}};
      try {{
        const resolved = new URL(candidates[0], document.baseURI);
        if (!['http:', 'https:'].includes(resolved.protocol))
          return {{error: 'unsupported_scheme'}};
        return {{
          url: resolved.href,
          tag: root.tagName.toLowerCase(),
          text: (root.innerText || root.textContent || '').trim().slice(0, 120)
        }};
      }} catch (_error) {{
        return {{error: 'invalid_download_url'}};
      }}
    }})()"""


def resolve_browser_download_element(
    *,
    owner_user_id: str,
    client_id: str = '',
    tab_id: object = None,
    text: str = '',
    selector: str = '',
) -> tuple[str, str]:
    """Return ``(exact_http_url, resolved_client_id)`` for one page element."""
    owner = str(owner_user_id or '').strip()
    if not owner.isdigit() or int(owner) < 1:
        raise BrowserDownloadTargetError(
            'browser_download_invalid_owner',
            'An authenticated owner is required for browser download resolution.',
            False,
            'Retry from an authenticated conversation.',
        )
    selected_client = _owned_client(owner, str(client_id or '').strip())
    runtime = BrowserToolRuntime(owner_user_id=owner, client_id=selected_client)
    args = {'tabId': tab_id} if tab_id is not None else {}
    resolved_tab = resolve_work_tab(
        args, route_key=runtime.route_key, send=runtime.send)
    if resolved_tab is None:
        raise BrowserDownloadTargetError(
            'browser_download_tab_unavailable',
            'No browser tab is available for the download element.',
            True,
            'Open the download page or pass a tab_id from browser_list_tabs.',
        )

    # The fixed DOM read and the downstream browser fetch share this exact
    # owner/device/tab. Read policy therefore cannot authorize one page and
    # resolve a different remembered tab.
    try:
        browser_tool_access(
            'browser_read_page', {'tabId': resolved_tab},
            owner_user_id=owner, client_id=selected_client)
    except BrowserAccessDenied as exc:
        raise BrowserDownloadTargetError(
            'browser_download_access_denied',
            'Browser read access is denied for the download page.',
            False,
            'Grant read access for that site or provide an allowed direct URL.',
        ) from exc

    chosen_selector = str(selector or '').strip()
    query = str(text or '').strip()
    if len(chosen_selector) > 2048 or len(query) > 500:
        raise BrowserDownloadTargetError(
            'browser_download_target_too_long',
            'The browser element target exceeds its bounded input size.',
            False,
            'Use a shorter visible text label or CSS selector.',
        )
    if not chosen_selector:
        if not query:
            raise BrowserDownloadTargetError(
                'browser_download_target_missing',
                'Pass url, text, or selector to identify the remote file.',
                True,
                'Use browser_read_page(mode="elements") to identify the link.',
            )
        element, note, candidates = resolve_element(
            resolved_tab, query, 'clickable', send=runtime.send)
        if element is None:
            detail = '\n'.join(candidates[:6])
            message = f'Could not resolve download text {query!r}: {note}.'
            if detail:
                message += f' Closest elements:\n{detail}'
            raise BrowserDownloadTargetError(
                'browser_download_target_ambiguous',
                message,
                True,
                'Retry with a more specific text value or an exact selector.',
            )
        chosen_selector = str(element.get('selector') or '')

    result, error = runtime.send(
        'execute_js',
        {'tabId': resolved_tab, 'code': _link_reader_script(chosen_selector)},
        timeout=10,
    )
    if error:
        raise BrowserDownloadTargetError(
            'browser_download_target_read_failed',
            f'The browser could not read the download element: {error}',
            True,
            'Refresh the page, then retry the same text or selector.',
        )
    if not isinstance(result, dict) or result.get('error') or result.get('__error'):
        reason = str((result or {}).get('error') or (result or {}).get('message')
                     or 'no_download_url') if isinstance(result, dict) else 'invalid_result'
        raise BrowserDownloadTargetError(
            'browser_download_link_unavailable',
            f'The selected element does not expose a safe HTTP(S) download link ({reason}).',
            False,
            'Use an anchor/data-url element or provide the exact network URL. '
            'Form submissions and click-triggered downloads are not guessed.',
        )
    target_url = str(result.get('url') or '').strip()
    try:
        parsed = urlsplit(target_url)
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        raise BrowserDownloadTargetError(
            'browser_download_link_invalid',
            'The selected element resolved to an invalid remote URL.',
            False,
            'Choose a link whose destination is an HTTP(S) file.',
        )
    return target_url, selected_client


__all__ = ['BrowserDownloadTargetError', 'resolve_browser_download_element']
