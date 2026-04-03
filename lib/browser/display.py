"""lib/browser/display.py — Concise display strings for browser tool calls."""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['browser_tool_display', 'update_tab_title', 'get_tab_title']

# ── Lightweight tab-title cache ───────────────────────────────────────
# Populated by handlers.py when list_tabs / read_tab / navigate etc.
# return tab metadata from the browser extension.
_tab_titles = {}          # {int(tabId): str(title)}
_tab_titles_lock = threading.Lock()
_MAX_CACHE = 200          # evict oldest when cache exceeds this


def update_tab_title(tab_id, title):
    """Cache a tab ID → title mapping (called from handlers after extension responds)."""
    if tab_id is None or not title:
        return
    try:
        key = int(tab_id)
    except (ValueError, TypeError):
        logger.debug('Non-numeric tab_id in update_tab_title: %s', tab_id)
        return
    with _tab_titles_lock:
        _tab_titles[key] = str(title)
        # Simple eviction: drop the first entry when too large
        if len(_tab_titles) > _MAX_CACHE:
            try:
                oldest = next(iter(_tab_titles))
                del _tab_titles[oldest]
            except StopIteration:
                logger.debug('Tab title cache empty during eviction')


def get_tab_title(tab_id):
    """Return cached title for a tab ID, or None if unknown."""
    if tab_id is None:
        return None
    try:
        key = int(tab_id)
    except (ValueError, TypeError):
        return None
    with _tab_titles_lock:
        return _tab_titles.get(key)


def _tab_label(tab_id):
    """Return a human-friendly label: truncated title if cached, else raw ID."""
    if tab_id is None:
        return '?'
    # Handle non-numeric tab IDs like 'active' gracefully
    try:
        int(tab_id)
    except (ValueError, TypeError):
        return str(tab_id)
    title = get_tab_title(tab_id)
    if title:
        short = title[:30] + ('…' if len(title) > 30 else '')
        return f'"{short}"'
    return str(tab_id)

_DISPLAY_HANDLERS = {
    'browser_list_tabs': lambda fn_args: '🌐 List browser tabs',
    'browser_read_tab': lambda fn_args: (
        f'📖 Read {_tab_label(fn_args.get("tabId"))} [{fn_args.get("selector", "")[:30]}]'
        if fn_args.get('selector')
        else f'📖 Read {_tab_label(fn_args.get("tabId"))}'
    ),
    'browser_execute_js': lambda fn_args: f'⚡ {_tab_label(fn_args.get("tabId"))}: {fn_args.get("code", "")[:40]}',
    'browser_screenshot': lambda fn_args: f'📸 Screenshot {_tab_label(fn_args.get("tabId"))}',
    'browser_get_cookies': lambda fn_args: f'🍪 Get cookies [{(fn_args.get("domain") or fn_args.get("url", "all"))[:30]}]',
    'browser_get_history': lambda fn_args: f'📜 Search history [{fn_args.get("query", "")[:30] or "all"}]',
    'browser_create_tab': lambda fn_args: f'➕ New tab: {fn_args.get("url", "")[:40]}',
    'browser_close_tab': lambda fn_args: f'✖ Close tab {_tab_label(fn_args.get("tabId", fn_args.get("tabIds", "?")))}',
    'browser_navigate': lambda fn_args: f'🔗 Navigate {_tab_label(fn_args.get("tabId"))} → {fn_args.get("url", "")[:30]}',
    'browser_get_interactive_elements': lambda fn_args: f'🔍 Get interactive elements {_tab_label(fn_args.get("tabId"))}',
    'browser_click': lambda fn_args: f'{"🖱️ Right-click" if fn_args.get("rightClick") else "🖱️ Click"} {_tab_label(fn_args.get("tabId"))}: {fn_args.get("selector", "")[:30]}',
    'browser_keyboard': lambda fn_args: f'⌨️ Keyboard {_tab_label(fn_args.get("tabId"))}: {fn_args.get("keys", "")[:30]}',
    'browser_hover': lambda fn_args: f'🖱️ Hover {_tab_label(fn_args.get("tabId"))}: {fn_args.get("selector", "")[:30]}',
    'browser_wait': lambda fn_args: (
        f'⏳ Wait for "{fn_args.get("selector", "")[:30]}" ({_tab_label(fn_args.get("tabId"))})'
        if fn_args.get('selector')
        else (f'⏳ Wait {fn_args.get("time", "")}s ({_tab_label(fn_args.get("tabId"))})'
              if fn_args.get('time')
              else f'⏳ Wait ({_tab_label(fn_args.get("tabId"))})')
    ),
    'browser_summarize_page': lambda fn_args: f'📄 Summarize page ({_tab_label(fn_args.get("tabId"))})',
    'browser_get_app_state': lambda fn_args: f'🔧 Get app state ({_tab_label(fn_args.get("tabId"))})',
    'browser_right_click_menu': lambda fn_args: f'🖱️ Right-click menu ({_tab_label(fn_args.get("tabId"))}): {fn_args.get("menu_item_text", "")[:30]}',
    'browser_hover_and_click': lambda fn_args: f'🖱️ Hover & click ({_tab_label(fn_args.get("tabId"))})',
    'browser_fill_form': lambda fn_args: f'📝 Fill form ({_tab_label(fn_args.get("tabId"))}), {len(fn_args.get("fields", []))} fields)',
}


def browser_tool_display(fn_name, fn_args):
    """Return a concise display string for a browser tool call."""
    handler = _DISPLAY_HANDLERS.get(fn_name)
    if handler is not None:
        return handler(fn_args)
    return fn_name
