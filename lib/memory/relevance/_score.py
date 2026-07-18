"""lib/memory/relevance/_score.py — generic BM25 scorer over text snippets.

A thin reuse of the shared tokenizer + BM25 formula, generalised to plain
strings so callers (e.g. the preference-profile detail tier) can relevance-gate
arbitrary text without re-implementing a scorer.
"""

import math

from lib.log import get_logger
from lib.memory.relevance._tokenize import BM25_B, BM25_K1, _tokenize

logger = get_logger(__name__)


def score_items(query: str, items: list[str]) -> list[tuple[int, float]]:
    """Score each snippet in *items* against *query* with BM25.

    A thin reuse of the same tokenizer + BM25 formula that backs
    :func:`filter_relevant_memories` / :func:`search_memories`, generalised
    to plain strings so callers (e.g. the preference-profile detail tier) can
    relevance-gate arbitrary text without re-implementing a scorer.

    Args:
        query: The query text (typically the last user message).
        items: Snippets to score (e.g. profile bullet lines).

    Returns:
        ``[(index, score), ...]`` sorted by score descending (index-stable on
        ties), covering ONLY items with score > 0. An empty query or empty
        item list yields ``[]``.
    """
    if not query or not items:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    n = len(items)
    docs = [_tokenize(it) for it in items]
    doc_lens = [len(d) for d in docs]
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    query_terms = set(query_tokens)
    df: dict[str, int] = {term: sum(1 for doc in docs if term in doc)
                          for term in query_terms}

    scored: list[tuple[int, float]] = []
    for i, (doc, dl) in enumerate(zip(docs, doc_lens)):
        score = 0.0
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in query_terms:
                tf_map[t] = tf_map.get(t, 0) + 1
        for term in query_terms:
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

    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored
