"""Single streaming BM25 core plus the public plain-text scorer.

The core retains only each document's length and query-term frequencies, never
the corpus-wide token lists. Memory search, automatic prefetch, and generic
snippet ranking share this formula so cost optimizations cannot drift scores.
"""

import math
from collections.abc import Iterable

from lib.log import get_logger
from lib.memory.relevance._tokenize import BM25_B, BM25_K1, _tokenize

logger = get_logger(__name__)


def _score_token_documents(
    query: str,
    token_documents: Iterable[list[str]],
) -> list[tuple[int, float]]:
    """Return source-ordered BM25 scores while consuming documents once.

    Only query-term frequencies survive each iteration. Peak retained state is
    therefore ``O(document_count × distinct_query_terms)`` instead of
    ``O(all_corpus_tokens)``. Every source document, including a zero-score
    document, receives one result so callers can preserve legacy fallback and
    stable-tie behavior.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    query_terms = set(query_tokens)
    document_stats: list[tuple[int, dict[str, int]]] = []
    document_frequency = {term: 0 for term in query_terms}
    total_document_length = 0

    for tokens in token_documents:
        document_length = len(tokens)
        total_document_length += document_length
        term_frequency: dict[str, int] = {}
        for token in tokens:
            if token in query_terms:
                term_frequency[token] = term_frequency.get(token, 0) + 1
        for term in term_frequency:
            document_frequency[term] += 1
        document_stats.append((document_length, term_frequency))

    document_count = len(document_stats)
    if not document_count:
        return []
    average_document_length = total_document_length / document_count

    scores: list[tuple[int, float]] = []
    for index, (document_length, term_frequency) in enumerate(document_stats):
        score = 0.0
        for term in query_terms:
            frequency = term_frequency.get(term, 0)
            if frequency == 0:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                (document_count - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
                + 1.0
            )
            numerator = frequency * (BM25_K1 + 1)
            denominator = frequency + BM25_K1 * (
                1 - BM25_B
                + BM25_B * document_length / average_document_length
            )
            score += inverse_document_frequency * numerator / denominator
        scores.append((index, score))
    return scores


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
    scored = [
        (index, score)
        for index, score in _score_token_documents(
            query, (_tokenize(item) for item in items))
        if score > 0
    ]

    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored
