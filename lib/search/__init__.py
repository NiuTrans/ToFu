"""lib/search/ — Multi-engine web search with dedup, reranking, and formatting.

Façade package — all public API is re-exported here::

    from lib.search import perform_web_search, format_search_for_tool_response
"""

from lib._pkg_utils import build_facade, safe_import

__all__: list[str] = []
_import = safe_import(__name__, globals(), __all__)

# ── Core (must load) ──
from . import format as _format_mod
from . import orchestrator
from .format import *  # noqa: F401,F403
from .orchestrator import *  # noqa: F401,F403

build_facade(__all__, orchestrator, _format_mod)

# ── Optional sub-modules (degrade gracefully) ──
_import('dedup', 'content deduplication')
_import('rerank', 'BM25 reranking')
_import('browser_fallback', 'browser search fallback')
