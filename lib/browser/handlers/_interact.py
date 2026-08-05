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


def _trusted_suffix(result):
    """Annotate how an input was delivered (extension >= 4.6.0 reports it).

    Trusted CDP events pass isTrusted checks (and real CSS :hover); the
    synthetic fallback does not — the model needs to know which happened
    when a click "did nothing".
    """
    trusted = result.get('trusted')
    if trusted is True:
        return ' [trusted CDP input]'
    if trusted is False:
        reason = result.get('fallbackReason') or 'CDP unavailable'
        return f' [synthetic fallback: {reason}]'
    return ''  # pre-4.6.0 extension — no annotation on the wire


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
            if el.get('pointer'): extra_parts.append('cursor-pointer')
            if el.get('disabled'): extra_parts.append('DISABLED')
            extra = f' ({", ".join(extra_parts)})' if extra_parts else ''
            display_text = f' "{text[:60]}"' if text else ''
            lines.append(f'  [{i+1}] <{tag}>{display_text}{extra}')
            lines.append(f'       selector: {selector}')
        return '\n'.join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_click(fn_args):
    # v2 (pt_869e5648403e4745): text= intent resolution, advisory auto-wait,
    # working-tab default, and a post-action page-state receipt — the model
    # says WHAT to click; the mechanics are owned here.
    from lib.browser._resolve import (
        action_receipt, auto_wait, resolve_element, resolve_work_tab,
        tab_snapshot,
    )
    tab_id = resolve_work_tab(fn_args, send_browser_command)
    if tab_id is None:
        return ('Error: no tab to act on. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    selector = fn_args.get('selector', '')
    text_query = fn_args.get('text', '')
    if not selector and not text_query:
        return ("Error: say WHAT to click — text='登录' (fuzzy-matched) or "
                "selector='#id' (explicit).")
    matched_note = ''
    if not selector:
        el, note, candidates = resolve_element(
            tab_id, text_query, 'clickable', send=send_browser_command)
        if el is None:
            lines = [f'No clear match for text="{text_query}" ({note}).']
            if candidates:
                lines.append('Closest elements:')
                lines.extend(candidates)
            lines.append('Retry with a more specific text=, or take a selector '
                         'from browser_read_page(mode="elements").')
            return '\n'.join(lines)
        selector = el.get('selector', '')
        matched_note = f' [matched "{text_query}"]'
    # Model-supplied selectors get an advisory presence wait; resolver-derived
    # ones came from a live enumeration milliseconds ago.
    wait_note = '' if text_query else auto_wait(
        tab_id, selector, send_browser_command)
    before = tab_snapshot(tab_id, send_browser_command)
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
        receipt = action_receipt(tab_id, before, send_browser_command)
        return (f'{click_type} <{tag}>{text_display} (selector: {selector})'
                f'{matched_note}{_trusted_suffix(result)}{wait_note}{receipt}')
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_type(fn_args):
    """browser_type — clear-first text entry (the type_text bridge command).

    Target by text= (placeholder/label fuzzy match) or selector=. Replaces
    the field content by default (clearFirst=True) — the lesson fill_form
    learned the hard way (keyboard_input appends).
    """
    from lib.browser._resolve import (
        action_receipt, resolve_element, resolve_work_tab, tab_snapshot,
    )
    tab_id = resolve_work_tab(fn_args, send_browser_command)
    if tab_id is None:
        return ('Error: no tab to act on. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    value = fn_args.get('value')
    if value is None:
        return 'Error: value is required (the text to type into the field).'
    selector = fn_args.get('selector', '')
    text_query = fn_args.get('text', '')
    if not selector and not text_query:
        return ("Error: say WHICH field — text='搜索' (matches placeholder/"
                "label) or selector='#input'.")
    matched_note = ''
    if not selector:
        el, note, candidates = resolve_element(
            tab_id, text_query, 'input', send=send_browser_command)
        if el is None:
            lines = [f'No input field matches text="{text_query}" ({note}).']
            if candidates:
                lines.append('Closest fields:')
                lines.extend(candidates)
            return '\n'.join(lines)
        selector = el.get('selector', '')
        matched_note = f' [matched "{text_query}"]'
    before = tab_snapshot(tab_id, send_browser_command)
    clear_first = fn_args.get('clearFirst', True)
    result, error = send_browser_command('type_text', {
        'tabId': int(tab_id),
        'selector': selector,
        'text': str(value),
        'clearFirst': clear_first,
    }, timeout=10)
    if error:
        return f'Error typing into tab {tab_id}: {error}'
    if isinstance(result, dict) and result.get('error'):
        return f'Type failed: {result.get("error")}'
    receipt = action_receipt(tab_id, before, send_browser_command)
    mode = 'replaced' if clear_first else 'appended to'
    return (f'Typed {len(str(value))} chars into {selector} ({mode} existing '
            f'content){matched_note}{receipt}')


def _handle_press_key(fn_args):
    """browser_press_key — special keys / shortcuts (v2 successor of
    browser_keyboard): working-tab default + action receipt."""
    from lib.browser._resolve import (
        action_receipt, resolve_work_tab, tab_snapshot,
    )
    tab_id = resolve_work_tab(fn_args, send_browser_command)
    if tab_id is None:
        return ('Error: no tab to act on. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    keys = fn_args.get('keys', '')
    if not keys:
        return 'Error: keys is required.'
    before = tab_snapshot(tab_id, send_browser_command)
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
            receipt = action_receipt(tab_id, before, send_browser_command)
            return (f'Sent keys "{keys}"{target_display}'
                    f'{_trusted_suffix(result)}{receipt}')
        return f'Keyboard input failed: {result.get("error", "unknown error")}'
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
            return f'Sent keys "{keys}"{target_display}{_trusted_suffix(result)}'
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
            return (f'Hovered <{tag}>{text_display} (selector: {selector})'
                    f'{_trusted_suffix(result)}')
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
