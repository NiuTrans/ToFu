"""lib/memory/prefetch/_shortlist.py — BM25 coarse stage.

Reuses lib/memory/relevance's tokenizer/doc builder so the coarse ranking
is consistent with relevance.search_memories.
"""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.relevance import _build_memory_doc
from lib.memory.relevance._score import _score_token_documents

from lib.memory.prefetch._config import PREFETCH_BM25_TOP_N

logger = get_logger(__name__)


def _bm25_top_n(memories: list[dict], query: str,
                top_n: int = PREFETCH_BM25_TOP_N,
                *, include_body: bool = False) -> list[tuple[int, float]]:
    """Return [(memory_index, score), ...] sorted by BM25 score descending.

    Only memories with score > 0 are returned.  Uses the same tokenizer
    and document construction as relevance.search_memories for consistency.
    """
    if not query or not memories:
        return []

    scored = [
        (index, score)
        for index, score in _score_token_documents(
            query,
            (_build_memory_doc(memory, include_body=include_body)
             for memory in memories),
        )
        if score > 0
    ]

    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]
