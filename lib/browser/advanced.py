"""Current compound browser interactions: menu selection and form fill."""

import time
from typing import Any

from lib.browser._resolve import resolve_element
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'fill_form_sequential', 'menu_click',
    'ADVANCED_BROWSER_TOOLS', 'ADVANCED_BROWSER_TOOL_NAMES',
    'ADVANCED_BROWSER_TOOL_FILL_FORM',
    'ADVANCED_BROWSER_TOOL_MENU_CLICK',
]


def _right_click_menu_select(
    tab_id: int,
    target_selector: str,
    menu_item_text: str,
    submenu_item_text: str | None = None,
    menu_wait: float = 0.5,
    timeout: float = 5.0,
    *,
    send,
) -> dict[str, Any]:
    """Right-click an element and select from the custom context menu.

    Steps: right-click target → wait for menu → find item → click → (optional) submenu.
    """
    start_time = time.time()
    steps = 0
    try:
        # Step 1: Get initial elements (validation)
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': False, 'maxElements': 200
        }, timeout=min(timeout, 3))
        if error:
            return {'success': False, 'steps_completed': 0, 'error': f"Failed to get elements: {error}"}
        steps = 1

        # Step 2: Right-click target
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': target_selector, 'rightClick': True, 'scrollTo': True
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 1, 'error': f"Right-click failed: {error or 'Unknown error'}"}
        steps = 2

        # Step 3: Wait for menu
        time.sleep(menu_wait)
        steps = 3

        # Step 4: Get menu elements
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': True, 'maxElements': 100
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error:
            return {'success': False, 'steps_completed': 3, 'error': f"Failed to get menu elements: {error}"}
        elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
        steps = 4

        # Step 5: Find menu item
        menu_item = None
        for el in elements:
            text = el.get('text', '').strip()
            if menu_item_text.lower() in text.lower():
                menu_item = el
                break
        if not menu_item:
            return {
                'success': False, 'steps_completed': 5,
                'error': f"Menu item '{menu_item_text}' not found",
                'available_items': [e.get('text', '') for e in elements[:20]]
            }

        # Step 6: Click menu item
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': menu_item['selector'],
            'rightClick': False, 'scrollTo': False
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 6, 'error': f"Click menu item failed: {error or 'Unknown error'}"}
        steps = 7

        # Step 7: Optional submenu
        if submenu_item_text:
            time.sleep(0.3)
            result, error = send('get_interactive_elements', {
                'tabId': tab_id, 'viewport': True, 'maxElements': 100
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error:
                return {'success': False, 'steps_completed': 7, 'error': f"Failed to get submenu elements: {error}"}
            elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
            steps = 8

            submenu_item = None
            for el in elements:
                if submenu_item_text.lower() in el.get('text', '').lower():
                    submenu_item = el
                    break
            if not submenu_item:
                return {
                    'success': False, 'steps_completed': 8,
                    'error': f"Submenu item '{submenu_item_text}' not found",
                    'available_items': [e.get('text', '') for e in elements[:20]]
                }
            result, error = send('click_element', {
                'tabId': tab_id, 'selector': submenu_item['selector'],
                'rightClick': False, 'scrollTo': False
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error or not (isinstance(result, dict) and result.get('clicked')):
                return {'success': False, 'steps_completed': 9, 'error': f"Click submenu item failed: {error or 'Unknown error'}"}
            steps = 9

        elapsed = time.time() - start_time
        return {
            'success': True, 'steps_completed': steps,
            'elapsed_ms': round(elapsed * 1000, 2),
            'details': {'target': target_selector, 'menu_item': menu_item_text, 'submenu_item': submenu_item_text}
        }
    except Exception as e:
        logger.warning('right_click_menu_select failed for target=%s menu_item=%s after %d steps: %s',
                       target_selector, menu_item_text, steps, e, exc_info=True)
        return {'success': False, 'steps_completed': steps, 'error': f"Exception: {str(e)}",
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


def menu_click(
    tab_id: int,
    item_text: str,
    target_selector: str | None = None,
    target_text: str | None = None,
    via: str = 'hover',
    submenu_item_text: str | None = None,
    menu_wait: float = 0.5,
    timeout: float = 5.0,
    *,
    send,
) -> dict[str, Any]:
    """Open a menu and click one visible item in a single call.

    via='right_click' opens a context menu; via='hover' (default) opens a
    hover menu, then runs the same
    text-match-and-click flow. The target may be named by target_text
    (fuzzy-resolved server-side) so the model never needs a selector for the
    common "open the File menu, click Export" case.
    """
    start_time = time.time()
    if not target_selector:
        if not target_text:
            return {'success': False,
                    'error': 'target_selector or target_text is required'}
        el, note, candidates = resolve_element(
            tab_id, target_text, 'clickable', send=send)
        if el is None:
            return {'success': False,
                    'error': f'menu target "{target_text}": {note}',
                    'available_items': [c.strip() for c in candidates]}
        target_selector = el.get('selector', '')
    if via == 'right_click':
        return _right_click_menu_select(
            tab_id, target_selector, item_text, submenu_item_text,
            menu_wait, timeout, send=send)
    # ── hover-open, then text-match the revealed item ──
    try:
        result, error = send('hover_element', {
            'tabId': tab_id, 'selector': target_selector,
        }, timeout=min(timeout, 3))
        hovered = isinstance(result, dict) and (
            result.get('hovered') or result.get('success'))
        if error or not hovered:
            return {'success': False,
                    'error': f'Hover failed: {error or (result or {}).get("error", "unknown")}'}
        time.sleep(menu_wait)
        result, error = send('get_interactive_elements', {
            'tabId': tab_id, 'viewport': True, 'maxElements': 100,
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error:
            return {'success': False,
                    'error': f'Failed to enumerate revealed menu: {error}'}
        elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
        menu_item = None
        for el in elements:
            if item_text.lower() in el.get('text', '').strip().lower():
                menu_item = el
                break
        if not menu_item:
            return {
                'success': False,
                'error': f"Menu item '{item_text}' not found",
                'available_items': [e.get('text', '') for e in elements[:20]],
            }
        result, error = send('click_element', {
            'tabId': tab_id, 'selector': menu_item['selector'], 'scrollTo': False,
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False,
                    'error': f"Click menu item failed: {error or 'unknown'}"}
        if submenu_item_text:
            time.sleep(0.3)
            result, error = send('get_interactive_elements', {
                'tabId': tab_id, 'viewport': True, 'maxElements': 100,
            }, timeout=min(timeout - (time.time() - start_time), 3))
            elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
            submenu = None
            for el in elements:
                if submenu_item_text.lower() in el.get('text', '').strip().lower():
                    submenu = el
                    break
            if not submenu:
                return {
                    'success': False,
                    'error': f"Submenu item '{submenu_item_text}' not found",
                    'available_items': [e.get('text', '') for e in elements[:20]],
                }
            result, error = send('click_element', {
                'tabId': tab_id, 'selector': submenu['selector'], 'scrollTo': False,
            }, timeout=min(timeout - (time.time() - start_time), 3))
            if error or not (isinstance(result, dict) and result.get('clicked')):
                return {'success': False,
                        'error': f"Click submenu item failed: {error or 'unknown'}"}
        return {
            'success': True,
            'elapsed_ms': round((time.time() - start_time) * 1000, 2),
            'details': {'via': 'hover', 'target': target_selector,
                        'menu_item': item_text,
                        'submenu_item': submenu_item_text},
        }
    except Exception as e:
        logger.warning('menu_click failed for target=%s item=%s: %s',
                       target_selector, item_text, e, exc_info=True)
        return {'success': False, 'error': f'Exception: {e}',
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


def fill_form_sequential(
    tab_id: int, fields: list[dict[str, str]],
    submit_selector: str | None = None,
    field_delay: float = 0.2, timeout: float = 10.0,
    submit_text: str | None = None,
    *,
    send,
) -> dict[str, Any]:
    """Fill form fields sequentially and optionally submit."""
    start_time = time.time()
    fields_filled = 0
    field_results: list[dict[str, Any]] = []
    try:
        for i, field in enumerate(fields):
            if time.time() - start_time > timeout:
                field_results.append({'index': i, 'selector': field.get('selector'),
                                      'ok': False, 'error': 'Timeout before field'})
                return {'success': False, 'fields_filled': fields_filled,
                        'fields_failed': len(fields) - fields_filled,
                        'field_results': field_results, 'error': 'Timeout'}
            selector = field.get('selector')
            value = field.get('value')
            field_type = field.get('type', 'type')

            # A field may name itself by text= (placeholder/label fuzzy
            # match) instead of a raw selector — resolved here, in code.
            if not selector and field.get('text'):
                el, note, _cand = resolve_element(
                    tab_id, field['text'], 'input', send=send)
                if el is None:
                    field_results.append({
                        'index': i, 'text': field.get('text'), 'ok': False,
                        'error': f'field "{field["text"]}" not matched: {note}',
                    })
                    time.sleep(field_delay)
                    continue
                selector = el.get('selector', '')

            if field_type == 'type':
                # Use type_text (not keyboard_input): it clears the field FIRST
                # (clearFirst defaults True) and sets the value via the native
                # input setter, so changing an existing value (e.g. origin
                # A→B) REPLACES it instead of appending "AB". keyboard_input
                # only appends keystrokes and would concatenate onto the old
                # value.
                _res, _err = send('type_text', {
                    'tabId': tab_id, 'selector': selector, 'text': value,
                    'clearFirst': True,
                }, timeout=3)
                if _err:
                    field_results.append({'index': i, 'selector': selector, 'type': 'type',
                                          'ok': False, 'error': f'type_text failed: {_err}'})
                else:
                    fields_filled += 1
                    field_results.append({'index': i, 'selector': selector, 'type': 'type', 'ok': True})
            elif field_type == 'click':
                _res, _err = send('click_element', {
                    'tabId': tab_id, 'selector': selector, 'scrollTo': True
                }, timeout=2)
                if _err:
                    field_results.append({'index': i, 'selector': selector, 'type': 'click',
                                          'ok': False, 'error': f'click failed: {_err}'})
                else:
                    fields_filled += 1
                    field_results.append({'index': i, 'selector': selector, 'type': 'click', 'ok': True})
            elif field_type == 'select':
                _res, _err = send('click_element', {
                    'tabId': tab_id, 'selector': selector, 'scrollTo': True
                }, timeout=2)
                if _err:
                    # Without this check a failed open-click fell through to
                    # get_interactive_elements and was misreported as
                    # "option not found" — every other branch here checks _err.
                    field_results.append({'index': i, 'selector': selector, 'type': 'select',
                                          'ok': False, 'error': f'select open failed: {_err}'})
                    time.sleep(field_delay)
                    continue
                time.sleep(0.3)
                result, error = send('get_interactive_elements', {
                    'tabId': tab_id, 'viewport': True, 'maxElements': 100
                }, timeout=2)
                elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
                matched = None
                for el in elements:
                    if value.lower() in el.get('text', '').lower():
                        matched = el
                        break
                if matched is None:
                    # Silent no-match is the real failure mode: the option never
                    # got clicked but the loop used to march on and report
                    # success. Report it explicitly with candidate options so
                    # the model can retry with a corrected value.
                    candidates = [e.get('text', '').strip() for e in elements
                                  if e.get('text', '').strip()][:20]
                    field_results.append({'index': i, 'selector': selector, 'type': 'select',
                                          'ok': False,
                                          'error': f"Option matching '{value}' not found",
                                          'available_options': candidates})
                    logger.warning("fill_form_sequential: select option '%s' not matched "
                                   "for selector=%s (%d candidates)", value, selector, len(candidates))
                else:
                    _res, _err = send('click_element', {
                        'tabId': tab_id, 'selector': matched['selector'], 'scrollTo': False
                    }, timeout=2)
                    if _err:
                        field_results.append({'index': i, 'selector': selector, 'type': 'select',
                                              'ok': False, 'error': f'select click failed: {_err}'})
                    else:
                        fields_filled += 1
                        field_results.append({'index': i, 'selector': selector, 'type': 'select',
                                              'ok': True, 'matched': matched.get('text', '').strip()})
            else:
                field_results.append({'index': i, 'selector': selector, 'ok': False,
                                      'error': f"Unknown field type '{field_type}'"})
            time.sleep(field_delay)

        fields_failed = len(fields) - fields_filled
        all_ok = fields_failed == 0

        submitted = False
        # The submit button may also be named by text (submit_text).
        if not submit_selector and submit_text and all_ok:
            el, note, _cand = resolve_element(
                tab_id, submit_text, 'clickable', send=send)
            if el is not None:
                submit_selector = el.get('selector', '')
            else:
                logger.warning('fill_form_sequential: submit_text %r not matched: %s',
                               submit_text, note)
        if submit_selector and all_ok:
            # Never submit a form with failed/missing fields — that would post a
            # half-filled booking. Skip submit and report the failures instead.
            send('click_element', {
                'tabId': tab_id, 'selector': submit_selector, 'scrollTo': True
            }, timeout=2)
            submitted = True
            time.sleep(0.5)

        result_out = {'success': all_ok, 'fields_filled': fields_filled,
                      'fields_failed': fields_failed, 'field_results': field_results,
                      'submitted': submitted,
                      'elapsed_ms': round((time.time() - start_time) * 1000, 2)}
        if not all_ok:
            result_out['error'] = (f'{fields_failed} of {len(fields)} field(s) failed; '
                                   f'submit skipped' if submit_selector else
                                   f'{fields_failed} of {len(fields)} field(s) failed')
        return result_out
    except Exception as e:
        logger.warning('fill_form_sequential failed after %d/%d fields filled: %s',
                       fields_filled, len(fields), e, exc_info=True)
        return {'success': False, 'fields_filled': fields_filled,
                'fields_failed': len(fields) - fields_filled,
                'field_results': field_results, 'submitted': False,
                'error': f"Exception: {str(e)}",
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


# ═══════════════════════════════════════════════════════
#  Tool Definitions (for LLM function calling)
# ═══════════════════════════════════════════════════════

ADVANCED_BROWSER_TOOL_MENU_CLICK = {
    "type": "function",
    "function": {
        "name": "browser_menu_click",
        "description": (
            "Open a menu and click an item, in ONE call. via='hover' (default) "
            "opens dropdown/nav menus; via='right_click' opens context menus. "
            "Name the target by target_text (fuzzy-matched, e.g. 'File') or "
            "target_selector; the item is clicked by visible-text match "
            "(item_text, case-insensitive substring). submenu_text handles "
            "nested menus. If the item is not found, the available menu items "
            "are returned so you can retry with the right text. If the item "
            "opens a new tab, the result says so and that tab becomes the "
            "working tab."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Tab ID (omit to use the current working tab)"},
                "target_text": {"type": "string", "description": "Visible text of the element that opens the menu (preferred over target_selector)"},
                "target_selector": {"type": "string", "description": "CSS selector of the element that opens the menu"},
                "item_text": {"type": "string", "description": "Text of the menu item to click (case-insensitive substring match)"},
                "via": {"type": "string", "enum": ["hover", "right_click"], "description": "How the menu opens (default: hover)"},
                "submenu_text": {"type": "string", "description": "(Optional) Text of a nested submenu item to click after item_text"},
                "menu_wait": {"type": "number", "description": "Seconds to wait for the menu to appear (default: 0.5)"},
                "timeout": {"type": "number", "description": "Total timeout in seconds (default: 5.0)"},
            },
            "required": ["item_text"]
        }
    }
}

ADVANCED_BROWSER_TOOL_FILL_FORM = {
    "type": "function",
    "function": {
        "name": "browser_fill_form",
        "description": (
            "Fill MULTIPLE form fields in ONE call, then optionally submit. "
            "Supports text input, clicks, and select dropdowns.\n"
            "PREFER THIS whenever you need to fill or change 2+ fields (e.g. a "
            "booking form's origin AND destination AND date). Do NOT loop "
            "browser_click + browser_type field-by-field — that is slower "
            "and error-prone. One browser_fill_form call handles the whole set.\n"
            "For type='type' fields the existing value is CLEARED first, so "
            "changing a pre-filled field (origin A→B) replaces it cleanly "
            "instead of concatenating.\n"
            "Fields format: [{selector|text, value, type}, ...] where type is 'type', 'click', or 'select'. "
            "A field may be targeted by text (its placeholder/label, fuzzy-matched) instead of selector.\n"
            "Automatically handles focusing, typing delays, and dropdown interactions. "
            "A new tab opened by the submit is reported and becomes the working tab."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Tab ID (omit to use the current working tab)"},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of the field"},
                            "text": {"type": "string", "description": "Field label/placeholder to fuzzy-match (alternative to selector)"},
                            "value": {"type": "string", "description": "Value to enter"},
                            "type": {"type": "string", "enum": ["type", "click", "select"], "description": "Input type"}
                        },
                        "required": ["value"]
                    },
                    "description": "List of fields to fill"
                },
                "submit_selector": {"type": "string", "description": "(Optional) CSS selector of submit button"},
                "submit_text": {"type": "string", "description": "(Optional) Visible text of the submit button (alternative to submit_selector)"},
                "field_delay": {"type": "number", "description": "Delay between fields in seconds (default: 0.2)"},
            },
            "required": ["fields"]
        }
    }
}

ADVANCED_BROWSER_TOOLS = [
    ADVANCED_BROWSER_TOOL_MENU_CLICK,
    ADVANCED_BROWSER_TOOL_FILL_FORM,
]

ADVANCED_BROWSER_TOOL_NAMES = {
    'browser_menu_click',
    'browser_fill_form',
}
