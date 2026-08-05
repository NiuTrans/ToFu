"""lib/browser/display.py — Concise display strings for browser tool calls."""

import threading
from urllib.parse import urlsplit

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['browser_tool_display', 'update_tab_title', 'get_tab_title',
           'get_tab_url', 'get_tab_hostname']

# ── Lightweight tab metadata cache ────────────────────────────────────
# Populated by handlers.py when list_tabs / read_tab / navigate etc.
# return tab metadata from the browser extension. We cache BOTH the title
# (preferred label) and the URL (so we can show a human-readable hostname
# even before the page title is known — e.g. right after create_tab).
_tab_titles = {}          # {int(tabId): str(title)}
_tab_urls = {}            # {int(tabId): str(url)}
_tab_titles_lock = threading.Lock()
_MAX_CACHE = 200          # evict oldest when cache exceeds this


def _evict_locked(cache):
    """Drop the oldest entry when a cache grows past _MAX_CACHE (lock held)."""
    if len(cache) > _MAX_CACHE:
        try:
            del cache[next(iter(cache))]
        except StopIteration:
            logger.debug('Tab cache empty during eviction')


def update_tab_title(tab_id, title=None, url=None):
    """Cache a tab's title and/or URL (called from handlers after the
    extension responds). Either ``title`` or ``url`` may be omitted — the
    URL alone is enough to render a hostname label for a freshly-opened tab.
    """
    if tab_id is None:
        return
    try:
        key = int(tab_id)
    except (ValueError, TypeError):
        logger.debug('Non-numeric tab_id in update_tab_title: %s', tab_id)
        return
    if not title and not url:
        return
    with _tab_titles_lock:
        if title:
            _tab_titles[key] = str(title)
            _evict_locked(_tab_titles)
        if url:
            _tab_urls[key] = str(url)
            _evict_locked(_tab_urls)


def get_tab_title(tab_id):
    """Return cached title for a tab ID, or None if unknown."""
    if tab_id is None:
        return None
    try:
        key = int(tab_id)
    except (ValueError, TypeError):
        logger.debug('[Display] Non-numeric tab_id: %s', tab_id)
        return None
    with _tab_titles_lock:
        return _tab_titles.get(key)


def get_tab_url(tab_id):
    """Return the cached full URL for a tab ID, or None if unknown."""
    if tab_id is None:
        return None
    try:
        key = int(tab_id)
    except (ValueError, TypeError):
        logger.debug('[Display] Non-numeric tab_id in get_tab_url: %s', tab_id)
        return None
    with _tab_titles_lock:
        return _tab_urls.get(key)


def get_tab_hostname(tab_id):
    """Return the cached URL's hostname for a tab ID, or None if unknown."""
    if tab_id is None:
        return None
    try:
        key = int(tab_id)
    except (ValueError, TypeError) as e:
        logger.debug('[Display] Non-numeric tab_id in get_tab_hostname: %s (%s)', tab_id, e)
        return None
    with _tab_titles_lock:
        url = _tab_urls.get(key)
    if not url:
        return None
    try:
        host = urlsplit(url).netloc
    except Exception as e:
        logger.debug('[Display] Bad URL in tab cache: %s (%s)', url, e)
        return None
    # Strip any user:pass@ credential prefix.
    host = host.split('@')[-1]
    return host or None


def _current_work_tab_id():
    """The remembered working-tab id, or None (lazy import — _resolve
    imports this module at top level, so a top-level import would cycle)."""
    try:
        from lib.browser._resolve import current_work_tab
        return current_work_tab()
    except Exception as e:
        logger.debug('[Display] work-tab lookup failed: %s', e)
        return None


def _tab_label(tab_id):
    """Return a human-friendly label for a tab: the cached title (quoted,
    in full) if known, else the page hostname, else a generic word. The
    frontend ``.ptool-text`` row wraps long labels (``word-break:break-word``),
    so we no longer truncate here.

    We deliberately NEVER surface the raw numeric tab ID — it is meaningless
    to a human reading the timeline (who knows what "12165686" is?).

    ``tab_id=None`` is the v2 DEFAULT, not missing data: the call lands on
    the working tab (the one last acted on, else the browser's active tab).
    Name that tab when the work-tab memory + title cache know it, otherwise
    say 'current tab' — a bare '?' reads as a broken render (the 2026-08-05
    'Read ?' incident).
    """
    if tab_id is None:
        wid = _current_work_tab_id()
        if wid is not None:
            title = get_tab_title(wid)
            if title:
                return f'"{title}"'
            host = get_tab_hostname(wid)
            if host:
                return host
        return 'current tab'
    # Non-numeric IDs like 'active' are themselves descriptive — keep them.
    try:
        int(tab_id)
    except (ValueError, TypeError):
        logger.debug('[Display] Non-numeric tab_id for label: %s', tab_id)
        return str(tab_id)
    title = get_tab_title(tab_id)
    if title:
        return f'"{title}"'
    host = get_tab_hostname(tab_id)
    if host:
        return host
    return 'tab'

# No emoji prefixes — the frontend renders a per-tool SVG icon (see
# ``_browserToolSvg`` in ``static/js/ui/tool_rounds.js``). An emoji here
# would duplicate that icon (CLAUDE.md §3.4).
_DISPLAY_HANDLERS = {
    'browser_list_tabs': lambda fn_args: 'List browser tabs',
    'browser_read_tab': lambda fn_args: (
        f'Read {_tab_label(fn_args.get("tabId"))} [{fn_args.get("selector", "")}]'
        if fn_args.get('selector')
        else f'Read {_tab_label(fn_args.get("tabId"))}'
    ),
    # The JS source is rendered as a dedicated code block by the frontend
    # (mirroring run_command), so the label only needs to name the tab.
    'browser_execute_js': lambda fn_args: f'Execute JS in {_tab_label(fn_args.get("tabId"))}',
    'browser_screenshot': lambda fn_args: (
        f'Screenshot (viewport) {_tab_label(fn_args.get("tabId"))}'
        if fn_args.get('fullPage') is False
        else f'Screenshot (full page) {_tab_label(fn_args.get("tabId"))}'
    ),
    'browser_get_cookies': lambda fn_args: f'Get cookies [{(fn_args.get("domain") or fn_args.get("url", "all"))}]',
    'browser_get_history': lambda fn_args: f'Search history [{fn_args.get("query", "") or "all"}]',
    'browser_create_tab': lambda fn_args: f'New tab: {fn_args.get("url", "")}',
    'browser_close_tab': lambda fn_args: (
        f'Close {_tab_label(fn_args["tabId"])}' if fn_args.get('tabId') is not None
        # Raw id lists are meaningless on a timeline ('Close [12, 87]') — count them.
        else (f'Close {len(fn_args["tabIds"])} tabs'
              if isinstance(fn_args.get('tabIds'), list) and fn_args['tabIds']
              else f'Close {_tab_label(None)}')
    ),
    'browser_navigate': lambda fn_args: (
        # v2: new_tab=true opens a NEW tab (the old tab is untouched) —
        # 'Navigate <tab> → url' would misdescribe that as reusing one.
        f'Open new tab → {fn_args.get("url", "")}' if fn_args.get('newTab')
        else f'Navigate {_tab_label(fn_args.get("tabId"))} → {fn_args.get("url", "")}'
    ),
    'browser_get_interactive_elements': lambda fn_args: f'Get interactive elements {_tab_label(fn_args.get("tabId"))}',
    'browser_click': lambda fn_args: (
        f'{"Right-click" if fn_args.get("rightClick") else "Click"} {_tab_label(fn_args.get("tabId"))}'
        # v2: text= (fuzzy, preferred) names the target; selector= is the
        # explicit fallback. Omit the colon entirely when neither is present
        # — 'Click tab: ' with a dangling colon reads as a broken render.
        + (f': {fn_args.get("text") or fn_args.get("selector")}'
           if (fn_args.get('text') or fn_args.get('selector')) else '')
    ),
    'browser_keyboard': lambda fn_args: f'Keyboard {_tab_label(fn_args.get("tabId"))}: {fn_args.get("keys", "")}',
    'browser_hover': lambda fn_args: f'Hover {_tab_label(fn_args.get("tabId"))}: {fn_args.get("selector", "")}',
    'browser_wait': lambda fn_args: (
        f'Wait for "{fn_args.get("selector", "")}" ({_tab_label(fn_args.get("tabId"))})'
        if fn_args.get('selector')
        else (f'Wait {fn_args.get("time", "")}s ({_tab_label(fn_args.get("tabId"))})'
              if fn_args.get('time')
              else f'Wait ({_tab_label(fn_args.get("tabId"))})')
    ),
    'browser_summarize_page': lambda fn_args: f'Summarize page ({_tab_label(fn_args.get("tabId"))})',
    'browser_get_app_state': lambda fn_args: f'Get app state ({_tab_label(fn_args.get("tabId"))})',
    'browser_right_click_menu': lambda fn_args: f'Right-click menu ({_tab_label(fn_args.get("tabId"))}): {fn_args.get("menu_item_text", "")}',
    'browser_hover_and_click': lambda fn_args: f'Hover & click ({_tab_label(fn_args.get("tabId"))})',
    'browser_fill_form': lambda fn_args: (
        f'Fill form {_tab_label(fn_args.get("tabId"))}: '
        f'{len(fn_args.get("fields", []))} fields'
    ),
    # ── v2 surface (pt_869e5648403e4745) — legacy formatters above stay for
    # history rendering even though those tools are no longer shipped.
    'browser_read_page': lambda fn_args: (
        f'Read {_tab_label(fn_args.get("tabId"))}'
        + (f' [{fn_args.get("mode")}]' if fn_args.get('mode') not in (None, 'auto') else '')
    ),
    'browser_type': lambda fn_args: (
        f'Type into {_tab_label(fn_args.get("tabId"))}'
        + (f': {fn_args.get("text") or fn_args.get("selector")}'
           if (fn_args.get('text') or fn_args.get('selector')) else '')
    ),
    'browser_press_key': lambda fn_args: f'Press {fn_args.get("keys", "")} ({_tab_label(fn_args.get("tabId"))})',
    'browser_menu_click': lambda fn_args: (
        f'Menu click ({_tab_label(fn_args.get("tabId"))})'
        + (f': {fn_args["item_text"]}' if fn_args.get('item_text') else '')
    ),
    'browser_preview_page': lambda fn_args: (
        f'Render page preview: {fn_args.get("path") or fn_args.get("url", "")}'
    ),
}


def browser_tool_display(fn_name, fn_args):
    """Return a concise display string for a browser tool call."""
    from lib.browser.dispatch import normalize_browser_args
    fn_args = normalize_browser_args(fn_args)
    handler = _DISPLAY_HANDLERS.get(fn_name)
    if handler is not None:
        return handler(fn_args)
    return fn_name
