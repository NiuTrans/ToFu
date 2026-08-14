"""lib/browser/handlers/_tabs.py — Tab lifecycle + navigation handlers.

Handlers for listing, reading, creating, closing tabs and navigating.
Each takes fn_args (dict) and returns a string result for the LLM.
They communicate with the browser extension via send_browser_command().
"""

import json

from lib.browser.display import update_tab_title
from lib.log import get_logger

logger = get_logger(__name__)


def _result_url_allowed(result) -> bool:
    """Fail closed when a read completed after a cross-domain redirect."""
    if not isinstance(result, dict) or not result.get('url'):
        return False
    try:
        from lib.browser.access import is_read_allowed
        from lib.browser.queue import _get_active_client, client_user_id
        return is_read_allowed(
            client_user_id(_get_active_client()), result.get('url') or '')
    except Exception as exc:
        logger.debug('read result URL policy check failed closed: %s', exc)
        return False


def _facade():
    """Return the package facade so collaborators resolve at call time.

    Lets ``monkeypatch.setattr(lib.browser.handlers, 'send_browser_command', ...)``
    (the historical patch point on the flat module) take effect here.
    """
    import lib.browser.handlers as _pkg
    return _pkg


def send_browser_command(*args, **kwargs):
    """Facade-resolving proxy for lib.browser.queue.send_browser_command."""
    return _facade().send_browser_command(*args, **kwargs)


def _handle_list_tabs(fn_args):
    result, error = send_browser_command('list_tabs', timeout=15)
    if error:
        return f'Error listing tabs: {error}'
    if isinstance(result, list):
        # A read denial applies even to tab enumeration: titles/URLs from a
        # denied domain are page data and must not leak through this side path.
        try:
            from lib.browser.access import is_read_allowed
            from lib.browser.queue import _get_active_client, client_user_id
            cid = _get_active_client()
            uid = client_user_id(cid)
            result = [t for t in result
                      if not t.get('url') or is_read_allowed(uid, t.get('url'))]
        except Exception as exc:
            logger.debug('tab access filtering failed closed: %s', exc)
            result = []
        lines = [f'Open tabs ({len(result)} total):\n']
        for t in result:
            active_mark = ' * (active)' if t.get('active') else ''
            url = t.get('url', '')
            title = t.get('title', '(no title)')
            # Cache tab ID → title + URL for display strings
            update_tab_title(t.get('id'), title, url=url)
            lines.append(f'  Tab {t["id"]}: {title}{active_mark}')
            lines.append(f'    URL: {url}')
        # Seed the working tab on first contact so later calls can omit tab_id.
        from lib.browser._resolve import resolve_work_tab
        resolve_work_tab({}, send_browser_command)
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _extract_best_text(result):
    """(text, method) from a read_tab payload — server-side HTML extraction
    preferred, innerText fallback. Shared by _handle_read_tab and the
    read_page auto mode (which measures sparsity on the SAME text)."""
    url = result.get('url', '')
    raw_html = result.get('html', '')
    text = None
    extract_method = 'innerText'
    if raw_html and len(raw_html) > 200:
        try:
            from tofu_search.fetch.html_extract import extract_html_text
            text = extract_html_text(raw_html, 80000, url=url)
            if text and len(text) > 50:
                extract_method = 'html→extract'
            else:
                text = None
        except Exception as e:
            logger.warning('read_tab HTML extraction failed, falling back to innerText: %s', e)
    if not text:
        text = result.get('text', '')
    return text, extract_method


def _render_read_result(result, tab_id):
    """Format a read_tab payload for the model (the original _handle_read_tab
    body, minus the send). Shared by browser_read_tab (legacy) and
    browser_read_page."""
    if isinstance(result, dict):
        if result.get('error'):
            return f'Error: {result["error"]}'
        title = result.get('title', '')
        url = result.get('url', '')
        # Cache tab ID → title + URL for display strings
        if title or url:
            update_tab_title(tab_id, title, url=url)
        if result.get('elements'):
            elements = result['elements']
            lines = [f'Tab: {title}', f'URL: {url}',
                     f'Found {result.get("count", len(elements))} element(s):\n']
            for i, el in enumerate(elements):
                text = el.get('text', '').strip()
                if text:
                    lines.append(f'[{i+1}] <{el.get("tag", "?")}> {text[:2000]}')
            return '\n'.join(lines)
        text, extract_method = _extract_best_text(result)
        truncated = result.get('truncated', False)
        header = f'Tab: {title}\nURL: {url}\nContent ({len(text):,} chars, {extract_method}'
        if truncated and extract_method == 'innerText':
            header += f', truncated from {result.get("textLength", "?"):,}'
        header += '):\n\n'
        return header + text
    return str(result)


def _handle_read_tab(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required. Use browser_list_tabs first to get tab IDs.'
    result, error = send_browser_command('read_tab', {
        'tabId': int(tab_id),
        'selector': fn_args.get('selector'),
        'maxChars': fn_args.get('maxChars', 50000),
    }, timeout=30)
    if error:
        return f'Error reading tab {tab_id}: {error}'
    if not _result_url_allowed(result):
        return 'Error: browser read result was denied by domain policy'
    return _render_read_result(result, tab_id)


def _handle_create_tab(fn_args):
    url = fn_args.get('url', 'about:blank')
    params = {'url': url}
    if fn_args.get('active') is not None:
        params['active'] = fn_args['active']
    result, error = send_browser_command('create_tab', params, timeout=10)
    if error:
        return f'Error creating tab: {error}'
    if isinstance(result, dict):
        # Cache the URL immediately so subsequent tool rows (wait / screenshot /
        # execute_js on this new tab) render a hostname label instead of the
        # opaque numeric tab id.
        update_tab_title(result.get('id'), result.get('title'), url=result.get('url') or url)
        return f'Created new tab #{result.get("id", "?")} -> {url}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_close_tab(fn_args):
    params = {}
    if fn_args.get('tabId') is not None:
        params['tabId'] = int(fn_args['tabId'])
    if fn_args.get('tabIds'):
        params['tabIds'] = [int(t) for t in fn_args['tabIds']]
    result, error = send_browser_command('close_tab', params, timeout=10)
    if error:
        return f'Error closing tab(s): {error}'
    if isinstance(result, dict) and result.get('closed'):
        from lib.browser._resolve import forget_work_tab
        closed = result['closed']
        for cid in (closed if isinstance(closed, list) else [closed]):
            forget_work_tab(cid)
        return f'Closed tab(s): {closed}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_navigate(fn_args):
    # v2 (pt_869e5648403e4745): absorbs browser_create_tab (new_tab=true),
    # defaults the working tab, and waits for load by default — the classic
    # "read too early" failure was the old fire-and-forget default.
    from lib.browser._resolve import remember_work_tab, resolve_work_tab
    url = fn_args.get('url')
    if not url:
        return 'Error: url is required.'
    if fn_args.get('newTab'):
        params = {'url': url}
        params['active'] = fn_args.get('active', False)
        result, error = send_browser_command('create_tab', params, timeout=10)
        if error:
            return f'Error opening new tab: {error}'
        if isinstance(result, dict):
            new_id = result.get('id')
            update_tab_title(new_id, result.get('title'), url=result.get('url') or url)
            remember_work_tab(new_id)
            return (f'Opened new tab #{new_id} -> {url} '
                    f'(now the working tab)')
        return json.dumps(result, ensure_ascii=False, indent=2)
    tab_id = resolve_work_tab(fn_args, send_browser_command)
    if tab_id is None:
        return ('Error: no tab to navigate. Pass tab_id, use new_tab=true, '
                'or call browser_list_tabs first.')
    params = {
        'tabId': int(tab_id),
        'url': url,
        'waitForLoad': fn_args.get('waitForLoad', True),
    }
    result, error = send_browser_command('navigate', params, timeout=35)
    if error:
        return f'Error navigating tab {tab_id}: {error}'
    if isinstance(result, dict):
        # Cache tab title from navigation result
        nav_title = result.get('title', '')
        nav_url = result.get('url', '') or url
        if nav_title or nav_url:
            update_tab_title(result.get('id', tab_id), nav_title, url=nav_url)
        return f'Navigated tab #{result.get("id", tab_id)} -> {result.get("url", url)} (status: {result.get("status", "?")})'
    return json.dumps(result, ensure_ascii=False, indent=2)
