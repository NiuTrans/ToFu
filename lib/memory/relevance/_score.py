"""Single BM25 formula: streaming core, reusable corpus index, text scorer.

The streaming core retains only each document's length and query-term
frequencies, never the corpus-wide token lists; body search uses it. The
immutable :class:`TokenCorpusIndex` keeps one tokenized corpus's per-document
term counts so repeated queries over an unchanged metadata corpus skip
re-tokenization. Both feed the same final scoring pass, and memory search,
automatic prefetch, and generic snippet ranking share it, so cost
optimizations cannot drift scores.
"""

import math
from collections import Counter
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

    return _score_query_term_frequencies(
        query_terms, document_stats, document_frequency, total_document_length)


def _score_query_term_frequencies(
    query_terms: set[str],
    document_stats: list[tuple[int, dict[str, int]]],
    document_frequency: dict[str, int],
    total_document_length: int,
) -> list[tuple[int, float]]:
    """Final BM25 pass shared by the streaming core and the corpus index."""
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


class TokenCorpusIndex:
    """Immutable per-document term counts of one source-ordered corpus.

    ``score(query)`` returns exactly what :func:`_score_token_documents`
    returns for the same token documents: it derives the same query-term
    statistics from retained counts and runs the same final pass. Equal token
    strings share one object, so retention follows the corpus vocabulary.
    """

    __slots__ = ('_document_lengths', '_term_counts', '_total_document_length')

    def __init__(self, token_documents: Iterable[list[str]]) -> None:
        vocabulary: dict[str, str] = {}
        document_lengths: list[int] = []
        term_counts: list[dict[str, int]] = []
        for tokens in token_documents:
            document_lengths.append(len(tokens))
            term_counts.append({
                vocabulary.setdefault(term, term): frequency
                for term, frequency in Counter(tokens).items()
            })
        self._document_lengths = tuple(document_lengths)
        self._term_counts = tuple(term_counts)
        self._total_document_length = sum(document_lengths)

    def score(self, query: str) -> list[tuple[int, float]]:
        """Return source-ordered BM25 scores for every indexed document."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        query_terms = set(query_tokens)
        document_stats: list[tuple[int, dict[str, int]]] = []
        document_frequency = {term: 0 for term in query_terms}
        for document_length, counts in zip(
                self._document_lengths, self._term_counts):
            term_frequency = {
                term: counts[term] for term in query_terms if term in counts}
            for term in term_frequency:
                document_frequency[term] += 1
            document_stats.append((document_length, term_frequency))

        return _score_query_term_frequencies(
            query_terms,
            document_stats,
            document_frequency,
            self._total_document_length,
        )


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
