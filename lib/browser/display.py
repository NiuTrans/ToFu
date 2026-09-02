"""Stateless timeline labels for the canonical browser tool catalogue.

Display rendering deliberately does not consult live browser state.  A tool
round can outlive its extension device, and tab identifiers are only unique
inside one device; process-global title caches therefore mislabeled rounds
across owners and devices.  Labels are derived solely from persisted args.
"""

from __future__ import annotations

__all__ = ['browser_tool_display']


def _tab_label(tab_id) -> str:
    """Name an implicit or explicit tab without exposing opaque numeric IDs."""
    return 'current tab' if tab_id is None else 'tab'


_DISPLAY_HANDLERS = {
    'browser_list_tabs': lambda args: 'List browser tabs',
    'browser_read_page': lambda args: (
        f'Read {_tab_label(args.get("tabId"))}'
        + (f' [{args["mode"]}]'
           if args.get('mode') not in (None, 'auto') else '')
    ),
    'browser_research_page': lambda args: (
        f'Research website → {args.get("url", "")}'
    ),
    'browser_devtools': lambda args: (
        f'DevTools {str(args.get("action") or "console_read").replace("_", " ")}'
        f' ({_tab_label(args.get("tabId"))})'
    ),
    'browser_execute_js': lambda args: (
        f'Execute JS in {_tab_label(args.get("tabId"))}'
    ),
    'browser_screenshot': lambda args: (
        f'Screenshot (viewport) {_tab_label(args.get("tabId"))}'
        if args.get('fullPage') is False
        else f'Screenshot (full page) {_tab_label(args.get("tabId"))}'
    ),
    'browser_get_cookies': lambda args: (
        f'Get cookies [{args.get("domain") or args.get("url") or "all"}]'
    ),
    'browser_get_history': lambda args: (
        f'Search history [{args.get("query") or "all"}]'
    ),
    'browser_close_tab': lambda args: (
        f'Close {_tab_label(args["tabId"])}'
        if args.get('tabId') is not None
        else (
            f'Close {len(args["tabIds"])} tabs'
            if isinstance(args.get('tabIds'), list) and args['tabIds']
            else 'Close current tab'
        )
    ),
    'browser_navigate': lambda args: (
        f'Open new tab → {args.get("url", "")}'
        if args.get('newTab')
        else f'Navigate {_tab_label(args.get("tabId"))} → {args.get("url", "")}'
    ),
    'browser_click': lambda args: (
        f'{"Right-click" if args.get("rightClick") else "Click"} '
        f'{_tab_label(args.get("tabId"))}'
        + (f': {args.get("text") or args.get("selector")}'
           if args.get('text') or args.get('selector') else '')
    ),
    'browser_type': lambda args: (
        f'Type into {_tab_label(args.get("tabId"))}'
        + (f': {args.get("text") or args.get("selector")}'
           if args.get('text') or args.get('selector') else '')
    ),
    'browser_press_key': lambda args: (
        f'Press {args.get("keys", "")} ({_tab_label(args.get("tabId"))})'
    ),
    'browser_menu_click': lambda args: (
        f'Menu click ({_tab_label(args.get("tabId"))})'
        + (f': {args["item_text"]}' if args.get('item_text') else '')
    ),
    'browser_fill_form': lambda args: (
        f'Fill form {_tab_label(args.get("tabId"))}: '
        f'{len(args.get("fields", []))} fields'
    ),
    'browser_preview_page': lambda args: (
        f'Render page preview: {args.get("path") or args.get("url")}'
        if args.get('path') or args.get('url')
        else 'Render page preview'
    ),
}


def browser_tool_display(fn_name, fn_args):
    """Return a deterministic label, or a clear invalid-argument label."""
    from lib.browser.dispatch import normalize_browser_args

    try:
        args = normalize_browser_args(fn_args)
    except ValueError:
        return f'{fn_name} (invalid arguments)'
    handler = _DISPLAY_HANDLERS.get(fn_name)
    return handler(args) if handler is not None else fn_name
