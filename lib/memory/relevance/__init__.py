"""lib/memory/relevance — BM25-based memory relevance scoring.

Lightweight BM25 scorer that ranks memories by relevance to a query string.
No external dependencies — uses only stdlib math.

Used to reduce the number of memories injected per turn from 100+ to ~30,
cutting context consumption while preserving discoverability.

No code lives in this file — it is a pure re-export facade. All implementations
live in the sub-modules (``_tokenize`` / ``_score`` / ``_search``); importing
them here keeps every historical ``from lib.memory.relevance import X`` working
byte-identically. This module is STATELESS (pure scoring).
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Tokenizer + shared constants (._tokenize) ──
from lib.memory.relevance._tokenize import (  # noqa: E402,F401
    BM25_B,
    BM25_K1,
    DEFAULT_TOP_K,
    _CJK_RUN_RE,
    _STOP_WORDS,
    _TOKENIZE_RE,
    _build_memory_doc,
    _cjk_tokens,
    _tokenize,
)

# ── Generic BM25 scorer over snippets (._score) ──
from lib.memory.relevance._score import score_items  # noqa: E402,F401

# ── Memory-facing entrypoints (._search) ──
from lib.memory.relevance._search import (  # noqa: E402,F401
    SEARCH_DEFAULT_TOP_K,
    filter_relevant_memories,
    search_memories,
)

__all__ = ['filter_relevant_memories', 'search_memories', 'score_items']
