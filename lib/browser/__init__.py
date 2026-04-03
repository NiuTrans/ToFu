"""lib/browser/ — Browser extension bridge: command queue, tool handlers, dispatch.

Façade package — all public API is re-exported here::

    from lib.browser import send_browser_command, execute_browser_tool, is_extension_connected
    from lib.browser import browser_tool_display, fetch_url_via_browser
    from lib.browser import ADVANCED_BROWSER_TOOLS, ADVANCED_BROWSER_TOOL_NAMES
"""

from lib._pkg_utils import build_facade, safe_import

__all__: list[str] = []
_import = safe_import(__name__, globals(), __all__)

# ── Core queue infrastructure (must load) ──
from . import queue
from .queue import *  # noqa: F401,F403

build_facade(__all__, queue)

# ── Dispatch (must load) ──
from . import dispatch
from .dispatch import *  # noqa: F401,F403

build_facade(__all__, dispatch)

# ── Display helpers ──
from . import display
from .display import *  # noqa: F401,F403

build_facade(__all__, display)

# ── Fetch via browser ──
from . import fetch
from .fetch import *  # noqa: F401,F403

build_facade(__all__, fetch)

# ── Advanced tools ──
from . import advanced
from .advanced import *  # noqa: F401,F403

build_facade(__all__, advanced)
