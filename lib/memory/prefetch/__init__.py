"""lib/memory/prefetch — Per-turn proactive memory surfacing.

Pipeline (round 0 only, once per user turn):

    1. Build a query from the current user request plus explicit identifiers
       in the task checklist; prior assistant/tool text is excluded.
    2. Metadata-only BM25 ranking over name+description+tags.
    3. Deterministic high-confidence gates select at most two candidates.
    4. Context Composer injects selected bodies as turn-scoped evidence.

The selector is synchronous, local, and makes no auxiliary LLM call.

The mechanism is a PROACTIVE companion to the model's explicit
``search_memories`` tool — it fixes the class of failures where the model
doesn't realise a relevant memory exists and therefore never searches.

Feature-flagged via ``features.json → memory_prefetch`` (default ``True``).
Environment-variable override: ``MEMORY_PREFETCH=0`` disables.

No implementation lives in this file — it is a pure re-export facade. All
code lives in the sub-modules (_config / _query / _shortlist / _inject /
_run). ``lib/memory/__init__.py`` continues to re-export the public surface.
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Tunables + resolved flags (._config) ──
from lib.memory.prefetch._config import (  # noqa: E402,F401
    PREFETCH_BM25_TOP_N,
    PREFETCH_ENABLED,
    PREFETCH_MAX_BYTES,
    PREFETCH_MAX_INJECTED,
    PREFETCH_RECENT_TURNS_K,
    _MAX_QUERY_CHARS,
)

# ── Query construction (._query) ──
from lib.memory.prefetch._query import (  # noqa: E402,F401
    _build_recent_turns_text,
    _extract_current_user_request,
    _msg_plain_text,
)

# ── BM25 coarse stage (._shortlist) ──
from lib.memory.prefetch._shortlist import _bm25_top_n  # noqa: E402,F401

# ── Injection stage (._inject) ──
from lib.memory.prefetch._inject import (  # noqa: E402,F401
    _RELEVANT_MEMORIES_TAG,
    _render_relevant_memories_block,
)

# ── Orchestration entry point (._run) ──
from lib.memory.prefetch._run import run_memory_prefetch  # noqa: E402,F401

# ``__all__`` preserved VERBATIM from the pre-split module so
# ``from lib.memory.prefetch import *`` (used by lib/memory/__init__.py)
# behaves byte-identically.
__all__ = [
    'run_memory_prefetch',
    'PREFETCH_ENABLED',
    'PREFETCH_BM25_TOP_N',
    'PREFETCH_MAX_INJECTED',
]
