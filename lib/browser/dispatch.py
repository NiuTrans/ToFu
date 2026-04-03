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
    _handle_read_tab,
    _handle_screenshot,
    _handle_summarize_page,
    _handle_wait,
)
from lib.browser.queue import _set_active_client
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['BROWSER_HANDLERS', 'execute_browser_tool']


def _handle_advanced_tool(fn_name, fn_args):
    """Handler for advanced browser tools (right-click menu, hover+click, fill form)."""
    from lib.browser.advanced import fill_form_sequential, hover_and_click, right_click_menu_select
    try:
        if fn_name == 'browser_right_click_menu':
            result = right_click_menu_select(
                tab_id=fn_args.get('tabId'),
                target_selector=fn_args.get('target_selector', ''),
                menu_item_text=fn_args.get('menu_item_text', ''),
                submenu_item_text=fn_args.get('submenu_item_text'),
                menu_wait=fn_args.get('menu_wait', 0.5),
                timeout=fn_args.get('timeout', 5.0),
            )
        elif fn_name == 'browser_hover_and_click':
            result = hover_and_click(
                tab_id=fn_args.get('tabId'),
                hover_selector=fn_args.get('hover_selector', ''),
                click_selector=fn_args.get('click_selector', ''),
                hover_wait=fn_args.get('hover_wait', 0.3),
                timeout=fn_args.get('timeout', 5.0),
            )
        elif fn_name == 'browser_fill_form':
            result = fill_form_sequential(
                tab_id=fn_args.get('tabId'),
                fields=fn_args.get('fields', []),
                submit_selector=fn_args.get('submit_selector'),
                field_delay=fn_args.get('field_delay', 0.2),
            )
        else:
            return f'❌ Unknown advanced browser tool: {fn_name}'
        # Format result dict
        if isinstance(result, dict):
            if result.get('success'):
                steps = result.get('steps_completed', '?')
                details = result.get('details', {})
                parts = [f'✅ {fn_name} succeeded ({steps} steps)']
                if details:
                    parts.append(json.dumps(details, ensure_ascii=False, indent=2))
                return '\n'.join(parts)
            else:
                return f'❌ {fn_name} failed: {result.get("error", "unknown error")} (completed {result.get("steps_completed", 0)} steps)'
        return str(result)
    except Exception as e:
        logger.warning("Browser tool %s error: %s", fn_name, e, exc_info=True)
        return f'❌ {fn_name} error: {e}'


# Maps browser tool fn_name → handler(fn_args).
BROWSER_HANDLERS = {
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
    # Advanced browser tools use a lambda wrapper to pass fn_name through
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
    # Store client_id in thread-local so send_browser_command can access it
    # without modifying every handler's signature.
    _set_active_client(client_id)
    handler = BROWSER_HANDLERS.get(fn_name)
    if handler is not None:
        return handler(fn_args)
    logger.warning("Unknown browser tool requested: %s", fn_name)
    return f'❌ Unknown browser tool: {fn_name}'
