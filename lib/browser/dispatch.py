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
    """Handler for advanced browser tools (menu click, fill form + legacies)."""
    from lib.browser.advanced import (
        fill_form_sequential, hover_and_click, menu_click,
        right_click_menu_select,
    )
    from lib.browser._resolve import resolve_work_tab
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
        # Format result dict
        if isinstance(result, dict):
            if result.get('success'):
                steps = result.get('steps_completed', '?')
                details = result.get('details', {})
                parts = [f'{fn_name} succeeded ({steps} steps)']
                if details:
                    parts.append(json.dumps(details, ensure_ascii=False, indent=2))
                return '\n'.join(parts)
            else:
                return f'{fn_name} failed: {result.get("error", "unknown error")} (completed {result.get("steps_completed", 0)} steps)'
        return str(result)
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


def execute_browser_tool(fn_name, fn_args, client_id=None):
    """Execute a browser tool call. Returns a string result for the LLM.

    Args:
        fn_name: Browser tool function name.
        fn_args: Tool arguments dict.
        client_id: Target browser extension client ID for per-device routing.
    """
    # Normalize snake_case LLM args → camelCase handler args (see
    # normalize_browser_args). Accepts legacy camelCase too.
    fn_args = normalize_browser_args(fn_args)
    # Store client_id in thread-local so send_browser_command can access it
    # without modifying every handler's signature.
    _set_active_client(client_id)
    handler = BROWSER_HANDLERS.get(fn_name)
    if handler is not None:
        return handler(fn_args)
    logger.warning("Unknown browser tool requested: %s", fn_name)
    return f'Error: Unknown browser tool: {fn_name}'
