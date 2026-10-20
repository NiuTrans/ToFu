"""Single source of truth for model-facing browser-extension schemas.

The catalogue is intent-first: one page reader chooses the useful
representation, actions resolve human-readable targets and wait internally,
navigation owns both same-tab and new-tab behavior, and every tab argument is
optional because the owner/device-scoped runtime remembers its working tab.
Dispatch accepts only these names and snake_case argument shapes.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.browser.content_contract import READ_TARGET_SCHEMA

_BROWSER_SITE_PREAMBLE = (
    'Run a native site capability, NOT web search. Omit site to discover; '
    'pass site to inspect schemas; add operation/params to execute. '
    'No web operators. Uses the selected logged-in browser.')

#: Bound on the registry-generated portion of the browser_site description:
#: hints are one short clause per registered site, so prompt growth stays
#: predictable no matter how many packs a user drops in.
_MAX_SITE_HINTS = 16
_MAX_SITE_DESCRIPTION_CHARS = 4096

BROWSER_TOOL_SITE = {
    'type': 'function',
    'function': {
        'name': 'browser_site',
        'description': _BROWSER_SITE_PREAMBLE,
        'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'site': {'type': 'string', 'maxLength': 64},
                'operation': {'type': 'string', 'maxLength': 64},
                'params': {'type': 'object', 'maxProperties': 16},
            },
        },
    },
}

_TAB_ID_OPT = {
    "type": "integer",
    "description": "Tab ID; omit for the current working tab."
}

BROWSER_TOOL_LIST_TABS = {
    "type": "function",
    "function": {
        "name": "browser_list_tabs",
        "description": (
            "List open browser tabs with titles, URLs and IDs. Use this only "
            "to choose a different tab; other tools use the working tab. Tabs "
            "marked [automation tab] are the ones tab_id-less calls act on; a "
            "tab the user opened (the Tofu chat, their reading, their form) "
            "is reached only by passing its id explicitly."
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
            "Read logged-in content with target/coverage. auto selects useful content; "
            "text uses DOM; data uses relevant APIs; elements lists controls; "
            "app_state inspects frameworks/charts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "target": READ_TARGET_SCHEMA,
                "mode": {

                    "type": "string",
                    "enum": ["auto", "text", "data", "elements", "app_state"],
                    "description": "What representation to return (default: auto)"
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS scope for text mode."
                }
            },
        }
    }
}

BROWSER_TOOL_RESEARCH_PAGE = {
    "type": "function",
    "function": {
        "name": "browser_research_page",
        "description": (
            "Deep-read an exact URL with bounded same-origin traversal, target extraction "
            "and coverage. For a named internal service without a URL, use "
            "browser_get_history first; never guess its hostname."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string"
                },
                "target": READ_TARGET_SCHEMA,
                "mode": {
                    "type": "string",
                    "enum": ["both", "content", "analysis"],
                    "description": "both (default), content, or analysis."
                },
                "max_scrolls": {
                    "type": "integer",
                    "description": (
                        "Lazy-list scrolls per page (default 4, max 8)."
                    )
                },
                "max_pages": {
                    "type": "integer",
                    "description": (
                        "Same-origin page limit (default 3, max 5)."
                    )
                },
                "pagination": {
                    "type": "string",
                    "enum": ["auto", "links", "none"],
                    "description": "auto (default), next links only, or none."
                }
            },
            "required": ["url"]
        }
    }
}

BROWSER_TOOL_EXECUTE_JS = {
    "type": "function",
    "function": {
        "name": "browser_execute_js",
        "description": (
            "Run one JavaScript expression/IIFE in the page MAIN world and return "
            "a JSON value. Prefer normal read/click/type tools; use browser_devtools "
            "for console history, promises, complex objects and debugger control. "
            "Arbitrary JS is always write-authorized. Pass tab_id for site-specific "
            "code or relative fetch URLs; browser_research_page uses a temporary tab "
            "and does not change the remembered working tab."
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
                    "description": "Short user-facing explanation of the script."
                }
            },
            "required": ["code"]
        }
    }
}

BROWSER_TOOL_DEVTOOLS = {
    "type": "function",
    "function": {
        "name": "browser_devtools",
        "description": (
            "Use a bounded DevTools Bridge in the user's Chrome: read console "
            "and exceptions, await/evaluate expressions, safely expand complex "
            "objects without invoking getters, inspect execution contexts, and "
            "debug JavaScript with scripts, breakpoints, pause and stepping. "
            "Call debug_start before debugger actions and debug_stop when done. "
            "Line/column numbers are zero-based. Prefer ordinary browser tools "
            "for routine page interaction."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "action": {
                    "type": "string",
                    "enum": [
                        "console_read", "console_clear", "context_list",
                        "evaluate", "inspect", "debug_start", "debug_state",
                        "debug_stop", "breakpoint_set", "breakpoint_remove",
                        "pause", "resume", "step_over", "step_into", "step_out",
                        "frame_evaluate", "script_source"
                    ]
                },
                "expression": {
                    "type": "string",
                    "description": "Expression for evaluate/inspect/frame_evaluate."
                },
                "await_promise": {
                    "type": "boolean",
                    "description": "Await a returned promise (default true)."
                },
                "observe_ms": {
                    "type": "integer",
                    "description": "Console observation window, 50–5000 ms."
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Object expansion depth, 0–6."
                },
                "session_ttl_ms": {
                    "type": "integer",
                    "description": "Debug session lifetime, 10000–120000 ms."
                },
                "context_id": {"type": "integer"},
                "session_id": {
                    "type": "string",
                    "description": "Related-target session from context/script state."
                },
                "source_url": {
                    "type": "string",
                    "description": "Exact script URL for a URL breakpoint."
                },
                "line_number": {"type": "integer"},
                "column_number": {"type": "integer"},
                "condition": {"type": "string"},
                "breakpoint_id": {"type": "string"},
                "call_frame_id": {"type": "string"},
                "script_id": {"type": "string"}
            },
            "required": ["action"]
        }
    }
}

BROWSER_TOOL_SCREENSHOT = {
    "type": "function",
    "function": {
        "name": "browser_screenshot",
        "description": (
            "Capture a visible image of a tab, including Canvas/SVG. Defaults to "
            "the full scrollable page; set full_page=false for the viewport."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": {
                    "type": "integer",
                    "description": "Tab ID; omit for the active tab."
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Image format (default: png)"
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Full page by default; false means viewport."
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
            "Inspect cookie METADATA in the user's browser, filtered by URL, domain, "
            "or name. Values are intentionally redacted and must remain inside Chrome. "
            "Use this only to diagnose whether a site has cookies; never use it to copy "
            "authentication into curl/wget or server HTTP. To fetch a logged-in page, "
            "use fetch_url. To place a remote file on the Tofu server, call "
            "browser_download_url_to_server with the exact file URL; it automatically uses the "
            "selected browser session when required."
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
            "Search the user's browser history. Returns URLs, titles, visit "
            "counts and timestamps. Use this to resolve a user-named internal "
            "service before browser_research_page when no exact URL was given."
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
            "Navigate the working tab, or a new one. Without an automation tab "
            "to continue in, this opens a BACKGROUND tab — it never repurposes "
            "or focuses the tab the user is using. Waits for load and reports "
            "final URL/title. The Tofu client tab is never navigated; targeting "
            "it opens a new tab instead. If unsure, use web_search; never guess "
            "a URL from memory."
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
                    "description": (
                        "Force a new working tab instead of continuing in the "
                        "current automation tab (default false)."
                    )
                },
                "active": {
                    "type": "boolean",
                    "description": (
                        "Whether the tab this call opens may take focus from "
                        "the user (default: false). Only ever opens a tab, so "
                        "it never brings an existing user tab forward."
                    )
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
            "Click by visible text/aria-label (preferred) or CSS selector. It waits, "
            "scrolls and reports navigation/NEW TAB results. Use menu_click for menus "
            "and browser_fill_form for multiple fields."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "text": {
                    "type": "string",
                    "description": "Visible text/aria-label, fuzzy matched."
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element (explicit alternative to text)"
                },
                "right_click": {
                    "type": "boolean",
                    "description": "Right-click (default false)."
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
            "Replace a field's text, targeting its label/placeholder (preferred) or "
            "CSS selector. Use fill_form for 2+ fields and press_key for shortcuts."
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
            "Press a key or combination such as Enter, Escape or Ctrl+Shift+P. "
            "Targets the focused element unless selector is supplied; use type for "
            "text and browser_fill_form for multiple fields."
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

BROWSER_TOOL_SCROLL = {
    "type": "function",
    "function": {
        "name": "browser_scroll",
        "description": (
            "Scroll the page (or bring an element into view) to reveal more "
            "content: down/up by an amount, to top/bottom, or to a CSS "
            "selector. Returns the new scroll position and whether top/bottom "
            "was reached. Read-only — only the viewport changes, never page "
            "state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom", "left", "right"],
                    "description": "Direction to scroll (default 'down')."
                },
                "amount": {
                    "type": "integer",
                    "description": "Pixels to scroll (default ~75% of the viewport)."
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of an element to scroll into view (overrides direction/amount)."
                }
            },
        }
    }
}

BROWSER_TOOL_CHECK = {
    "type": "function",
    "function": {
        "name": "browser_check",
        "description": (
            "Set a radio/checkbox/switch to an exact state (idempotent), by "
            "visible label text (preferred) or CSS selector. Handles custom "
            "controls (MTD, Element-UI, Ant Design) that hide the real input — "
            "prefer over browser_click for radios/checkboxes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "text": {
                    "type": "string",
                    "description": "Visible label of the control, fuzzy matched."
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the control (explicit alternative to text)"
                },
                "checked": {
                    "type": "boolean",
                    "description": "Target state (default true); false unchecks."
                },
                "scroll_to": {
                    "type": "boolean",
                    "description": "Scroll the control into view first (default: true)"
                }
            },
        }
    }
}

BROWSER_TOOL_SELECT_OPTION = {
    "type": "function",
    "function": {
        "name": "browser_select_option",
        "description": (
            "Choose an option in a custom dropdown (MTD, Element-UI, Ant "
            "Design) or a native <select>: targets the trigger by text (preferred) "
            "or selector, opens it, and clicks the option a generic filler can't."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB_ID_OPT,
                "text": {
                    "type": "string",
                    "description": "Visible label/placeholder of the trigger, fuzzy matched."
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the trigger (explicit alternative to text)"
                },
                "option": {
                    "type": "string",
                    "description": "Visible text of the option to choose."
                },
                "exact": {
                    "type": "boolean",
                    "description": "Exact option-text match (default true); false allows substring."
                }
            },
            "required": ["option"]
        }
    }
}

BROWSER_TOOL_PREVIEW_PAGE = {
    "type": "function",
    "function": {
        "name": "browser_preview_page",
        "description": (
            "Render one page in a headless browser ON THE SERVER; return screenshot, "
            "console, uncaught JS errors, and failed requests. Use after frontend edits "
            "for layout/runtime. Give exactly one source: a "
            "project-relative .html path, served from project root so relative assets/ES "
            "modules work and external requests are blocked/reported; or an HTTP(S) url "
            "such as a dev server. Not for text (use fetch_url/browser_read_page) or the "
            "user's browser extension."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Project-relative .html/.htm file."
                },
                "url": {
                    "type": "string",
                    "minLength": 1,
                    "description": "HTTP(S) page URL."
                },
                "width": {
                    "type": "integer", "minimum": 320, "maximum": 3840,
                    "description": "Viewport width px; default 1280."
                },
                "height": {
                    "type": "integer", "minimum": 240, "maximum": 2160,
                    "description": "Viewport height px; default 800."
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page; default false."
                },
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 15000,
                    "description": "Settle after DOM load in ms; default 1500."
                }
            },
            "oneOf": [
                {
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]
                },
                {
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]
                }
            ],
            "additionalProperties": False
        }
    }
}

#: The preview tool is part of the browser FAMILY for dispatch/display, but
#: deliberately NOT in BROWSER_TOOLS: those ship only when the user's browser
#: extension is connected, while the preview renders server-side in the
#: shared Playwright pool (its own ToolSpec gate in tools/registry/_build.py).
PAGE_PREVIEW_TOOL_NAMES = frozenset({'browser_preview_page'})

BROWSER_TOOLS = [

    BROWSER_TOOL_SITE,
    BROWSER_TOOL_LIST_TABS,
    BROWSER_TOOL_READ_PAGE,
    BROWSER_TOOL_RESEARCH_PAGE,
    BROWSER_TOOL_DEVTOOLS,
    BROWSER_TOOL_EXECUTE_JS,
    BROWSER_TOOL_SCREENSHOT,
    BROWSER_TOOL_CLICK,
    BROWSER_TOOL_TYPE,
    BROWSER_TOOL_PRESS_KEY,
    BROWSER_TOOL_CHECK,
    BROWSER_TOOL_SELECT_OPTION,
    BROWSER_TOOL_SCROLL,
    BROWSER_TOOL_NAVIGATE,
    BROWSER_TOOL_CLOSE_TAB,
    BROWSER_TOOL_GET_COOKIES,
    BROWSER_TOOL_GET_HISTORY,
]
BROWSER_TOOL_NAMES = {

    'browser_site',
    'browser_list_tabs', 'browser_read_page', 'browser_research_page',
    'browser_devtools', 'browser_execute_js',
    'browser_screenshot', 'browser_click', 'browser_type', 'browser_press_key',
    'browser_check', 'browser_select_option', 'browser_scroll',
    'browser_navigate', 'browser_close_tab',
    'browser_get_cookies', 'browser_get_history',
}


def browser_site_tool() -> dict:
    """browser_site with a registry-generated routing description.

    Per-site hints come from the adapter registry at call time (pack
    ``routing_hint`` fields), so an unregistered site leaves no trace in the
    model's prompt. The static BROWSER_TOOL_SITE constant stays preamble-only.
    """
    hints: list[str] = []
    try:
        from lib.browser.adapters import list_adapters
        for adapter in list_adapters():
            if adapter.routing_hint:
                hints.append(f'{adapter.name}: {adapter.routing_hint}.')
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('[Tools] site-hint projection unavailable: %s', exc)
    description = _BROWSER_SITE_PREAMBLE
    if hints:
        description += ' ' + ' '.join(hints[:_MAX_SITE_HINTS])
    description = description[:_MAX_SITE_DESCRIPTION_CHARS]
    return {
        'type': BROWSER_TOOL_SITE['type'],
        'function': {**BROWSER_TOOL_SITE['function'],
                     'description': description},
    }


def browser_tools() -> list[dict]:
    """BROWSER_TOOLS with the site tool's description projected live."""
    return [browser_site_tool() if tool is BROWSER_TOOL_SITE else tool
            for tool in BROWSER_TOOLS]


__all__ = [

    'BROWSER_TOOL_SITE',
    'BROWSER_TOOL_LIST_TABS', 'BROWSER_TOOL_READ_PAGE',
    'BROWSER_TOOL_RESEARCH_PAGE', 'BROWSER_TOOL_DEVTOOLS',
    'BROWSER_TOOL_EXECUTE_JS',
    'BROWSER_TOOL_SCREENSHOT', 'BROWSER_TOOL_CLICK', 'BROWSER_TOOL_TYPE',
    'BROWSER_TOOL_PRESS_KEY', 'BROWSER_TOOL_CHECK', 'BROWSER_TOOL_SELECT_OPTION',
    'BROWSER_TOOL_NAVIGATE', 'BROWSER_TOOL_CLOSE_TAB',
    'BROWSER_TOOL_GET_COOKIES', 'BROWSER_TOOL_GET_HISTORY',
    'BROWSER_TOOL_SCROLL',
    'BROWSER_TOOL_PREVIEW_PAGE', 'PAGE_PREVIEW_TOOL_NAMES',
    'BROWSER_TOOLS', 'BROWSER_TOOL_NAMES',
    'browser_site_tool', 'browser_tools',
]
