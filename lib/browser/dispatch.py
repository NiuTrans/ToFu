"""Strict dispatch boundary for the current browser tool catalogue."""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict

from lib.browser.handlers import (
    _handle_click,
    _handle_close_tab,
    _handle_devtools,
    _handle_execute_js,
    _handle_get_cookies,
    _handle_get_history,
    _handle_list_tabs,
    _handle_navigate,
    _handle_press_key,
    _handle_preview_page,
    _handle_read_page,
    _handle_research_page,
    _handle_screenshot,
    _handle_type,
)
from lib.browser.tool_runtime import BrowserToolRuntime
from lib.log import get_logger
from lib.tools.result_envelope import typed_tool_error

logger = get_logger(__name__)

__all__ = ['BROWSER_HANDLERS', 'execute_browser_tool', 'normalize_browser_args']


# Per-owner sticky device for calls that arrive WITHOUT a cfg pin. Two Chrome
# instances of the same owner poll every 1-2s, so 'most recent poll wins'
# makes consecutive calls hop between machines — and a tab id learned on one
# instance is meaningless (or worse, collides) on the other. Keep using the
# owner's last device while it stays connected; re-pick only when it drops.
_STICKY_LOCK = threading.Lock()
_STICKY_CLIENTS: OrderedDict[str, str] = OrderedDict()


def _sticky_capacity() -> int:
    from lib.browser.queue._limits import client_registry_limits
    process_limit, _owner_limit = client_registry_limits()
    return process_limit


def _pick_connected_client(owner_user_id, owned):
    owned_ids = {str(row.get('client_id') or '') for row in owned}
    with _STICKY_LOCK:
        sticky = _STICKY_CLIENTS.get(owner_user_id)
        if sticky and sticky in owned_ids:
            _STICKY_CLIENTS.move_to_end(owner_user_id)
            return sticky
        _STICKY_CLIENTS.pop(owner_user_id, None)
        chosen = str(max(
            owned, key=lambda row: row.get('last_poll', 0)
        ).get('client_id') or '')
        if chosen:
            _STICKY_CLIENTS[owner_user_id] = chosen
            capacity = max(1, int(_sticky_capacity()))
            while len(_STICKY_CLIENTS) > capacity:
                _STICKY_CLIENTS.popitem(last=False)
        return chosen


def _clear_sticky_clients() -> int:
    """Test/maintenance hook: drop all remembered device routes."""
    with _STICKY_LOCK:
        dropped = len(_STICKY_CLIENTS)
        _STICKY_CLIENTS.clear()
        return dropped


# Tool schemas use snake_case; the extension wire uses camelCase. This is the
# one translation boundary. Direct camelCase tool arguments are rejected so
# there is one callable contract for models and tests.
_SNAKE_TO_WIRE = {
    'tab_id': 'tabId',
    'tab_ids': 'tabIds',
    'max_chars': 'maxChars',
    'max_results': 'maxResults',
    'max_elements': 'maxElements',
    'max_scrolls': 'maxScrolls',
    'max_pages': 'maxPages',
    'observe_ms': 'observeMs',
    'max_depth': 'maxDepth',
    'session_ttl_ms': 'sessionTtlMs',
    'await_promise': 'awaitPromise',
    'context_id': 'contextId',
    'source_url': 'sourceUrl',
    'line_number': 'lineNumber',
    'column_number': 'columnNumber',
    'breakpoint_id': 'breakpointId',
    'call_frame_id': 'callFrameId',
    'script_id': 'scriptId',
    'session_id': 'sessionId',
    'full_page': 'fullPage',
    'right_click': 'rightClick',
    'scroll_to': 'scrollTo',
    'wait_for_load': 'waitForLoad',
    'wait_ms': 'waitMs',
    'new_tab': 'newTab',
    'clear_first': 'clearFirst',
}


def normalize_browser_args(fn_args):
    """Validate model arguments and translate schema names to wire names."""
    if not isinstance(fn_args, dict):
        raise ValueError('Browser tool arguments must be an object')
    wire_keys = sorted(set(fn_args) & set(_SNAKE_TO_WIRE.values()))
    if wire_keys:
        raise ValueError(
            'Browser tool arguments must use snake_case: '
            + ', '.join(wire_keys))
    out = dict(fn_args)
    for schema_name, wire_name in _SNAKE_TO_WIRE.items():
        if schema_name in out:
            out[wire_name] = out.pop(schema_name)
    return out


def _handle_advanced_tool(fn_name, fn_args, runtime):
    """Run a compound interaction under one owner/device runtime."""
    if fn_name not in {'browser_menu_click', 'browser_fill_form'}:
        return f'Error: Unknown browser tool: {fn_name}'

    from lib.browser._resolve import (
        action_receipt, resolve_work_tab, tab_snapshot,
    )
    from lib.browser.advanced import fill_form_sequential, menu_click

    try:
        tab_id = resolve_work_tab(
            fn_args, route_key=runtime.route_key, send=runtime.send)
        if tab_id is None:
            return ('Error: no tab to act on. Pass tab_id, or call '
                    'browser_list_tabs / browser_navigate first.')
        before = tab_snapshot(tab_id, send=runtime.send)
        if fn_name == 'browser_menu_click':
            result = menu_click(
                tab_id=tab_id,
                item_text=fn_args.get('item_text', ''),
                target_selector=fn_args.get('target_selector'),
                target_text=fn_args.get('target_text'),
                via=fn_args.get('via', 'hover'),
                submenu_item_text=fn_args.get('submenu_text'),
                menu_wait=fn_args.get('menu_wait', 0.5),
                timeout=fn_args.get('timeout', 5.0),
                send=runtime.send,
            )
        elif fn_name == 'browser_fill_form':
            result = fill_form_sequential(
                tab_id=tab_id,
                fields=fn_args.get('fields', []),
                submit_selector=fn_args.get('submit_selector'),
                field_delay=fn_args.get('field_delay', 0.2),
                submit_text=fn_args.get('submit_text'),
                send=runtime.send,
            )
        receipt = action_receipt(
            tab_id,
            before,
            route_key=runtime.route_key,
            send=runtime.send,
        )
        if not isinstance(result, dict):
            return str(result) + receipt
        if result.get('success'):
            steps = result.get('steps_completed', '?')
            details = result.get('details', {})
            parts = [f'{fn_name} succeeded ({steps} steps)']
            if details:
                parts.append(json.dumps(details, ensure_ascii=False, indent=2))
            return '\n'.join(parts) + receipt
        return (
            f'{fn_name} failed: {result.get("error", "unknown error")} '
            f'(completed {result.get("steps_completed", 0)} steps)'
            + receipt
        )
    except Exception as exc:
        logger.warning(
            'Browser tool %s error: %s', fn_name, exc, exc_info=True)
        return f'{fn_name} error: {exc}'


# Extension-backed handlers share ``handler(arguments, runtime)``. The page
# preview is a server renderer and is called separately below.
BROWSER_HANDLERS = {
    'browser_list_tabs': _handle_list_tabs,
    'browser_read_page': _handle_read_page,
    'browser_research_page': _handle_research_page,
    'browser_devtools': _handle_devtools,
    'browser_execute_js': _handle_execute_js,
    'browser_screenshot': _handle_screenshot,
    'browser_click': _handle_click,
    'browser_type': _handle_type,
    'browser_press_key': _handle_press_key,
    'browser_navigate': _handle_navigate,
    'browser_close_tab': _handle_close_tab,
    'browser_get_cookies': _handle_get_cookies,
    'browser_get_history': _handle_get_history,
    'browser_menu_click': (
        lambda fn_args, runtime: _handle_advanced_tool(
            'browser_menu_click', fn_args, runtime)),
    'browser_fill_form': (
        lambda fn_args, runtime: _handle_advanced_tool(
            'browser_fill_form', fn_args, runtime)),
    'browser_preview_page': _handle_preview_page,
}


_WORKING_TAB_TOOLS = frozenset({
    'browser_read_page',
    'browser_execute_js',
    'browser_devtools',
    'browser_click',
    'browser_type',
    'browser_press_key',
    'browser_menu_click',
    'browser_fill_form',
})

_ORIGIN_RELATIVE_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*(['\"`])/(?!/)", re.IGNORECASE,
)


def _origin_relative_js_requires_explicit_tab(fn_name: str, fn_args: dict) -> bool:
    if fn_name != 'browser_execute_js' or fn_args.get('tabId') is not None:
        return False
    return bool(_ORIGIN_RELATIVE_FETCH_RE.search(str(fn_args.get('code') or '')))


def _explicit_browser_tab_error() -> str:
    return typed_tool_error(
        'browser_explicit_tab_required',
        retryable=True,
        message=(
            'browser_execute_js contains an origin-relative fetch, but tab_id '
            'was omitted. The server will not guess which page origin to use.'),
        next_action=(
            'Call browser_list_tabs, choose the intended site, and retry with '
            'explicit tab_id. For read-only extraction, use '
            'browser_research_page with the full URL.'),
    ).to_envelope_text()


def _terminal_access_error(
    code: str,
    exc: Exception,
    *,
    fn_name: str,
    fn_args: dict | None,
) -> str:
    """Serialize a stable, non-retryable browser authority failure."""
    args = fn_args or {}
    domain = str(getattr(exc, 'domain', '') or 'unknown-domain')
    tab_id = args.get('tabId', args.get('tab_id'))
    target = f'tab #{tab_id} on {domain}' if tab_id is not None else domain
    if code == 'browser_write_authorization_required':
        message = str(exc)
        if fn_name == 'browser_execute_js':
            message = (
                'browser_execute_js is treated as a browser write because '
                'arbitrary JavaScript can mutate the page. Write '
                f'authorization is missing for {target}.')
        next_action = (
            f'Check that {target} is intended. If wrong, call '
            'browser_list_tabs and retry with explicit tab_id. If correct, '
            'ask the user to grant browser access. For read-only work use '
            'browser_read_page or browser_research_page.')
    else:
        message = str(exc)
        next_action = (
            f'Stop this browser action and verify the owner, device, and '
            f'access policy for {target} before retrying.')
    return typed_tool_error(
        code,
        retryable=False,
        next_action=next_action,
        message=message,
    ).to_envelope_text()


def execute_browser_tool(
    fn_name,
    fn_args,
    *,
    owner_user_id: str,
    client_id=None,
):
    """Execute one catalogued browser tool under explicit owner authority."""
    caller_owner = str(owner_user_id or '').strip()
    if not caller_owner.isdigit() or int(caller_owner) < 1:
        return 'Error: Browser tool requires an authenticated owner'
    handler = BROWSER_HANDLERS.get(str(fn_name or ''))
    if handler is None:
        logger.warning('Unknown browser tool requested: %s', fn_name)
        return f'Error: Unknown browser tool: {fn_name}'
    try:
        normalized_args = normalize_browser_args(fn_args)
    except ValueError as exc:
        return f'Error: {exc}'

    if fn_name == 'browser_preview_page':
        return handler(normalized_args)

    if _origin_relative_js_requires_explicit_tab(fn_name, normalized_args):
        return _explicit_browser_tab_error()

    from lib.browser.queue import get_connected_clients

    owned = get_connected_clients(owner_user_id=caller_owner)
    owned_ids = {str(row.get('client_id') or '') for row in owned}
    if client_id and str(client_id) not in owned_ids:
        return 'Error: Browser client is not connected for this user'
    if not client_id and owned:
        client_id = _pick_connected_client(caller_owner, owned)
    if not client_id:
        return 'Error: No browser extension is connected for this user'

    runtime = BrowserToolRuntime(
        owner_user_id=caller_owner,
        client_id=str(client_id),
    )
    if (
        fn_name in _WORKING_TAB_TOOLS
        and normalized_args.get('tabId') is None
    ):
        # The access check and the handler must resolve the same target.  If
        # we left ``tabId`` absent here, access.py would inspect the browser's
        # active tab while the handler could later reuse this route's
        # remembered working tab, authorizing one domain and acting on
        # another. Resolve once under the immutable owner/device route and
        # pass that concrete target through both boundaries.
        from lib.browser._resolve import resolve_work_tab

        resolved_tab_id = resolve_work_tab(
            normalized_args,
            route_key=runtime.route_key,
            send=runtime.send,
        )
        if resolved_tab_id is not None:
            normalized_args = dict(normalized_args)
            normalized_args['tabId'] = resolved_tab_id
    try:
        from lib.browser.access import (
            BrowserAccessDenied,
            BrowserWriteAuthorizationRequired,
            browser_tool_access,
        )

        browser_tool_access(
            fn_name,
            normalized_args,
            owner_user_id=caller_owner,
            client_id=runtime.client_id,
        )
    except BrowserWriteAuthorizationRequired as exc:
        logger.debug('[Browser] write authorization required for %s: %s',
                     fn_name, exc)
        return _terminal_access_error(
            'browser_write_authorization_required', exc,
            fn_name=fn_name, fn_args=normalized_args)
    except BrowserAccessDenied as exc:
        logger.debug('[Browser] access denied for %s: %s', fn_name, exc)
        return _terminal_access_error(
            'browser_access_denied', exc,
            fn_name=fn_name, fn_args=normalized_args)
    except Exception as exc:
        logger.debug('[Browser] tool access check rejected %s: %s',
                     fn_name, exc)
        return f'Error: {exc}'
    return handler(normalized_args, runtime)
