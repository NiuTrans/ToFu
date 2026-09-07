"""Current LLM-facing browser handlers.

Each extension-backed handler has the uniform signature
``handler(arguments, BrowserToolRuntime)``. Internal wire-level readers stay
in their owning modules and are not re-exported as callable tool names.
"""

from ._capture import (
    _handle_execute_js,
    _handle_get_cookies,
    _handle_get_history,
    _handle_screenshot,
)
from ._devtools import _handle_devtools
from ._interact import _handle_click, _handle_press_key, _handle_type
from ._page import _handle_read_page
from ._preview import _handle_preview_page
from ._research import _handle_research_page
from ._tabs import _handle_close_tab, _handle_list_tabs, _handle_navigate

__all__ = [
    '_handle_click',
    '_handle_close_tab',
    '_handle_devtools',
    '_handle_execute_js',
    '_handle_get_cookies',
    '_handle_get_history',
    '_handle_list_tabs',
    '_handle_navigate',
    '_handle_press_key',
    '_handle_preview_page',
    '_handle_read_page',
    '_handle_research_page',
    '_handle_screenshot',
    '_handle_type',
]
