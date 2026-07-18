"""lib/memory/prefetch — Per-turn proactive memory surfacing.

Pipeline (round 0 only, once per user turn):

    1. Build a query from the recent conversational surface (last K
       user+assistant text turns, stripping all tool_calls / tool_results
       / thinking blocks).
    2. BM25 coarse ranking over name+description+tags+body → top-N candidates.
    3. Cheap-LLM precision filter:  given the recent turns + candidate
       summaries, the cheap model returns the JSON list of memory indices
       that are directly relevant — preferring precision over recall.
    4. Inject the selected memories (full body) into the last user message
       as a ``<relevant_memories>`` block wrapped in ``<system-reminder>``.

Every stage emits an SSE ``memory_prefetch`` event so the frontend can
show the user that a cheap model is filtering memories in the background
(otherwise the latency would feel unexplained).

The mechanism is a PROACTIVE companion to the model's explicit
``search_memories`` tool — it fixes the class of failures where the model
doesn't realise a relevant memory exists and therefore never searches.

Design note (no-fallback policy): the cheap-LLM reranker runs under a hard
wall-clock deadline but with NO exception handling of dispatch failures. If
the cheap call fails or hangs past the deadline, we inject NOTHING. We
deliberately do NOT fall back to BM25 top-K, because a noisy BM25 injection
tends to waste tokens and distract the main model more than it helps.

Feature-flagged via ``features.json → memory_prefetch`` (default ``True``).
Environment-variable override: ``MEMORY_PREFETCH=0`` disables.

No implementation lives in this file — it is a pure re-export facade. All
code lives in the sub-modules (_config / _query / _shortlist / _rerank /
_inject / _run); importing them here keeps every historical
``from lib.memory.prefetch import X`` working byte-identically. In
particular ``lib/memory/__init__.py`` does ``from .prefetch import *`` and
``lib/memory/profile_consolidate.py`` imports the privates
``_build_recent_turns_text`` + ``_extract_first_balanced_object``.
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Tunables + resolved flags (._config) ──
from lib.memory.prefetch._config import (  # noqa: E402,F401
    PREFETCH_BM25_TOP_N,
    PREFETCH_BODY_PREVIEW_LEN,
    PREFETCH_DEADLINE_MS,
    PREFETCH_ENABLED,
    PREFETCH_MAX_BYTES,
    PREFETCH_MAX_INJECTED,
    PREFETCH_MIN_CANDIDATES,
    PREFETCH_RECENT_TURNS_K,
    _MAX_QUERY_CHARS,
    _SUMMARY_BODY_CAP,
)

# ── Query construction (._query) ──
from lib.memory.prefetch._query import (  # noqa: E402,F401
    _build_recent_turns_text,
    _extract_current_user_request,
    _msg_plain_text,
)

# ── BM25 coarse stage (._shortlist) ──
from lib.memory.prefetch._shortlist import _bm25_top_n  # noqa: E402,F401

# ── Cheap-LLM precision stage (._rerank) ──
from lib.memory.prefetch._rerank import (  # noqa: E402,F401
    _DEADLINE_SENTINEL,
    _RERANK_SYSTEM_PROMPT,
    _call_cheap_reranker,
    _extract_first_balanced_object,
    _format_active_environment,
    _format_candidates_for_rerank,
    _parse_rerank_response,
    _run_with_deadline,
    _salvage_ids_from_truncated,
)

# ── Injection stage (._inject) ──
from lib.memory.prefetch._inject import (  # noqa: E402,F401
    _RELEVANT_MEMORIES_TAG,
    _render_relevant_memories_block,
    inject_relevant_memories,
)

# ── Orchestration entry point (._run) ──
from lib.memory.prefetch._run import run_memory_prefetch  # noqa: E402,F401

# ``__all__`` preserved VERBATIM from the pre-split module so
# ``from lib.memory.prefetch import *`` (used by lib/memory/__init__.py)
# behaves byte-identically.
__all__ = [
    'run_memory_prefetch',
    'inject_relevant_memories',
    'PREFETCH_ENABLED',
    'PREFETCH_BM25_TOP_N',
    'PREFETCH_MAX_INJECTED',
]
