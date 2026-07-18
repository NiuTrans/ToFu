# HOT_PATH
"""Search-related tool handlers: web_search, fetch_url.

Facade-preserving package. Every symbol that lived in the old
``lib.tasks_pkg.handlers.search`` module is re-exported here so both
``from lib.tasks_pkg.handlers.search import X`` and
``import lib.tasks_pkg.handlers.search as search_h`` keep working
byte-identically.

MONKEYPATCH PARITY: ``_web_search_one`` / ``_fetch_url_one`` live in
``._core`` but are re-exported here as package attributes. The handler
orchestrators in ``._handlers`` resolve them THROUGH this package module at
call time, so ``patch('lib.tasks_pkg.handlers.search._web_search_one', ...)``
(and the ``_fetch_url_one`` equivalent) steer them exactly as before.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── tofu_search primitives re-exported as PACKAGE ATTRS so a test/consumer
#    patching ``lib.tasks_pkg.handlers.search.perform_web_search`` (etc.) steers
#    the _core primitives, which resolve these through this facade at call time.
#    (The old single-file module imported them at module level, making them
#    patchable on the module; this preserves that byte-identically.) ──
from tofu_search import (  # noqa: E402,F401
    perform_web_search,
    fetch_page_content,
    looks_like_text_asset,
)

# ── Core primitives (search/fetch) ──────────────────────────────────────────
from lib.tasks_pkg.handlers.search._core import (  # noqa: E402,F401
    resolve_vertical,
    _web_search_one,
    _safe_filename,
    _stage_binary_asset,
    _fetch_url_one,
)

# ── Display formatting helpers ───────────────────────────────────────────────
from lib.tasks_pkg.handlers.search._display import (  # noqa: E402,F401
    _format_fetch_display,
    _format_search_display_for_results,
    _vertical_to_sse_payload,
    _vertical_header_for_llm,
)

# ── Handler orchestrators (register @tool_registry.handler on import) ────────
from lib.tasks_pkg.handlers.search._handlers import (  # noqa: E402,F401
    _handle_web_search,
    _handle_web_search_batch,
    _handle_fetch_url,
    _handle_fetch_url_batch,
)

__all__ = [
    'perform_web_search',
    'fetch_page_content',
    'looks_like_text_asset',
    'resolve_vertical',
    '_web_search_one',
    '_fetch_url_one',
    '_safe_filename',
    '_stage_binary_asset',
    '_format_fetch_display',
    '_format_search_display_for_results',
    '_vertical_to_sse_payload',
    '_vertical_header_for_llm',
    '_handle_web_search',
    '_handle_web_search_batch',
    '_handle_fetch_url',
    '_handle_fetch_url_batch',
]
