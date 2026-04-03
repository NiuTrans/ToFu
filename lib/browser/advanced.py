"""lib/browser/advanced.py — Complex multi-step browser interaction patterns.

Reference: Playwright, Selenium, BrowseAgent best practices.
Provides high-level compound operations for multi-step, deep interactions.
"""

import time
from typing import Any

from lib.browser.queue import send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'right_click_menu_select', 'hover_and_click', 'wait_and_find_element',
    'fill_form_sequential',
    'ADVANCED_BROWSER_TOOLS', 'ADVANCED_BROWSER_TOOL_NAMES',
    'ADVANCED_BROWSER_TOOL_RIGHT_CLICK_MENU',
    'ADVANCED_BROWSER_TOOL_HOVER_CLICK',
    'ADVANCED_BROWSER_TOOL_FILL_FORM',
]


def right_click_menu_select(
    tab_id: int,
    target_selector: str,
    menu_item_text: str,
    submenu_item_text: str | None = None,
    menu_wait: float = 0.5,
    timeout: float = 5.0
) -> dict[str, Any]:
    """Right-click an element and select from the custom context menu.

    Steps: right-click target → wait for menu → find item → click → (optional) submenu.
    """
    start_time = time.time()
    steps = 0

    try:
        # Step 1: Get initial elements (validation)
        result, error = send_browser_command('get_interactive_elements', {
            'tabId': tab_id, 'viewport': False, 'maxElements': 200
        }, timeout=min(timeout, 3))
        if error:
            return {'success': False, 'steps_completed': 0, 'error': f"Failed to get elements: {error}"}
        steps = 1

        # Step 2: Right-click target
        result, error = send_browser_command('click_element', {
            'tabId': tab_id, 'selector': target_selector, 'rightClick': True, 'scrollTo': True
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 1, 'error': f"Right-click failed: {error or 'Unknown error'}"}
        steps = 2

        # Step 3: Wait for menu
        time.sleep(menu_wait)
        steps = 3

        # Step 4: Get menu elements
        result, error = send_browser_command('get_interactive_elements', {
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
        result, error = send_browser_command('click_element', {
            'tabId': tab_id, 'selector': menu_item['selector'],
            'rightClick': False, 'scrollTo': False
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'steps_completed': 6, 'error': f"Click menu item failed: {error or 'Unknown error'}"}
        steps = 7

        # Step 7: Optional submenu
        if submenu_item_text:
            time.sleep(0.3)
            result, error = send_browser_command('get_interactive_elements', {
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
            result, error = send_browser_command('click_element', {
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


def hover_and_click(
    tab_id: int, hover_selector: str, click_selector: str,
    hover_wait: float = 0.3, timeout: float = 5.0
) -> dict[str, Any]:
    """Hover over an element to reveal a dropdown, then click a menu item."""
    start_time = time.time()
    try:
        result, error = send_browser_command('hover_element', {
            'tabId': tab_id, 'selector': hover_selector
        }, timeout=min(timeout, 3))
        if error or not (isinstance(result, dict) and result.get('hovered')):
            return {'success': False, 'error': f"Hover failed: {error or 'Unknown error'}"}
        time.sleep(hover_wait)
        result, error = send_browser_command('click_element', {
            'tabId': tab_id, 'selector': click_selector, 'scrollTo': False
        }, timeout=min(timeout - (time.time() - start_time), 3))
        if error or not (isinstance(result, dict) and result.get('clicked')):
            return {'success': False, 'error': f"Click failed: {error or 'Unknown error'}"}
        elapsed = time.time() - start_time
        return {'success': True, 'elapsed_ms': round(elapsed * 1000, 2),
                'details': {'hovered': hover_selector, 'clicked': click_selector}}
    except Exception as e:
        logger.warning('hover_and_click failed for hover=%s click=%s: %s',
                       hover_selector, click_selector, e, exc_info=True)
        return {'success': False, 'error': f"Exception: {str(e)}",
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


def wait_and_find_element(
    tab_id: int, selector: str, condition: str = 'visible',
    timeout_ms: int = 5000, poll_interval_ms: int = 100
) -> dict[str, Any]:
    """Wait for an element to appear, similar to Selenium WebDriverWait."""
    start_time = time.time()
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        try:
            result, error = send_browser_command('wait_for_element', {
                'tabId': tab_id, 'selector': selector, 'condition': condition,
                'timeout': min(timeout_ms - elapsed_ms, 1000), 'interval': poll_interval_ms
            }, timeout=2)
            if isinstance(result, dict) and result.get('found'):
                return {'found': True, 'selector': selector, 'condition': condition,
                        'waited_ms': int((time.time() - start_time) * 1000),
                        'element': {'visible': result.get('visible', False), 'clickable': result.get('clickable', False)}}
            if error:
                break
        except Exception as e:
            logger.warning('wait_for_element poll failed for selector=%s condition=%s elapsed=%dms: %s',
                           selector, condition, elapsed_ms, e, exc_info=True)
        elapsed_ms = int((time.time() - start_time) * 1000)
        time.sleep(poll_interval_ms / 1000.0)
    return {'found': False, 'selector': selector, 'condition': condition,
            'waited_ms': int((time.time() - start_time) * 1000),
            'error': f"Element not found within {timeout_ms}ms"}


def fill_form_sequential(
    tab_id: int, fields: list[dict[str, str]],
    submit_selector: str | None = None,
    field_delay: float = 0.2, timeout: float = 10.0
) -> dict[str, Any]:
    """Fill form fields sequentially and optionally submit."""
    start_time = time.time()
    fields_filled = 0
    try:
        for i, field in enumerate(fields):
            if time.time() - start_time > timeout:
                return {'success': False, 'fields_filled': fields_filled, 'error': 'Timeout'}
            selector = field.get('selector')
            value = field.get('value')
            field_type = field.get('type', 'type')

            if field_type == 'type':
                send_browser_command('click_element', {
                    'tabId': tab_id, 'selector': selector, 'scrollTo': True
                }, timeout=2)
                time.sleep(0.1)
                send_browser_command('keyboard_input', {
                    'tabId': tab_id, 'keys': value, 'selector': selector
                }, timeout=2)
            elif field_type == 'click':
                send_browser_command('click_element', {
                    'tabId': tab_id, 'selector': selector, 'scrollTo': True
                }, timeout=2)
            elif field_type == 'select':
                send_browser_command('click_element', {
                    'tabId': tab_id, 'selector': selector, 'scrollTo': True
                }, timeout=2)
                time.sleep(0.3)
                result, error = send_browser_command('get_interactive_elements', {
                    'tabId': tab_id, 'viewport': True, 'maxElements': 100
                }, timeout=2)
                elements = (result or {}).get('elements', []) if isinstance(result, dict) else []
                for el in elements:
                    if value.lower() in el.get('text', '').lower():
                        send_browser_command('click_element', {
                            'tabId': tab_id, 'selector': el['selector'], 'scrollTo': False
                        }, timeout=2)
                        break
            fields_filled += 1
            time.sleep(field_delay)

        submitted = False
        if submit_selector:
            send_browser_command('click_element', {
                'tabId': tab_id, 'selector': submit_selector, 'scrollTo': True
            }, timeout=2)
            submitted = True
            time.sleep(0.5)

        return {'success': True, 'fields_filled': fields_filled, 'submitted': submitted,
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}
    except Exception as e:
        logger.warning('fill_form_sequential failed after %d/%d fields filled: %s',
                       fields_filled, len(fields), e, exc_info=True)
        return {'success': False, 'fields_filled': fields_filled, 'submitted': False,
                'error': f"Exception: {str(e)}",
                'elapsed_ms': round((time.time() - start_time) * 1000, 2)}


# ═══════════════════════════════════════════════════════
#  Tool Definitions (for LLM function calling)
# ═══════════════════════════════════════════════════════

ADVANCED_BROWSER_TOOL_RIGHT_CLICK_MENU = {
    "type": "function",
    "function": {
        "name": "browser_right_click_menu",
        "description": (
            "Right-click an element and select a menu item from the context menu. "
            "Supports nested submenus. This is a high-level compound operation that handles "
            "the full sequence: right-click → wait for menu → find menu item → click → (optional) submenu.\n"
            "Use this for complex menu interactions instead of manual multi-step commands.\n"
            "Returns detailed status including which steps succeeded and available menu items if not found."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "description": "Tab ID from browser_list_tabs"},
                "target_selector": {"type": "string", "description": "CSS selector of the element to right-click"},
                "menu_item_text": {"type": "string", "description": "Text of the menu item to click (case-insensitive partial match)"},
                "submenu_item_text": {"type": "string", "description": "(Optional) Text of submenu item to click"},
                "menu_wait": {"type": "number", "description": "Seconds to wait for menu to appear (default: 0.5)"},
                "timeout": {"type": "number", "description": "Total timeout in seconds (default: 5.0)"},
            },
            "required": ["tabId", "target_selector", "menu_item_text"]
        }
    }
}

ADVANCED_BROWSER_TOOL_HOVER_CLICK = {
    "type": "function",
    "function": {
        "name": "browser_hover_and_click",
        "description": (
            "Hover over an element to reveal a dropdown menu, then click a menu item. "
            "This handles the common pattern: hover → wait for animation → click.\n"
            "Use this for navigation menus, dropdowns, and hover-activated interfaces."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "description": "Tab ID from browser_list_tabs"},
                "hover_selector": {"type": "string", "description": "CSS selector of the element to hover over"},
                "click_selector": {"type": "string", "description": "CSS selector of the menu item to click"},
                "hover_wait": {"type": "number", "description": "Seconds to wait after hover for menu to appear (default: 0.3)"},
            },
            "required": ["tabId", "hover_selector", "click_selector"]
        }
    }
}

ADVANCED_BROWSER_TOOL_FILL_FORM = {
    "type": "function",
    "function": {
        "name": "browser_fill_form",
        "description": (
            "Fill form fields sequentially and optionally submit. "
            "Supports text input, clicks, and select dropdowns.\n"
            "Fields format: [{selector, value, type}, ...] where type is 'type', 'click', or 'select'.\n"
            "Automatically handles focusing, typing delays, and dropdown interactions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {"type": "integer", "description": "Tab ID from browser_list_tabs"},
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of the field"},
                            "value": {"type": "string", "description": "Value to enter"},
                            "type": {"type": "string", "enum": ["type", "click", "select"], "description": "Input type"}
                        },
                        "required": ["selector", "value"]
                    },
                    "description": "List of fields to fill"
                },
                "submit_selector": {"type": "string", "description": "(Optional) CSS selector of submit button"},
                "field_delay": {"type": "number", "description": "Delay between fields in seconds (default: 0.2)"},
            },
            "required": ["tabId", "fields"]
        }
    }
}

ADVANCED_BROWSER_TOOLS = [
    ADVANCED_BROWSER_TOOL_RIGHT_CLICK_MENU,
    ADVANCED_BROWSER_TOOL_HOVER_CLICK,
    ADVANCED_BROWSER_TOOL_FILL_FORM,
]

ADVANCED_BROWSER_TOOL_NAMES = {
    'browser_right_click_menu',
    'browser_hover_and_click',
    'browser_fill_form',
}
