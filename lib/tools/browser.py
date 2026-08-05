"""lib/tools/browser.py — Browser extension tool definitions (v2 surface).

v2 (epic pt_869e5648403e4745) — intent-first consolidation, 19 → 11:

  * ONE perception entry: browser_read_page absorbs read_tab /
    summarize_page / get_interactive_elements / get_app_state. Its auto
    mode does the canvas/SPA diagnosis in code instead of asking the model
    which rendering technology the page uses.
  * Actions say WHAT, not HOW: browser_click / browser_type accept text=
    (fuzzy-matched server-side), auto-wait for the element, and return a
    page-state receipt so verification costs no extra LLM round.
    browser_keyboard split into browser_type (clear-first text entry) and
    browser_press_key (special keys).
  * browser_navigate absorbs browser_create_tab (new_tab=true) and waits
    for load by default.
  * browser_wait is gone from the model surface — waiting is handled
    inside the actions.
  * tab_id is OPTIONAL everywhere: the server remembers the working tab.

The removed names keep their dispatch handlers (direct execute_browser_tool
callers) and their display formatters (history rendering) — they are only
gone from the MODEL's schema list. See LEGACY_BROWSER_TOOL_NAMES.
"""

from lib.log import get_logger

logger = get_logger(__name__)

_TAB_ID_OPT = {
    "type": "integer",
    "description": "Tab ID. Omit to use the current working tab (the one you last acted on, else the active tab)."
}

BROWSER_TOOL_LIST_TABS = {
    "type": "function",
    "function": {
        "name": "browser_list_tabs",
        "description": (
            "List all open browser tabs with their titles, URLs, and tab IDs. "
            "You usually do NOT need this before acting — every browser tool "
            "defaults to the current working tab. Use it to pick a DIFFERENT "
            "tab than the one you were working with."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        }
    }
}

BROWSER_TOOL_READ_PAGE = {
    "type": "function",
    "function": {
        "name": "browser_read_page",
        "description": (
            "Read a web page from the user's real browser (works on login-walled "
            "pages: internal tools, logged-in dashboards).\n"
            "mode='auto' (default) picks the best representation FOR you: page text "
            "when it is substantive; when the text is sparse (Canvas/SVG/SPA page) "
            "it automatically attaches a structural summary (framework, forms, "
            "canvas count) with concrete next steps — no diagnosis needed on your part.\n"
            "mode='text' forces DOM text (optionally scoped by selector). "
            "mode='elements' lists interactive elements (buttons/links/inputs with "
            "selectors) — rarely needed since browser_click/browser_type accept text=. "
            "mode='app_state' extracts Vue/React state and chart data (G6/ECharts)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "mode": {
                    "type": "string",
                    "enum": ["auto", "text", "elements", "app_state"],
                    "description": "What representation to return (default: auto)"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector to scope text extraction (optional)"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 30000 in auto, 50000 in text)"
                }
            },
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
            "This is the escape hatch: prefer browser_click / browser_type for "
            "interactions and browser_read_page for reading — they handle waiting, "
            "targeting and verification for you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "code": {
                    "type": "string",
                    "description": "JavaScript code to execute in the page context"
                },
                "description": {
                    "type": "string",
                    "description": "ALWAYS provide a short one-line summary (in the user's language) of what this JS does and why. It is rendered as a caption above the code in the UI so the user can grasp the intent at a glance without parsing the script. E.g. 'Extract the flight prices from the results grid', 'Read the logged-in user id from window state'."
                }
            },
            "required": ["code"]
        }
    }
}

BROWSER_TOOL_SCREENSHOT = {
    "type": "function",
    "function": {
        "name": "browser_screenshot",
        "description": (
            "Capture a screenshot of a browser tab. By default returns a FULL-PAGE screenshot "
            "of the entire scrollable content (not just the visible viewport) — captured in one shot "
            "via the Chrome DevTools Protocol, which also triggers lazy-loaded content.\n"
            "Returns the screenshot as an IMAGE you can actually SEE and analyze visually (not base64 text!).\n"
            "Use this to: 1) Understand page layout; 2) See Canvas-rendered content (charts, graphs, DAG diagrams); "
            "3) Verify click/navigation results; 4) Read text from images when DOM extraction fails.\n"
            "Prefer the default full-page capture so you don't miss content below the fold. "
            "Only set full_page=false when you specifically need just the current viewport. "
            "Large images are auto-compressed to JPEG."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {
                    "type": "integer",
                    "description": "Tab ID to screenshot. If omitted, captures the currently active tab."
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Image format (default: png)"
                },
                "full_page": {
                    "type": "boolean",
                    "description": "If true (default), capture the entire scrollable page. If false, capture only the visible viewport."
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
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 100)"
                }
            },
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
                "tab_id": {
                    "type": "integer",
                    "description": "Single tab ID to close"
                },
                "tab_ids": {
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
            "Navigate a browser tab to a URL — or open a NEW tab with new_tab=true "
            "(background by default, does not interrupt the user). "
            "Waits for the page to load by default and reports the final URL/title.\n"
            "If you are NOT certain of the exact URL, FIRST call web_search to obtain the real "
            "link — never guess or reconstruct a domain from memory (guessed URLs 404 or land on "
            "the wrong site)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "url": {
                    "type": "string",
                    "description": "URL to navigate to"
                },
                "new_tab": {
                    "type": "boolean",
                    "description": "Open the URL in a new tab instead of reusing the working tab (default: false). The new tab becomes the working tab."
                },
                "active": {
                    "type": "boolean",
                    "description": "Only with new_tab=true: whether the new tab steals focus (default: false)"
                },
                "wait_for_load": {
                    "type": "boolean",
                    "description": "Wait for the page to fully load before returning (default true)"
                }
            },
            "required": ["url"]
        }
    }
}

BROWSER_TOOL_CLICK = {
    "type": "function",
    "function": {
        "name": "browser_click",
        "description": (
            "Click an element in the user's browser. Say WHAT to click, not how: "
            "text='登录' fuzzy-matches a button/link by visible text or aria-label "
            "(preferred); selector='#id' is the explicit fallback. "
            "The element is automatically awaited and scrolled into view, and the result "
            "reports whether the page changed (URL/title) — you usually do NOT need a "
            "separate verification read afterwards.\n"
            "For hover/right-click menus use browser_menu_click. For typing into fields "
            "use browser_type; for 2+ fields use ONE browser_fill_form call. "
            "On ambiguity the closest candidates are returned — retry with more specific text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "text": {
                    "type": "string",
                    "description": "Visible text / aria-label of the element to click (fuzzy-matched, preferred)"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element (explicit alternative to text)"
                },
                "right_click": {
                    "type": "boolean",
                    "description": "Right-click instead of left-click (default: false). To then pick a context-menu item, use browser_menu_click instead."
                },
                "scroll_to": {
                    "type": "boolean",
                    "description": "Scroll the element into view before clicking (default: true)"
                }
            },
        }
    }
}

BROWSER_TOOL_TYPE = {
    "type": "function",
    "function": {
        "name": "browser_type",
        "description": (
            "Type text into a field, REPLACING its current content (clear-first). "
            "Say WHICH field, not how: text='搜索' fuzzy-matches the input by "
            "placeholder/label/aria-label (preferred); selector='#q' is the explicit fallback.\n"
            "To fill or change 2+ fields (e.g. origin AND destination AND date), use ONE "
            "browser_fill_form call instead — it is faster and less error-prone. "
            "For Enter/Tab/Escape and keyboard shortcuts use browser_press_key."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "text": {
                    "type": "string",
                    "description": "Placeholder / label / aria-label of the field (fuzzy-matched, preferred)"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the field (explicit alternative to text)"
                },
                "value": {
                    "type": "string",
                    "description": "The text to type into the field"
                },
                "clear_first": {
                    "type": "boolean",
                    "description": "Clear the existing content before typing (default: true — changing a pre-filled field replaces it cleanly)"
                }
            },
            "required": ["value"]
        }
    }
}

BROWSER_TOOL_PRESS_KEY = {
    "type": "function",
    "function": {
        "name": "browser_press_key",
        "description": (
            "Press a key or key combination: 'Enter', 'Escape', 'Tab', 'Backspace', "
            "'ArrowUp/Down/Left/Right', 'Home', 'End', 'PageUp/Down', 'F1-F12', "
            "or combos like 'Ctrl+S', 'Ctrl+Shift+P' (modifiers: Ctrl, Alt, Shift, Meta).\n"
            "Goes to the currently focused element unless selector is given.\n"
            "NOTE: keystrokes APPEND — to enter text into a field use browser_type "
            "(clear-first) or browser_fill_form (2+ fields), not repeated press_key calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "keys": {
                    "type": "string",
                    "description": "Keys to send. Use + to combine modifiers, e.g., 'Ctrl+S'"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of target element (optional, defaults to the focused element)"
                }
            },
            "required": ["keys"]
        }
    }
}

BROWSER_TOOL_PREVIEW_PAGE = {
    "type": "function",
    "function": {
        "name": "browser_preview_page",
        "description": (
            "Render a web page in a headless browser ON THE SERVER and return a real "
            "screenshot you can SEE, plus console messages, uncaught JS errors and failed "
            "requests. This is how you check what a page YOU wrote looks like when it runs.\n"
            "Two modes:\n"
            "1) path: a project-relative .html file — served to the browser from the project "
            "root (relative assets and ES modules work; external network requests are blocked "
            "and reported).\n"
            "2) url: an http(s) URL, e.g. a dev server you or the user started.\n"
            "Use after writing/editing front-end code to verify layout visually and catch "
            "runtime JS errors. NOT for reading text content (use fetch_url / browser_read_page) "
            "and NOT tied to the user's browser extension — it runs fully server-side."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative HTML file to render (e.g. 'dist/index.html'). Mutually exclusive with url."
                },
                "url": {
                    "type": "string",
                    "description": "http(s) URL to render (e.g. 'http://127.0.0.1:8080/'). Mutually exclusive with path."
                },
                "width": {"type": "integer", "description": "Viewport width px (default 1280)"},
                "height": {"type": "integer", "description": "Viewport height px (default 800)"},
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the entire scrollable page instead of just the viewport (default false)"
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Extra settle time in ms after the DOM loads before screenshotting (default 1500, max 15000) — raise it for pages with async rendering"
                }
            },
            "required": []
        }
    }
}

#: The preview tool is part of the browser FAMILY for dispatch/display, but
#: deliberately NOT in BROWSER_TOOLS: those ship only when the user's browser
#: extension is connected, while the preview renders server-side in the
#: shared Playwright pool (its own ToolSpec gate in tools/registry/_build.py).
PAGE_PREVIEW_TOOL_NAMES = frozenset({'browser_preview_page'})

BROWSER_TOOLS = [
    BROWSER_TOOL_LIST_TABS,
    BROWSER_TOOL_READ_PAGE,
    BROWSER_TOOL_EXECUTE_JS,
    BROWSER_TOOL_SCREENSHOT,
    BROWSER_TOOL_CLICK,
    BROWSER_TOOL_TYPE,
    BROWSER_TOOL_PRESS_KEY,
    BROWSER_TOOL_NAVIGATE,
    BROWSER_TOOL_CLOSE_TAB,
    BROWSER_TOOL_GET_COOKIES,
    BROWSER_TOOL_GET_HISTORY,
]
BROWSER_TOOL_NAMES = {
    'browser_list_tabs', 'browser_read_page', 'browser_execute_js',
    'browser_screenshot', 'browser_click', 'browser_type', 'browser_press_key',
    'browser_navigate', 'browser_close_tab',
    'browser_get_cookies', 'browser_get_history',
}

#: Names REMOVED from the model surface by the v2 consolidation
#: (pt_869e5648403e4745). Their dispatch handlers and display formatters
#: stay — old conversations must keep rendering their tool cards, and direct
#: execute_browser_tool callers keep working. Consumers: the frontend icon
#: map and lib/tasks_pkg/tool_display/_dispatch.py (history display).
LEGACY_BROWSER_TOOL_NAMES = frozenset({
    'browser_read_tab', 'browser_get_interactive_elements',
    'browser_summarize_page', 'browser_get_app_state',
    'browser_wait', 'browser_hover', 'browser_keyboard',
    'browser_create_tab', 'browser_hover_and_click',
    'browser_right_click_menu',
})

__all__ = [
    'BROWSER_TOOL_LIST_TABS', 'BROWSER_TOOL_READ_PAGE', 'BROWSER_TOOL_EXECUTE_JS',
    'BROWSER_TOOL_SCREENSHOT', 'BROWSER_TOOL_CLICK', 'BROWSER_TOOL_TYPE',
    'BROWSER_TOOL_PRESS_KEY', 'BROWSER_TOOL_NAVIGATE', 'BROWSER_TOOL_CLOSE_TAB',
    'BROWSER_TOOL_GET_COOKIES', 'BROWSER_TOOL_GET_HISTORY',
    'BROWSER_TOOL_PREVIEW_PAGE', 'PAGE_PREVIEW_TOOL_NAMES',
    'BROWSER_TOOLS', 'BROWSER_TOOL_NAMES', 'LEGACY_BROWSER_TOOL_NAMES',
]
