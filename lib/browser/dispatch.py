"""lib/browser/dispatch.py — Dispatch table mapping tool names to handlers."""

import json

from lib.browser.handlers import (
    _handle_click,
    _handle_close_tab,
    _handle_create_tab,
    _handle_execute_js,
    _handle_get_app_state,
    _handle_get_cookies,
    _handle_get_history,
    _handle_get_interactive_elements,
    _handle_hover,
    _handle_keyboard,
    _handle_list_tabs,
    _handle_navigate,
    _handle_press_key,
    _handle_preview_page,
    _handle_read_page,
    _handle_read_tab,
    _handle_screenshot,
    _handle_summarize_page,
    _handle_type,
    _handle_wait,
)
from lib.browser.queue import _set_active_client, send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['BROWSER_HANDLERS', 'execute_browser_tool', 'normalize_browser_args']


# The LLM-facing browser schemas use snake_case (consistent with all other
# Tofu tools), but the handlers + extension wire protocol speak camelCase.
# This maps the snake_case keys the model now emits onto the camelCase the
# rest of the browser stack expects.  Old persisted conversations that still
# carry camelCase args pass through unchanged (the camelCase key already
# matches what handlers read).
_SNAKE_TO_CAMEL = {
    'tab_id': 'tabId',
    'tab_ids': 'tabIds',
    'max_chars': 'maxChars',
    'max_results': 'maxResults',
    'max_elements': 'maxElements',
    'full_page': 'fullPage',
    'right_click': 'rightClick',
    'scroll_to': 'scrollTo',
    'wait_for_load': 'waitForLoad',
    'wait_ms': 'waitMs',
    'new_tab': 'newTab',
    'clear_first': 'clearFirst',
}


def normalize_browser_args(fn_args):
    """Translate snake_case LLM args to the camelCase handlers expect.

    Returns a new dict; if a camelCase key is already present (legacy
    persisted args) it wins and the snake_case alias is dropped.
    """
    if not isinstance(fn_args, dict):
        return fn_args
    out = dict(fn_args)
    for snake, camel in _SNAKE_TO_CAMEL.items():
        if snake in out:
            if camel not in out:
                out[camel] = out[snake]
            del out[snake]
    return out


def _handle_advanced_tool(fn_name, fn_args):
    """Handler for advanced browser tools (menu click, fill form + legacies).

    v3 receipt seam (2026-08-05, owner review of pt_4ef0583e3ad44278): the
    four advanced flows call click_element DIRECTLY — and a menu item or a
    form SUBMIT are the two highest-frequency new-tab openers — so without
    this seam they bypass the base handlers' action receipt and the
    "clicked but nothing perceived" incident recurs. Snapshot the tab set
    before the flow, diff after it HERE: one seam covers all four flows
    without touching their internals. Patch points for tests: THIS module's
    send_browser_command serves the snapshot/receipt list_tabs calls;
    lib.browser.advanced's own module-level name serves the flow commands.
    """
    from lib.browser.advanced import (
        fill_form_sequential, hover_and_click, menu_click,
        right_click_menu_select,
    )
    from lib.browser._resolve import (
        action_receipt, resolve_work_tab, tab_snapshot,
    )
    try:
        # v2: tab_id optional on the shipped advanced tools — resolve the
        # working tab once here. Legacy names keep their explicit-id contract
        # (they only serve direct execute_browser_tool callers now).
        if fn_name in ('browser_menu_click', 'browser_fill_form'):
            tab_id = resolve_work_tab(fn_args, send_browser_command)
            if tab_id is None:
                return ('Error: no tab to act on. Pass tab_id, or call '
                        'browser_list_tabs / browser_navigate first.')
        else:
            tab_id = fn_args.get('tabId')
        # Pre-flow snapshot: the receipt diffs against this AFTER the flow.
        before = tab_snapshot(tab_id, send_browser_command) if tab_id is not None else None
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
            )
        elif fn_name == 'browser_right_click_menu':
            result = right_click_menu_select(
                tab_id=tab_id,
                target_selector=fn_args.get('target_selector', ''),
                menu_item_text=fn_args.get('menu_item_text', ''),
                submenu_item_text=fn_args.get('submenu_item_text'),
                menu_wait=fn_args.get('menu_wait', 0.5),
                timeout=fn_args.get('timeout', 5.0),
            )
        elif fn_name == 'browser_hover_and_click':
            result = hover_and_click(
                tab_id=tab_id,
                hover_selector=fn_args.get('hover_selector', ''),
                click_selector=fn_args.get('click_selector', ''),
                hover_wait=fn_args.get('hover_wait', 0.3),
                timeout=fn_args.get('timeout', 5.0),
            )
        elif fn_name == 'browser_fill_form':
            result = fill_form_sequential(
                tab_id=tab_id,
                fields=fn_args.get('fields', []),
                submit_selector=fn_args.get('submit_selector'),
                field_delay=fn_args.get('field_delay', 0.2),
                submit_text=fn_args.get('submit_text'),
            )
        else:
            return f'Error: Unknown advanced browser tool: {fn_name}'
        # Post-flow receipt: fires on success AND failure (a flow can open a
        # tab and still report failure, e.g. submenu item missing).
        receipt = ''
        if before is not None:
            receipt = action_receipt(tab_id, before, send_browser_command)
        # Format result dict
        if isinstance(result, dict):
            if result.get('success'):
                steps = result.get('steps_completed', '?')
                details = result.get('details', {})
                parts = [f'{fn_name} succeeded ({steps} steps)']
                if details:
                    parts.append(json.dumps(details, ensure_ascii=False, indent=2))
                return '\n'.join(parts) + receipt
            else:
                return f'{fn_name} failed: {result.get("error", "unknown error")} (completed {result.get("steps_completed", 0)} steps)' + receipt
        return str(result) + receipt
    except Exception as e:
        logger.warning("Browser tool %s error: %s", fn_name, e, exc_info=True)
        return f'{fn_name} error: {e}'


# Maps browser tool fn_name → handler(fn_args).
BROWSER_HANDLERS = {
    # ── v2 surface (shipped to the model) ──
    'browser_read_page':              _handle_read_page,
    'browser_type':                   _handle_type,
    'browser_press_key':              _handle_press_key,
    # ── v2 + legacy (legacy names keep working for direct callers; they are
    #    simply no longer in the model's schema list) ──
    'browser_list_tabs':              _handle_list_tabs,
    'browser_read_tab':               _handle_read_tab,
    'browser_execute_js':             _handle_execute_js,
    'browser_screenshot':             _handle_screenshot,
    'browser_get_cookies':            _handle_get_cookies,
    'browser_get_history':            _handle_get_history,
    'browser_create_tab':             _handle_create_tab,
    'browser_close_tab':              _handle_close_tab,
    'browser_navigate':               _handle_navigate,
    'browser_get_interactive_elements': _handle_get_interactive_elements,
    'browser_click':                  _handle_click,
    'browser_keyboard':               _handle_keyboard,
    'browser_hover':                  _handle_hover,
    'browser_wait':                   _handle_wait,
    'browser_summarize_page':         _handle_summarize_page,
    'browser_get_app_state':          _handle_get_app_state,
    # Server-side render — does NOT go through the extension queue
    # (lib/browser/preview.py, shared Playwright pool).
    'browser_preview_page':           _handle_preview_page,
    # Advanced browser tools use a lambda wrapper to pass fn_name through
    'browser_menu_click':             lambda fn_args: _handle_advanced_tool('browser_menu_click', fn_args),
    'browser_right_click_menu':       lambda fn_args: _handle_advanced_tool('browser_right_click_menu', fn_args),
    'browser_hover_and_click':        lambda fn_args: _handle_advanced_tool('browser_hover_and_click', fn_args),
    'browser_fill_form':              lambda fn_args: _handle_advanced_tool('browser_fill_form', fn_args),
}


def execute_browser_tool(fn_name, fn_args, client_id=None, user_id=None):
    """Execute a browser tool call. Returns a string result for the LLM.

    Args:
        fn_name: Browser tool function name.
        fn_args: Tool arguments dict.
        client_id: Target browser extension client ID for per-device routing.
        user_id: Owning task user. When supplied, client selection is confined
                 to that user's browser fleet before anything is enqueued.
    """
    # Normalize snake_case LLM args → camelCase handler args (see
    # normalize_browser_args). Accepts legacy camelCase too.
    fn_args = normalize_browser_args(fn_args)
    caller_user = None if user_id is None else str(user_id or '')
    if caller_user is not None:
        from lib.browser.queue import get_connected_clients
        owned = get_connected_clients(user_id=caller_user)
        owned_ids = {str(row.get('client_id') or '') for row in owned}
        if client_id and str(client_id) not in owned_ids:
            return 'Error: Browser client is not connected for this user'
        if not client_id and owned:
            client_id = str(max(
                owned, key=lambda row: row.get('last_poll', 0)
            ).get('client_id') or '')
    # Store the already-scoped client in thread-local so handlers route every
    # nested command to the same device.
    _set_active_client(client_id)
    # Policy is independent of the server-side SSRF allowlist: the URL is
    # resolved from the user's live tab and checked against the user's browser
    # read-deny/write-grant store.
    if fn_name != 'browser_preview_page':
        try:
            from lib.browser.access import browser_tool_access
            from lib.browser.queue import client_user_id
            from lib.browser.protocol import client_protocol
            resolved_client = client_protocol(client_id or None).get('client_id') or ''
            browser_tool_access(
                fn_name, fn_args,
                user_id=(client_user_id(resolved_client)
                         if caller_user is None else caller_user),
                client_id=resolved_client)
        except Exception as exc:
            logger.debug('[Browser] tool access check rejected %s: %s',
                         fn_name, exc)
            return f'Error: {exc}'
    handler = BROWSER_HANDLERS.get(fn_name)
    if handler is not None:
        return handler(fn_args)
    logger.warning("Unknown browser tool requested: %s", fn_name)
    return f'Error: Unknown browser tool: {fn_name}'
