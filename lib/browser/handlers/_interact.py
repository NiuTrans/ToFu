"""Current intent-based browser interaction handlers.

Every handler receives a request-scoped runtime with immutable owner/device
authority.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


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


def _read_elements(fn_args, runtime):
    tab_id = fn_args.get('tabId')
    if tab_id is None:
        return 'Error: tabId is required. Use browser_list_tabs first.'
    params = {
        'tabId': int(tab_id),
        'maxElements': fn_args.get('maxElements', 200),
        'viewport': fn_args.get('viewport', False),
    }
    result, error = runtime.send(
        'get_interactive_elements', params, timeout=15)
    if error:
        return f'Error getting elements from tab {tab_id}: {error}'
    if isinstance(result, dict):
        elements = result.get('elements', [])
        title = result.get('title', '')
        url = result.get('url', '')
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


def _handle_click(fn_args, runtime):
    # v2 (): text= intent resolution, advisory auto-wait,
    # working-tab default, and a post-action page-state receipt — the model
    # says WHAT to click; the mechanics are owned here.
    from lib.browser._resolve import (
        action_receipt, auto_wait, resolve_element, resolve_work_tab,
        tab_snapshot,
    )
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
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
            tab_id, text_query, 'clickable', send=runtime.send)
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
        tab_id, selector, send=runtime.send)
    before = tab_snapshot(tab_id, send=runtime.send)
    params = {
        'tabId': int(tab_id),
        'selector': selector,
        'rightClick': fn_args.get('rightClick', False),
        'scrollTo': fn_args.get('scrollTo', True),
    }
    result, error = runtime.send('click_element', params, timeout=15)
    if error:
        return f'Error clicking element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if not result.get('clicked'):
            return f'Click failed: {result.get("error", "unknown error")}'
        click_type = 'Right-clicked' if result.get('rightClick') else 'Clicked'
        tag = result.get('tag', '?')
        text = result.get('text', '')
        text_display = f' "{text[:60]}"' if text else ''
        receipt = action_receipt(
            tab_id, before, route_key=runtime.route_key, send=runtime.send)
        return (f'{click_type} <{tag}>{text_display} (selector: {selector})'
                f'{matched_note}{_trusted_suffix(result)}{wait_note}{receipt}')
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_type(fn_args, runtime):
    """browser_type — clear-first text entry (the type_text bridge command).

    Target by text= (placeholder/label fuzzy match) or selector=. Replaces
    the field content by default (clearFirst=True) — the lesson fill_form
    learned the hard way (keyboard_input appends).
    """
    from lib.browser._resolve import (
        action_receipt, resolve_element, resolve_work_tab, tab_snapshot,
    )
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
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
            tab_id, text_query, 'input', send=runtime.send)
        if el is None:
            lines = [f'No input field matches text="{text_query}" ({note}).']
            if candidates:
                lines.append('Closest fields:')
                lines.extend(candidates)
            return '\n'.join(lines)
        selector = el.get('selector', '')
        matched_note = f' [matched "{text_query}"]'
    before = tab_snapshot(tab_id, send=runtime.send)
    clear_first = fn_args.get('clearFirst', True)
    result, error = runtime.send('type_text', {
        'tabId': int(tab_id),
        'selector': selector,
        'text': str(value),
        'clearFirst': clear_first,
    }, timeout=10)
    if error:
        return f'Error typing into tab {tab_id}: {error}'
    if isinstance(result, dict) and result.get('error'):
        return f'Type failed: {result.get("error")}'
    receipt = action_receipt(
        tab_id, before, route_key=runtime.route_key, send=runtime.send)
    mode = 'replaced' if clear_first else 'appended to'
    return (f'Typed {len(str(value))} chars into {selector} ({mode} existing '
            f'content){matched_note}{receipt}')


def _handle_press_key(fn_args, runtime):
    """Send special keys or shortcuts and return an action receipt."""
    from lib.browser._resolve import (
        action_receipt, resolve_work_tab, tab_snapshot,
    )
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return ('Error: no tab to act on. Pass tab_id, or call '
                'browser_list_tabs / browser_navigate first.')
    keys = fn_args.get('keys', '')
    if not keys:
        return 'Error: keys is required.'
    before = tab_snapshot(tab_id, send=runtime.send)
    params = {
        'tabId': int(tab_id),
        'keys': keys,
    }
    if fn_args.get('selector'):
        params['selector'] = fn_args['selector']
    result, error = runtime.send('keyboard_input', params, timeout=10)
    if error:
        return f'Error sending keyboard input in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('success'):
            target = result.get('target', '')
            target_display = f' on <{target}>' if target else ''
            receipt = action_receipt(
                tab_id, before, route_key=runtime.route_key,
                send=runtime.send)
            return (f'Sent keys "{keys}"{target_display}'
                    f'{_trusted_suffix(result)}{receipt}')
        return f'Keyboard input failed: {result.get("error", "unknown error")}'
    return json.dumps(result, ensure_ascii=False, indent=2)
