"""lib/memory/prefetch/_shortlist.py — BM25 coarse stage.

Reuses lib/memory/relevance's tokenizer/doc builder and BM25 formula so the
coarse ranking is consistent with relevance.search_memories.

The tokenized corpus of the most recent call is retained as one
:class:`TokenCorpusIndex`, keyed by the exact source-ordered document texts:
an unchanged metadata corpus is never re-tokenized, and any changed, added,
removed or reordered entry is a different key that rebuilds the index.
"""
from __future__ import annotations

from lib.log import get_logger

from lib.memory.queued_read_coalescer import QueuedReadCoalescer
from lib.memory.relevance import _tokenize
from lib.memory.relevance._score import TokenCorpusIndex
from lib.memory.relevance._tokenize import _memory_document_text

from lib.memory.prefetch._config import PREFETCH_BM25_TOP_N

logger = get_logger(__name__)

# One slot: (documents, index) of the latest corpus. Replaced, never grown.
_corpus_index_slot: tuple[tuple[str, ...], TokenCorpusIndex] | None = None
# Concurrent callers with an identical corpus share one build.
_corpus_index_reads: QueuedReadCoalescer[
    tuple[str, ...], TokenCorpusIndex] = QueuedReadCoalescer()


def _retained_corpus_index(documents: tuple[str, ...]):
    slot = _corpus_index_slot
    if slot is not None and slot[0] == documents:
        return slot[1]
    return None


def _build_corpus_index(documents: tuple[str, ...]) -> TokenCorpusIndex:
    global _corpus_index_slot
    index = _retained_corpus_index(documents)
    if index is None:
        index = TokenCorpusIndex(_tokenize(text) for text in documents)
        _corpus_index_slot = (documents, index)
    return index


def _corpus_index(documents: tuple[str, ...]) -> TokenCorpusIndex:
    index = _retained_corpus_index(documents)
    if index is not None:
        return index
    return _corpus_index_reads.run(documents, _build_corpus_index)


def _bm25_top_n(memories: list[dict], query: str,
                top_n: int = PREFETCH_BM25_TOP_N,
                *, include_body: bool = False) -> list[tuple[int, float]]:
    """Return [(memory_index, score), ...] sorted by BM25 score descending.

    Only memories with score > 0 are returned.  Uses the same tokenizer
    and document construction as relevance.search_memories for consistency.
    """
    if not query or not memories:
        return []

    documents = tuple(
        _memory_document_text(memory, include_body=include_body)
        for memory in memories)
    scored = [
        (index, score)
        for index, score in _corpus_index(documents).score(query)
        if score > 0
    ]

    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]
