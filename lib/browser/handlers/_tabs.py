"""Current tab lifecycle and navigation handlers.

Handlers for listing, reading, creating, closing tabs and navigating.
Each handler receives an explicit :class:`BrowserToolRuntime` authority.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _result_url_allowed(result, runtime) -> bool:
    """Fail closed when a read completed after a cross-domain redirect."""
    if not isinstance(result, dict) or not result.get('url'):
        return False
    try:
        from lib.browser.access import is_read_allowed
        return is_read_allowed(
            runtime.owner_user_id, result.get('url') or '')
    except Exception as exc:
        logger.debug('read result URL policy check failed closed: %s', exc)
        return False


def _handle_list_tabs(fn_args, runtime):
    result, error = runtime.send('list_tabs', timeout=15)
    if error:
        return f'Error listing tabs: {error}'
    if isinstance(result, list):
        # A read denial applies even to tab enumeration: titles/URLs from a
        # denied domain are page data and must not leak through this side path.
        try:
            from lib.browser.access import is_read_allowed
            result = [t for t in result
                      if not t.get('url') or is_read_allowed(
                          runtime.owner_user_id, t.get('url'))]
        except Exception as exc:
            logger.debug('tab access filtering failed closed: %s', exc)
            result = []
        lines = [f'Open tabs ({len(result)} total):\n']
        for t in result:
            active_mark = ' * (active)' if t.get('active') else ''
            client_mark = (' [Tofu client — never navigated]'
                           if t.get('isClient') else '')
            url = t.get('url', '')
            title = t.get('title', '(no title)')
            lines.append(f'  Tab {t["id"]}: {title}{active_mark}{client_mark}')
            lines.append(f'    URL: {url}')
        # Seed only from the already-filtered result. Re-querying through the
        # resolver would both waste a bridge round and could remember a tab
        # whose domain was removed by the owner-scoped read policy above.
        # isClient rows (the Tofu app itself) are never seeded — navigating
        # or clicking the chat out from under the user is never the intent.
        from lib.browser._resolve import remember_work_tab
        selected = next((t for t in result
                         if t.get('active') and not t.get('isClient')), None)
        if selected is None:
            selected = next((t for t in result if not t.get('isClient')), None)
        if selected is not None and selected.get('id') is not None:
            remember_work_tab(runtime.route_key, selected['id'])
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
            from lib.search_runtime import prepare_search_dependency_import
            prepare_search_dependency_import()
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


def _render_read_result(result, tab_id, *, network_text='', max_chars=50_000):
    """Format the extension's ``read_tab`` wire payload for read_page."""
    if isinstance(result, dict):
        if result.get('error'):
            return f'Error: {result["error"]}'
        title = result.get('title', '')
        url = result.get('url', '')
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
        if network_text:
            from lib.browser.network_evidence import merge_page_and_network
            text = merge_page_and_network(
                text, network_text, max_chars=max_chars)
        return header + text
    return str(result)


def _read_tab(fn_args, runtime):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required. Use browser_list_tabs first to get tab IDs.'
    result, error = runtime.send('read_tab', {
        'tabId': int(tab_id),
        'selector': fn_args.get('selector'),
        'maxChars': fn_args.get('maxChars', 50000),
    }, timeout=30)
    if error:
        return f'Error reading tab {tab_id}: {error}'
    if not _result_url_allowed(result, runtime):
        return 'Error: browser read result was denied by domain policy'
    return _render_read_result(result, tab_id)


def _handle_close_tab(fn_args, runtime):
    params = {}
    if fn_args.get('tabId') is not None:
        params['tabId'] = int(fn_args['tabId'])
    if fn_args.get('tabIds'):
        params['tabIds'] = [int(t) for t in fn_args['tabIds']]
    result, error = runtime.send('close_tab', params, timeout=10)
    if error:
        return f'Error closing tab(s): {error}'
    if isinstance(result, dict) and result.get('closed'):
        from lib.browser._resolve import forget_work_tab
        closed = result['closed']
        for cid in (closed if isinstance(closed, list) else [closed]):
            forget_work_tab(runtime.route_key, cid)
        return f'Closed tab(s): {closed}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_navigate(fn_args, runtime):
    # New-tab and same-tab navigation share one model-facing contract. Load
    # waiting is on by default so the next action cannot race navigation.
    from lib.browser._resolve import remember_work_tab, resolve_work_tab
    url = fn_args.get('url')
    if not url:
        return 'Error: url is required.'
    if fn_args.get('newTab'):
        params = {
            'url': url,
            'active': fn_args.get('active', False),
            'waitForLoad': fn_args.get('waitForLoad', True),
            'timeoutMs': 20_000,
        }
        result, error = runtime.send('create_tab', params, timeout=35)
        if error:
            return f'Error opening new tab: {error}'
        if isinstance(result, dict):
            new_id = result.get('id')
            remember_work_tab(runtime.route_key, new_id)
            return (f'Opened new tab #{new_id} -> {url} '
                    f'(now the working tab)')
        return json.dumps(result, ensure_ascii=False, indent=2)
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return ('Error: no tab to navigate. Pass tab_id, use new_tab=true, '
                'or call browser_list_tabs first.')
    params = {
        'tabId': int(tab_id),
        'url': url,
        'waitForLoad': fn_args.get('waitForLoad', True),
    }
    result, error = runtime.send('navigate', params, timeout=35)
    if error:
        return f'Error navigating tab {tab_id}: {error}'
    if isinstance(result, dict):
        # The extension refuses to navigate the Tofu client tab and opens a
        # new tab instead — follow it so the next tab_id-less call lands on
        # the new page, not back on the chat.
        remember_work_tab(runtime.route_key, result.get('id', tab_id))
        if result.get('redirectedToNewTab'):
            return (f'Tab #{tab_id} is the Tofu client tab and is never '
                    f'navigated; opened new tab #{result.get("id")} -> '
                    f'{result.get("url", url)} instead (now the working tab)')
        return f'Navigated tab #{result.get("id", tab_id)} -> {result.get("url", url)} (status: {result.get("status", "?")})'
    return json.dumps(result, ensure_ascii=False, indent=2)
