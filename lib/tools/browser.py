"""lib/tools/browser.py — Browser extension tool definitions."""

import logging

logger = logging.getLogger(__name__)

BROWSER_TOOL_LIST_TABS = {
    "type": "function",
    "function": {
        "name": "browser_list_tabs",
        "description": (
            "List all open browser tabs with their titles, URLs, and tab IDs. "
            "Use this first to discover what tabs the user has open, then use "
            "browser_read_tab or browser_execute_js on specific tabs."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        }
    }
}

BROWSER_TOOL_READ_TAB = {
    "type": "function",
    "function": {
        "name": "browser_read_tab",
        "description": (
            "Read the text content of a browser tab. Can extract the full page text "
            "or use a CSS selector to extract specific elements. "
            "The content is read from the user's actual browser, including pages that "
            "require authentication (e.g. internal tools, logged-in dashboards).\n"
            "NOTE: This only extracts DOM text. If the result is sparse/empty, the page likely uses "
            "Canvas/SVG/WebGL rendering (common for charts, DAG diagrams, data viz). In that case: "
            "1) Use browser_screenshot to see the visual layout; 2) Use browser_get_app_state to access "
            "Vue/React/graph data; 3) Use browser_execute_js for custom data extraction."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector to extract specific elements (optional, reads full page if omitted)"
                },
                "maxChars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 50000)"
                }
            },
            "required": ["tabId"]
        }
    }
}

BROWSER_TOOL_EXECUTE_JS = {
    "type": "function",
    "function": {
        "name": "browser_execute_js",
        "description": (
            "Execute JavaScript code in a browser tab and return the result. "
            "Use this for: reading specific data from JS variables, accessing framework state "
            "(Vue/React data), calling page APIs, or advanced DOM manipulation.\n"
            "The code runs in MAIN world with full page context (window, document, app state).\n"
            "IMPORTANT: The code must be a single expression or IIFE. "
            "Use (() => { ... return result; })() for multi-statement code. "
            "Return value must be JSON-serializable.\n"
            "TIP: For simple clicks, prefer browser_click. For discovering elements, prefer "
            "browser_get_interactive_elements. For first-time page exploration, prefer "
            "browser_summarize_page or browser_get_app_state. Use execute_js for data extraction "
            "or complex interactions that other tools can't handle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "code": {
                    "type": "string",
                    "description": "JavaScript code to execute in the page context"
                }
            },
            "required": ["tabId", "code"]
        }
    }
}

BROWSER_TOOL_SCREENSHOT = {
    "type": "function",
    "function": {
        "name": "browser_screenshot",
        "description": (
            "Capture a screenshot of the currently visible tab in the user's browser. "
            "Returns the screenshot as an IMAGE you can actually SEE and analyze visually (not base64 text!).\n"
            "Use this to: 1) Understand page layout; 2) See Canvas-rendered content (charts, graphs, DAG diagrams); "
            "3) Verify click/navigation results; 4) Read text from images when DOM extraction fails.\n"
            "STRATEGY: First-time page visit → start with browser_screenshot. If browser_read_tab returns sparse text, "
            "the page uses Canvas/SVG/WebGL — screenshot is your primary tool. Large images are auto-compressed to JPEG."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID to screenshot (activates the tab first). If omitted, captures the currently active tab."
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Image format (default: png)"
                }
            },
        }
    }
}

BROWSER_TOOL_GET_COOKIES = {
    "type": "function",
    "function": {
        "name": "browser_get_cookies",
        "description": (
            "Get cookies from the user's browser. Can filter by URL, domain, or name. "
            "Useful for reading authentication tokens, session cookies, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to get cookies for"
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to filter cookies"
                },
                "name": {
                    "type": "string",
                    "description": "Specific cookie name to retrieve"
                }
            },
        }
    }
}

BROWSER_TOOL_GET_HISTORY = {
    "type": "function",
    "function": {
        "name": "browser_get_history",
        "description": (
            "Search the user's browser history. Returns URLs, titles, visit counts and timestamps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to filter history entries (empty string = all)"
                },
                "maxResults": {
                    "type": "integer",
                    "description": "Maximum results to return (default 100)"
                }
            },
        }
    }
}

BROWSER_TOOL_CREATE_TAB = {
    "type": "function",
    "function": {
        "name": "browser_create_tab",
        "description": "Open a new browser tab with the given URL. Tab opens in the background by default without interrupting the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open in the new tab"
                },
                "active": {
                    "type": "boolean",
                    "description": "Whether the new tab should become active and steal focus (default: false, opens in background)"
                }
            },
            "required": ["url"]
        }
    }
}

BROWSER_TOOL_CLOSE_TAB = {
    "type": "function",
    "function": {
        "name": "browser_close_tab",
        "description": "Close one or more browser tabs by their tab IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Single tab ID to close"
                },
                "tabIds": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Multiple tab IDs to close"
                }
            },
        }
    }
}

BROWSER_TOOL_NAVIGATE = {
    "type": "function",
    "function": {
        "name": "browser_navigate",
        "description": (
            "Navigate an existing browser tab to a new URL. "
            "Optionally wait for the page to finish loading."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID to navigate"
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to"
                },
                "waitForLoad": {
                    "type": "boolean",
                    "description": "Wait for the page to fully load before returning (default false)"
                }
            },
            "required": ["tabId", "url"]
        }
    }
}

BROWSER_TOOL_GET_INTERACTIVE_ELEMENTS = {
    "type": "function",
    "function": {
        "name": "browser_get_interactive_elements",
        "description": (
            "Discover all clickable and interactive elements on a page. "
            "Returns a structured list of buttons, links, inputs, menus, etc. with their "
            "CSS selectors, text content, roles, and positions.\n"
            "Use this BEFORE browser_click to find the correct selector for the element you want to click. "
            "Much more reliable than guessing selectors or writing custom JS.\n"
            "Set viewport=true to only get elements currently visible on screen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "viewport": {
                    "type": "boolean",
                    "description": "If true, only return elements currently visible in the viewport (default: false = all elements)"
                },
                "maxElements": {
                    "type": "integer",
                    "description": "Maximum number of elements to return (default: 200)"
                }
            },
            "required": ["tabId"]
        }
    }
}

BROWSER_TOOL_CLICK = {
    "type": "function",
    "function": {
        "name": "browser_click",
        "description": (
            "Click an element on the page using its CSS selector. "
            "Supports both left-click and right-click. The element is automatically scrolled into view.\n"
            "Use browser_get_interactive_elements first to discover available selectors. "
            "After clicking, use browser_screenshot or browser_read_tab to verify the result.\n"
            "For Canvas-rendered UIs where DOM elements don't exist, fall back to browser_execute_js "
            "with synthetic MouseEvent dispatching on the canvas element."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click (get this from browser_get_interactive_elements)"
                },
                "rightClick": {
                    "type": "boolean",
                    "description": "If true, perform a right-click (contextmenu event) instead of left-click (default: false)"
                },
                "scrollTo": {
                    "type": "boolean",
                    "description": "Whether to scroll the element into view before clicking (default: true)"
                }
            },
            "required": ["tabId", "selector"]
        }
    }
}

BROWSER_TOOL_HOVER = {
    "type": "function",
    "function": {
        "name": "browser_hover",
        "description": (
            "Hover over an element to trigger dropdown menus, tooltips, or hover states. "
            "This simulates mouse movement over the element, triggering mouseenter/mouseover events.\n"
            "Use this before clicking items in dropdown menus that require hover to reveal.\n"
            "After hovering, use browser_wait or time.sleep() to allow menu animation to complete, "
            "then use browser_get_interactive_elements to find newly revealed menu items."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to hover over"
                }
            },
            "required": ["tabId", "selector"]
        }
    }
}

BROWSER_TOOL_KEYBOARD = {
    "type": "function",
    "function": {
        "name": "browser_keyboard",
        "description": (
            "Send keyboard input to the page. Supports special keys and modifier combinations.\n"
            "Examples: 'Enter', 'Escape', 'Tab', 'Backspace', 'ArrowUp', 'Ctrl+S', 'Ctrl+Shift+P'\n"
            "Supported modifiers: Ctrl, Alt, Shift, Meta (Command on Mac)\n"
            "Special keys: Enter, Escape, Tab, Backspace, Delete, ArrowUp/Down/Left/Right, "
            "Home, End, PageUp, PageDown, F1-F12\n"
            "If no selector is specified, sends to the currently focused element."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "keys": {
                    "type": "string",
                    "description": "Keys to send. Use + to combine modifiers, e.g., 'Ctrl+S', 'Alt+Tab'"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of target element (optional, defaults to activeElement)"
                }
            },
            "required": ["tabId", "keys"]
        }
    }
}

BROWSER_TOOL_WAIT = {
    "type": "function",
    "function": {
        "name": "browser_wait",
        "description": (
            "Wait for an element to appear or wait for a specified time. "
            "This implements explicit wait strategy similar to Selenium WebDriverWait.\n"
            "Use this to wait for dynamically loaded content, animations, or AJAX requests.\n"
            "Parameters:\n"
            "- selector: CSS selector to wait for\n"
            "- condition: 'present' (in DOM), 'visible', or 'clickable' (default: 'present')\n"
            "- timeout: Maximum wait time in milliseconds (default: 5000)\n"
            "- time: Wait for specified seconds instead of waiting for element"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector to wait for (optional if using time parameter)"
                },
                "condition": {
                    "type": "string",
                    "enum": ["present", "visible", "clickable"],
                    "description": "Condition to wait for (default: 'present')"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum wait time in milliseconds (default: 5000)"
                },
                "time": {
                    "type": "number",
                    "description": "Wait for specified seconds instead of element (e.g., 0.5 for 500ms)"
                }
            },
            "required": ["tabId"]
        }
    }
}

BROWSER_TOOL_SUMMARIZE_PAGE = {
    "type": "function",
    "function": {
        "name": "browser_summarize_page",
        "description": (
            "Get a structured summary of a web page: framework detection, button/link counts, forms, tables, modals, etc.\n"
            "Returns concise metadata to quickly understand the page layout without reading full HTML.\n"
            "Useful for: 1) First-time exploration of an unknown page; 2) Detecting Canvas/SVG rendering; 3) Finding main interactive elements.\n"
            "NOTE: If canvasCount > 0, the page uses Canvas rendering — use browser_screenshot to see the visual layout, then browser_execute_js or browser_get_app_state to access app data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                }
            },
            "required": ["tabId"]
        }
    }
}

BROWSER_TOOL_GET_APP_STATE = {
    "type": "function",
    "function": {
        "name": "browser_get_app_state",
        "description": (
            "Extract application state from the page: Vue/React detection, component tree, chart data (G6/ECharts), and global variables.\n"
            "Returns framework-specific data like Vue instances, React version, graph nodes/edges, and interesting global vars (config, store, apiBase, etc.).\n"
            "Use this when browser_read_tab returns sparse text (Canvas-rendered apps) or when you need to access app-level data without reverse-engineering the JS.\n"
            "Especially useful for internal dashboards, data visualization tools, and SPA applications."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tabId": {
                    "type": "integer",
                    "description": "Tab ID from browser_list_tabs"
                },
                "depth": {
                    "type": "string",
                    "description": "Extraction depth: 'shallow' (default) or 'deep' (more aggressive data extraction)"
                }
            },
            "required": ["tabId"]
        }
    }
}

BROWSER_TOOLS = [
    BROWSER_TOOL_LIST_TABS,
    BROWSER_TOOL_READ_TAB,
    BROWSER_TOOL_EXECUTE_JS,
    BROWSER_TOOL_SCREENSHOT,
    BROWSER_TOOL_GET_INTERACTIVE_ELEMENTS,
    BROWSER_TOOL_CLICK,
    BROWSER_TOOL_HOVER,
    BROWSER_TOOL_KEYBOARD,
    BROWSER_TOOL_WAIT,
    BROWSER_TOOL_SUMMARIZE_PAGE,
    BROWSER_TOOL_GET_APP_STATE,
    BROWSER_TOOL_GET_COOKIES,
    BROWSER_TOOL_GET_HISTORY,
    BROWSER_TOOL_CREATE_TAB,
    BROWSER_TOOL_CLOSE_TAB,
    BROWSER_TOOL_NAVIGATE,
]
BROWSER_TOOL_NAMES = {
    'browser_list_tabs', 'browser_read_tab', 'browser_execute_js',
    'browser_screenshot', 'browser_get_interactive_elements', 'browser_click',
    'browser_hover', 'browser_keyboard', 'browser_wait',
    'browser_summarize_page', 'browser_get_app_state',
    'browser_get_cookies', 'browser_get_history',
    'browser_create_tab', 'browser_close_tab', 'browser_navigate',
}

__all__ = [
    'BROWSER_TOOL_LIST_TABS', 'BROWSER_TOOL_READ_TAB', 'BROWSER_TOOL_EXECUTE_JS',
    'BROWSER_TOOL_SCREENSHOT', 'BROWSER_TOOL_GET_INTERACTIVE_ELEMENTS', 'BROWSER_TOOL_CLICK',
    'BROWSER_TOOL_HOVER', 'BROWSER_TOOL_KEYBOARD', 'BROWSER_TOOL_WAIT',
    'BROWSER_TOOL_SUMMARIZE_PAGE', 'BROWSER_TOOL_GET_APP_STATE',
    'BROWSER_TOOL_GET_COOKIES', 'BROWSER_TOOL_GET_HISTORY',
    'BROWSER_TOOL_CREATE_TAB', 'BROWSER_TOOL_CLOSE_TAB', 'BROWSER_TOOL_NAVIGATE',
    'BROWSER_TOOLS', 'BROWSER_TOOL_NAMES',
]
