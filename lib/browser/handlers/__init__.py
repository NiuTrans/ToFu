"""lib/browser/handlers/ — Tool handler functions for each browser tool.

Each handler takes fn_args (dict) and returns a string result for the LLM.
They communicate with the browser extension via send_browser_command().

This package is a re-export facade: it preserves the original
``lib.browser.handlers`` import path byte-for-byte. Every symbol in the
original ``__all__`` remains importable as ``from lib.browser.handlers import X``.
The implementations live in cohesive sub-modules:

  * ``_tabs``     — list/read/create/close tabs + navigate
  * ``_interact`` — get_interactive_elements + click/keyboard/hover/wait
  * ``_capture``  — screenshot/execute_js/get_cookies/get_history
  * ``_page``     — summarize_page/get_app_state

CRITICAL — monkeypatch contract: the original flat module imported
``send_browser_command`` at module level, so existing tests patch
``lib.browser.handlers.send_browser_command`` and expect the handlers to
honour it. This facade re-exports the real ``send_browser_command`` and each
sub-module resolves it THROUGH this facade at call time (see each
sub-module's ``_facade()`` helper), so patching the facade name still works.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Collaborator re-exported for backwards-compatible monkeypatching.
from lib.browser.queue import send_browser_command  # noqa: F401

# ── Re-export every handler from its sub-module (facade) ────────────────────
from lib.browser.handlers._tabs import (  # noqa: E402,F401
    _extract_best_text,
    _handle_list_tabs,
    _handle_read_tab,
    _handle_create_tab,
    _handle_close_tab,
    _handle_navigate,
    _render_read_result,
)
from lib.browser.handlers._interact import (  # noqa: E402,F401
    _handle_get_interactive_elements,
    _handle_click,
    _handle_keyboard,
    _handle_hover,
    _handle_wait,
    _handle_type,
    _handle_press_key,
)
from lib.browser.handlers._capture import (  # noqa: E402,F401
    _handle_execute_js,
    _handle_screenshot,
    _handle_get_cookies,
    _handle_get_history,
)
from lib.browser.handlers._page import (  # noqa: E402,F401
    _handle_summarize_page,
    _handle_get_app_state,
    _handle_read_page,
)
from lib.browser.handlers._preview import (  # noqa: E402,F401
    _handle_preview_page,
)

__all__ = [
    '_handle_list_tabs', '_handle_read_tab', '_handle_execute_js',
    '_handle_screenshot', '_handle_get_cookies', '_handle_get_history',
    '_handle_create_tab', '_handle_close_tab', '_handle_navigate',
    '_handle_get_interactive_elements', '_handle_click', '_handle_keyboard',
    '_handle_hover', '_handle_wait', '_handle_summarize_page',
    '_handle_get_app_state', '_handle_preview_page',
    '_handle_read_page', '_handle_type', '_handle_press_key',
    '_extract_best_text', '_render_read_result',
]
