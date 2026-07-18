"""lib/browser/handlers/_interact.py — Element interaction handlers.

Handlers for discovering interactive elements and interacting with them
(click, keyboard, hover, wait). Each takes fn_args (dict) and returns a
string result for the LLM, communicating with the browser extension via
send_browser_command().
"""

import json

from lib.browser.display import update_tab_title
from lib.log import get_logger

logger = get_logger(__name__)


def _facade():
    """Return the package facade so collaborators resolve at call time."""
    import lib.browser.handlers as _pkg
    return _pkg


def send_browser_command(*args, **kwargs):
    """Facade-resolving proxy for lib.browser.queue.send_browser_command."""
    return _facade().send_browser_command(*args, **kwargs)


def _handle_get_interactive_elements(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required. Use browser_list_tabs first.'
    params = {
        'tabId': int(tab_id),
        'maxElements': fn_args.get('maxElements', 200),
        'viewport': fn_args.get('viewport', False),
    }
    result, error = send_browser_command('get_interactive_elements', params, timeout=15)
    if error:
        return f'Error getting elements from tab {tab_id}: {error}'
    if isinstance(result, dict):
        elements = result.get('elements', [])
        title = result.get('title', '')
        url = result.get('url', '')
        # Cache tab ID → title + URL for display strings
        if title or url:
            update_tab_title(tab_id, title, url=url)
        total = result.get('total', len(elements))
        lines = [f'Tab: {title}', f'URL: {url}',
                 f'Interactive elements ({len(elements)} shown, {total} total):\n']
        for i, el in enumerate(elements):
            tag = el.get('tag', '?')
            text = el.get('text', '')
            selector = el.get('selector', '')
            role = el.get('role', '')
            extra_parts = []
            if role: extra_parts.append(f'role={role}')
            if el.get('href'): extra_parts.append(f'href={el["href"][:80]}')
            if el.get('type'): extra_parts.append(f'type={el["type"]}')
            if el.get('ariaLabel'): extra_parts.append(f'aria-label="{el["ariaLabel"]}"')
            if el.get('title'): extra_parts.append(f'title="{el["title"]}"')
            if el.get('placeholder'): extra_parts.append(f'placeholder="{el["placeholder"]}"')
            if el.get('disabled'): extra_parts.append('DISABLED')
            extra = f' ({", ".join(extra_parts)})' if extra_parts else ''
            display_text = f' "{text[:60]}"' if text else ''
            lines.append(f'  [{i+1}] <{tag}>{display_text}{extra}')
            lines.append(f'       selector: {selector}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_click(fn_args):
    tab_id = fn_args.get('tabId')
    selector = fn_args.get('selector', '')
    if tab_id is None:
        return 'Error: tabId is required.'
    if not selector:
        return 'Error: selector is required. Use browser_get_interactive_elements to discover selectors.'
    params = {
        'tabId': int(tab_id),
        'selector': selector,
        'rightClick': fn_args.get('rightClick', False),
        'scrollTo': fn_args.get('scrollTo', True),
    }
    result, error = send_browser_command('click_element', params, timeout=15)
    if error:
        return f'Error clicking element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if not result.get('clicked'):
            return f'Click failed: {result.get("error", "unknown error")}'
        click_type = 'Right-clicked' if result.get('rightClick') else 'Clicked'
        tag = result.get('tag', '?')
        text = result.get('text', '')
        text_display = f' "{text[:60]}"' if text else ''
        return f'{click_type} <{tag}>{text_display} (selector: {selector})'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_keyboard(fn_args):
    tab_id = fn_args.get('tabId')
    keys = fn_args.get('keys', '')
    if tab_id is None:
        return 'Error: tabId is required.'
    if not keys:
        return 'Error: keys is required.'
    params = {
        'tabId': int(tab_id),
        'keys': keys,
    }
    if fn_args.get('selector'):
        params['selector'] = fn_args['selector']
    result, error = send_browser_command('keyboard_input', params, timeout=10)
    if error:
        return f'Error sending keyboard input in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('success'):
            target = result.get('target', '')
            target_display = f' on <{target}>' if target else ''
            return f'Sent keys "{keys}"{target_display}'
        return f'Keyboard input failed: {result.get("error", "unknown error")}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_hover(fn_args):
    tab_id = fn_args.get('tabId')
    selector = fn_args.get('selector', '')
    if tab_id is None:
        return 'Error: tabId is required.'
    if not selector:
        return 'Error: selector is required.'
    params = {
        'tabId': int(tab_id),
        'selector': selector,
    }
    result, error = send_browser_command('hover_element', params, timeout=10)
    if error:
        return f'Error hovering element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('hovered') or result.get('success'):
            tag = result.get('tag', '?')
            text = result.get('text', '')
            text_display = f' "{text[:60]}"' if text else ''
            return f'Hovered <{tag}>{text_display} (selector: {selector})'
        return f'Hover failed: {result.get("error", "unknown error")}'
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_wait(fn_args):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required.'
    params = {'tabId': int(tab_id)}
    selector = fn_args.get('selector')
    wait_time = fn_args.get('time')
    if selector:
        params['selector'] = selector
        params['condition'] = fn_args.get('condition', 'present')
        params['timeout'] = fn_args.get('timeout', 5000)
        result, error = send_browser_command('wait_for_element', params, timeout=max(15, (params['timeout'] / 1000) + 5))
    elif wait_time:
        # Simple time-based wait: just sleep on server side
        import time
        wait_seconds = min(float(wait_time), 30)  # Cap at 30 seconds
        time.sleep(wait_seconds)
        return f'Waited {wait_seconds}s'
    else:
        return 'Error: either "selector" or "time" parameter is required.'
    if error:
        return f'Error waiting for element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('found') or result.get('success'):
            return f'Element found: {selector} (condition: {params.get("condition", "present")})'
        return f'Timeout: element "{selector}" not found within {params["timeout"]}ms'
    return json.dumps(result, ensure_ascii=False, indent=2)
