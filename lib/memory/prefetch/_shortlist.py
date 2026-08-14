"""lib/memory/prefetch/_shortlist.py — BM25 coarse stage.

Reuses lib/memory/relevance's tokenizer/doc builder so the coarse ranking
is consistent with relevance.search_memories.
"""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.relevance import _tokenize, _build_memory_doc

from lib.memory.prefetch._config import PREFETCH_BM25_TOP_N

logger = get_logger(__name__)


def _bm25_top_n(memories: list[dict], query: str,
                top_n: int = PREFETCH_BM25_TOP_N,
                *, include_body: bool = False) -> list[tuple[int, float]]:
    """Return [(memory_index, score), ...] sorted by BM25 score descending.

    Only memories with score > 0 are returned.  Uses the same tokenizer
    and document construction as relevance.search_memories for consistency.
    """
    import math

    from lib.memory.relevance import BM25_K1, BM25_B

    q_tokens = _tokenize(query)
    if not q_tokens or not memories:
        return []

    docs = [_build_memory_doc(m, include_body=include_body) for m in memories]
    doc_lens = [len(d) for d in docs]
    n = len(memories)
    avg_dl = (sum(doc_lens) / n) if n > 0 else 1.0

    q_terms = set(q_tokens)
    df: dict[str, int] = {}
    for term in q_terms:
        df[term] = sum(1 for d in docs if term in d)

    scored: list[tuple[int, float]] = []
    for i, (doc, dl) in enumerate(zip(docs, doc_lens)):
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in q_terms:
                tf_map[t] = tf_map.get(t, 0) + 1
        score = 0.0
        for term in q_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            d = df.get(term, 0)
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl)
            score += idf * numerator / denominator
        if score > 0:
            scored.append((i, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]
