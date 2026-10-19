"""Current tab lifecycle and navigation handlers.

Handlers for listing, reading, creating, closing tabs and navigating.
Each handler receives an explicit :class:`BrowserToolRuntime` authority.
"""

import json

from lib.log import get_logger
from lib.tools.result_envelope import typed_tool_error

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
        return typed_tool_error(
            'browser_list_tabs_failed', retryable=True,
            message=f'Error listing tabs: {error}',
            next_action=(
                'Report the failed browser read. Check the selected extension '
                'connection and bridge diagnostics, then retry after recovery. '
                'Do not infer that there are no tabs from this failed read.'),
        ).to_envelope_text()
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


def _render_read_result(result, tab_id, *, network_text='', max_chars=50_000):
    """Format only errors/elements; page content belongs to collect_content.

    The legacy keyword signature remains for callers, but network_text is
    deliberately ignored: this formatter has no owner-scoped acceptance context.
    Callers must authorize the result URL before displaying element metadata.
    """
    try:
        budget = max(0, min(int(max_chars), 50_000))
    except (TypeError, ValueError, OverflowError):
        budget = 50_000

    def bounded_string(value, limit=budget):
        return value[:limit] if isinstance(value, str) else ''

    if isinstance(result, dict):
        if result.get('error'):
            error = bounded_string(result['error']) or 'Browser read failed'
            return f'Error: {error}'[:budget]
        elements = result.get('elements')
        if isinstance(elements, list):
            title = bounded_string(result.get('title'))
            url = bounded_string(result.get('url'))
            output = (f'Tab: {title}\nURL: {url}\n'
                      f'Found {len(elements)} element(s):\n')[:budget]
            for i, element in enumerate(elements[:budget]):
                if len(output) >= budget:
                    break
                if not isinstance(element, dict):
                    continue
                text = bounded_string(element.get('text'), 2000).strip()
                if text:
                    tag = bounded_string(element.get('tag'), 80) or '?'
                    output += f'\n[{i+1}] <{tag}> {text}'[:budget - len(output)]
            return output
    return 'Error: browser page content requires unified content collection'[:budget]


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
