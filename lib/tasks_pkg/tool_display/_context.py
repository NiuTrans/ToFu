"""Display-time argument enrichment for tool-round labels.

Some tool arguments are machine handles a human cannot read — conversation
ids, browser tab ids. Execution needs them; the timeline should show what
they MEAN. This module resolves the human-readable counterpart (the
conversation's title, the tab's title) under the caller's authority and
injects it as an underscore-prefixed hint (``_conv_title`` / ``_tab_title``)
on a COPY of the args, for display handlers only. The enriched copy never
reaches execution or persistence.

Every lookup is best-effort and bounded: a miss, an offline store, or an
ambiguous multi-device mapping yields no hint, and the handler renders its
pre-existing generic label. Callers without task context (``tool_round_label``
secondary surfaces) get no enrichment by design.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['enrich_display_args']

# tool name → arg key carrying the referenced conversation id
_CONV_ID_ARGS = {
    'get_conversation': 'conversation_id',
    'project_peer_status': 'conv_id',
    'project_message': 'to_conv_id',
    'project_intervene': 'to_conv_id',
}

# Browser tools whose label names a tab (see lib/browser/display.py).
_TAB_LABEL_TOOLS = frozenset({
    'browser_read_page', 'browser_devtools', 'browser_execute_js',
    'browser_screenshot', 'browser_navigate', 'browser_click',
    'browser_type', 'browser_press_key', 'browser_menu_click',
    'browser_fill_form', 'browser_close_tab',
})

_CONV_TITLE_TTL_SEC = 120.0
_CONV_TITLE_CAP = 512
_conv_title_lock = threading.Lock()
# (owner_user_id, conv_id) -> (title, fetched_at); '' caches a miss too
_conv_title_cache: dict[tuple[str, str], tuple[str, float]] = {}


def _conversation_title(owner_user_id: str, conv_id: str) -> str:
    """Title of one of the caller's conversations, cached briefly."""
    key = (owner_user_id, conv_id)
    now = time.time()
    with _conv_title_lock:
        hit = _conv_title_cache.get(key)
        if hit is not None and now - hit[1] < _CONV_TITLE_TTL_SEC:
            return hit[0]
    title = ''
    try:
        from lib.conversations import repository
        snap = repository.get_conversation(
            conv_id, user_id=int(owner_user_id), include_messages=False)
        if snap is not None:
            title = str(snap.get('title') or '').strip()
    except Exception as e:
        logger.debug('[ToolDisplay] conversation title lookup failed for '
                     '%.32s: %s', conv_id, e)
    with _conv_title_lock:
        if len(_conv_title_cache) >= _CONV_TITLE_CAP:
            _conv_title_cache.clear()
        _conv_title_cache[key] = (title, now)
    return title


def _owner_of(task) -> str:
    owner = str((task or {}).get('_userId') or '').strip()
    return owner if owner.isdigit() and int(owner) >= 1 else ''


def _tab_title_hint(fn_args, owner: str) -> str:
    """Resolve the page title for a browser call, unique across devices."""
    from lib.browser import tab_titles
    from lib.browser.queue import get_connected_clients
    clients = get_connected_clients(owner_user_id=owner)
    client_ids = [c.get('client_id') for c in clients if c.get('client_id')]
    if not client_ids:
        return ''
    tab_id = fn_args.get('tab_id', fn_args.get('tabId'))
    if tab_id is not None:
        return tab_titles.tab_title(owner, tab_id, client_ids=client_ids)
    _tab_id, title = tab_titles.work_tab_title(owner, client_ids=client_ids)
    return title


def enrich_display_args(fn_name, fn_args, *, conv_id=None, task=None):
    """Return ``fn_args`` plus readability hints, or ``fn_args`` unchanged.

    The returned dict (when enriched) is a shallow copy — the caller keeps
    the original for execution and persistence, so hints never leak into
    either. Never raises.
    """
    if not isinstance(fn_args, dict):
        return fn_args
    owner = _owner_of(task)
    if not owner:
        return fn_args
    try:
        conv_key = _CONV_ID_ARGS.get(fn_name)
        if conv_key:
            ref = str(fn_args.get(conv_key) or '').strip()
            if ref:
                title = _conversation_title(owner, ref)
                if title:
                    return {**fn_args, '_conv_title': title}
            return fn_args
        if fn_name in _TAB_LABEL_TOOLS:
            title = _tab_title_hint(fn_args, owner)
            if title:
                return {**fn_args, '_tab_title': title}
    except Exception as e:
        logger.debug('[ToolDisplay] arg enrichment failed for %s: %s',
                     fn_name, e)
    return fn_args
