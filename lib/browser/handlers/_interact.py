"""Current intent-based browser interaction handlers.

Every handler receives a request-scoped runtime with immutable owner/device
authority.
"""

import json
import re

from lib.log import get_logger

logger = get_logger(__name__)


_TAB_GONE_RE = re.compile(r'element discovery failed: Tab \d+ not found')


_NO_AUTOMATION_TAB_NOTE = (
    ' There is no automation tab to continue in either; call '
    'browser_navigate (it opens a background tab) or pass the id of a tab '
    'the user has open.')


def _retarget_if_tab_gone(note, tab_id, fn_args, runtime):
    """Re-seed the working tab when the remembered one died between calls.

    Returns ``(replacement_tab_id, note)``. When nothing is left to retarget
    onto — the normal outcome now, because a user tab is never adopted — the
    note gains the recovery action, so the caller is not sent looking for a
    better ``text=`` on a tab that no longer exists. An explicit tabId arg is
    a deliberate target and is never silently rerouted.
    """
    if fn_args.get('tabId') is not None:
        return None, note
    if not note or not _TAB_GONE_RE.search(note):
        return None, note
    from lib.browser._resolve import forget_work_tab, resolve_work_tab
    forget_work_tab(runtime.route_key, tab_id)
    retry_tab = resolve_work_tab(
        {}, route_key=runtime.route_key, send=runtime.send)
    if retry_tab is None or retry_tab == tab_id:
        return None, note + _NO_AUTOMATION_TAB_NOTE
    return retry_tab, note


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
                'browser_navigate first (it opens an automation tab), or pass '
                'the id of a tab the user already has open.')
    selector = fn_args.get('selector', '')
    text_query = fn_args.get('text', '')
    if not selector and not text_query:
        return ("Error: say WHAT to click — text='登录' (fuzzy-matched) or "
                "selector='#id' (explicit).")
    forward_target = ''
    matched_note = ''
    if not selector:
        el, note, candidates = resolve_element(
            tab_id, text_query, 'clickable', send=runtime.send)

        if el is None:
            retry_tab, note = _retarget_if_tab_gone(
                note, tab_id, fn_args, runtime)
            if retry_tab is not None:
                tab_id = retry_tab
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
        # A custom control (MTD/Element/AntD) indexes its visible wrapper but
        # carries the hidden input it drives in targetSelector — forward it so
        # the extension interacts with the real control, not just the wrapper.
        forward_target = el.get('targetSelector') or ''
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
    if forward_target:
        params['forwardTarget'] = forward_target
    result, error = runtime.send('click_element', params, timeout=15)
    if error:
        return f'Error clicking element in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if not result.get('clicked'):
            code = result.get('targetCode')
            code_note = f' [{code}]' if code else ''
            extra = ''
            if code == 'TARGET_OBSCURED' and result.get('obscuring'):
                extra = f' (obscuring: {result["obscuring"]})'
            return (f'Click failed: {result.get("error", "unknown error")}'
                    f'{code_note}{extra}')
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
                'browser_navigate first (it opens an automation tab), or pass '
                'the id of a tab the user already has open.')
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
            retry_tab, note = _retarget_if_tab_gone(
                note, tab_id, fn_args, runtime)
            if retry_tab is not None:
                tab_id = retry_tab
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
                'browser_navigate first (it opens an automation tab), or pass '
                'the id of a tab the user already has open.')
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


def _resolve_target_with_forward(fn_args, runtime, kinds):
    """Shared text/selector resolution returning (tab_id, selector,
    forward_target, error_response). Mirrors _handle_click's lookup so custom
    controls (radio/checkbox wrappers, combobox triggers) forward to their
    real control."""
    from lib.browser._resolve import resolve_element, resolve_work_tab
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return None, '', '', ('Error: no tab to act on. Pass tab_id, or call '
                              'browser_navigate first (it opens an automation '
                              'tab), or pass the id of a tab the user has open.')
    selector = fn_args.get('selector', '')
    text_query = fn_args.get('text', '')
    if not selector and not text_query:
        return None, '', '', ("Error: say WHICH control — text='同意协议' "
                              "(fuzzy-matched) or selector='#id' (explicit).")
    forward_target = ''
    if not selector:
        el, note, candidates = resolve_element(
            tab_id, text_query, kinds, send=runtime.send)
        if el is None:
            retry_tab, note = _retarget_if_tab_gone(
                note, tab_id, fn_args, runtime)
            if retry_tab is not None:
                tab_id = retry_tab
                el, note, candidates = resolve_element(
                    tab_id, text_query, kinds, send=runtime.send)
        if el is None:
            lines = [f'No match for text="{text_query}" ({note}).']
            if candidates:
                lines.append('Closest elements:')
                lines.extend(candidates)
            return None, '', '', '\n'.join(lines)
        selector = el.get('selector', '')
        forward_target = el.get('targetSelector') or ''
    return tab_id, selector, forward_target, None


def _handle_check(fn_args, runtime):
    """browser_check — semantic set of a radio/checkbox/switch to a target
    state. Forwards custom controls to their hidden input; verifies the
    resulting state. A set, not a blind toggle: safe to call idempotently."""
    from lib.browser._resolve import action_receipt, tab_snapshot
    checked = fn_args.get('checked', True)
    tab_id, selector, forward_target, err = _resolve_target_with_forward(
        fn_args, runtime, 'clickable')
    if err:
        return err
    before = tab_snapshot(tab_id, send=runtime.send)
    params = {
        'tabId': int(tab_id),
        'selector': selector,
        'checked': bool(checked),
        'scrollTo': fn_args.get('scrollTo', True),
    }
    if forward_target:
        params['forwardTarget'] = forward_target
    result, error = runtime.send('set_checked', params, timeout=15)
    if error:
        return f'Error setting checked in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('applied'):
            state = result.get('checked')
            role = result.get('role', 'checkbox')
            already = ' (already in that state)' if result.get('already') else ''
            receipt = action_receipt(
                tab_id, before, route_key=runtime.route_key, send=runtime.send)
            return (f'Set {role} to {"checked" if state else "unchecked"}'
                    f' (selector: {selector}){already}{receipt}')
        code = result.get('targetCode')
        code_note = f' [{code}]' if code else ''
        return (f'Check failed: {result.get("error", "unknown error")}'
                f'{code_note}')
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_select_option(fn_args, runtime):
    """browser_select_option — choose an option in a custom dropdown (MTD /
    Element-UI / Ant Design) or a native <select>. Opens the trigger, waits
    for the option list, clicks the matching option. This is the two-step the
    generic fill_form could not do (its 'option menu did not open' failure)."""
    from lib.browser._resolve import action_receipt, tab_snapshot
    option = fn_args.get('option')
    if option is None or str(option) == '':
        return 'Error: option is required (the visible option text to choose).'
    tab_id, selector, forward_target, err = _resolve_target_with_forward(
        fn_args, runtime, 'clickable')
    if err:
        return err
    before = tab_snapshot(tab_id, send=runtime.send)
    params = {
        'tabId': int(tab_id),
        'selector': selector,
        'option': str(option),
        'exact': fn_args.get('exact', True),
        'timeoutMs': int(fn_args.get('timeoutMs', 3000)),
    }
    if forward_target:
        params['forwardTarget'] = forward_target
    result, error = runtime.send('select_option', params, timeout=20)
    if error:
        return f'Error selecting option in tab {tab_id}: {error}'
    if isinstance(result, dict):
        if result.get('selected'):
            text = result.get('text', '')
            receipt = action_receipt(
                tab_id, before, route_key=runtime.route_key, send=runtime.send)
            return (f'Selected option "{text}" in {selector}{receipt}')
        code = result.get('targetCode')
        code_note = f' [{code}]' if code else ''
        opts = result.get('options')
        opts_note = ''
        if opts:
            shown = '", "'.join(str(o)[:40] for o in opts[:8])
            opts_note = f' Available options: ["{shown}"]'
        return (f'Select failed: {result.get("error", "unknown error")}'
                f'{code_note}{opts_note}')
    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_scroll(fn_args, runtime):
    """browser_scroll — scroll the page (or bring an element into view).
    Read-only: only the viewport changes, so this stays out of the write
    partition and never needs approval. Reports the new position plus the
    top/bottom boundary so the model knows when to stop scrolling."""
    from lib.browser._resolve import resolve_work_tab
    tab_id = resolve_work_tab(
        fn_args, route_key=runtime.route_key, send=runtime.send)
    if tab_id is None:
        return ('Error: no tab to act on. Pass tab_id, or call '
                'browser_navigate first (it opens an automation tab), or pass '
                'the id of a tab the user already has open.')
    params = {'tabId': int(tab_id)}
    selector = fn_args.get('selector')
    if selector:
        params['selector'] = selector
    else:
        params['direction'] = fn_args.get('direction', 'down')
        if fn_args.get('amount') is not None:
            params['amount'] = int(fn_args['amount'])
    result, error = runtime.send('scroll_page', params, timeout=10)
    if error:
        return f'Error scrolling in tab {tab_id}: {error}'
    if not isinstance(result, dict) or not result.get('scrolled'):
        reason = result.get('error', 'unknown error') if isinstance(result, dict) \
            else 'unknown error'
        return f'Scroll failed: {reason}'
    if result.get('method') == 'scrollIntoView':
        return (f'Scrolled "{selector}" into view '
                f'(scrollY {result.get("scrollY")}, '
                f'{result.get("scrollPercent")}% down the page).')
    direction = result.get('direction', params.get('direction', 'down'))
    moved = result.get('pixelsMoved', 0)
    boundary = ''
    if result.get('atBottom'):
        boundary = ' — reached the bottom of the page'
    elif result.get('atTop'):
        boundary = ' — reached the top of the page'
    return (f'Scrolled {direction} {moved}px '
            f'(scrollY {result.get("scrollY")}, '
            f'{result.get("scrollPercent")}% down the page){boundary}.')
