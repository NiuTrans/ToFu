"""lib/memory/relevance/_search.py — memory-facing BM25 entrypoints.

The two functions that operate over memory dicts:

  * :func:`filter_relevant_memories` — per-turn prefetch gate (metadata only).
  * :func:`search_memories` — tool-callable search that includes body content
    in scoring, lazily pulling the corpus from ``lib.memory.storage``.
"""

import math
from typing import Any

from lib.log import get_logger
from lib.memory.relevance._tokenize import (
    BM25_B,
    BM25_K1,
    DEFAULT_TOP_K,
    _build_memory_doc,
    _tokenize,
)

logger = get_logger(__name__)


def filter_relevant_memories(
    memories: list[dict[str, Any]],
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Filter memories by BM25 relevance to query, returning top-K.

    Args:
        memories: List of memory dicts (with 'name', 'description', 'tags').
        query: User message text to match against.
        top_k: Maximum number of memories to return.

    Returns:
        List of memory dicts, sorted by relevance (most relevant first).
        If len(memories) <= top_k, returns all memories unchanged (no filtering).
        If query is empty/None, returns all memories unchanged.
    """
    if not query or not memories:
        return memories

    n = len(memories)
    if n <= top_k:
        return memories

    query_tokens = _tokenize(query)
    if not query_tokens:
        return memories

    # Build document token lists for all memories
    docs = [_build_memory_doc(s) for s in memories]
    doc_lens = [len(d) for d in docs]
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    # Compute document frequency (DF) for each query term
    query_terms = set(query_tokens)
    df: dict[str, int] = {}
    for term in query_terms:
        count = sum(1 for doc in docs if term in doc)
        df[term] = count

    # Compute BM25 score for each memory
    scores = []
    for i, (mem, doc, dl) in enumerate(zip(memories, docs, doc_lens)):
        score = 0.0
        # Term frequency map for this document
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in query_terms:
                tf_map[t] = tf_map.get(t, 0) + 1

        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            d = df.get(term, 0)
            # IDF: log((N - df + 0.5) / (df + 0.5) + 1)
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            # BM25 term score
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl)
            score += idf * numerator / denominator

        scores.append((score, i))

    # Sort by score descending, then by original index for stability
    scores.sort(key=lambda x: (-x[0], x[1]))

    # Return top_k memories
    result = [memories[idx] for _, idx in scores[:top_k]]
    n_filtered = n - len(result)
    if n_filtered > 0:
        logger.debug('[MemoryBM25] Filtered %d→%d memories for query (%.60s)',
                     n, len(result), query)
    return result


# ═══════════════════════════════════════════════════════
#  search_memories — Tool-callable search with body content
# ═══════════════════════════════════════════════════════

SEARCH_DEFAULT_TOP_K = 30


def _score_corpus(
    query: str,
    memories: list[dict[str, Any]],
    *,
    include_body: bool = True,
) -> list[tuple[float, dict[str, Any]]]:
    """Score ``memories`` against ``query`` (BM25), best-first.

    The single scoring core shared by ``search_memories`` (formatted tool
    output) and ``search_memories_scored`` (structured API for programmatic
    callers such as the charter lesson-router). Returns ``[(score, mem)]``
    sorted by score desc, stable on original index; empty list when the
    query has no usable terms.
    """
    if not memories or not query or not query.strip():
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    n = len(memories)
    docs = [_build_memory_doc(m, include_body=include_body) for m in memories]
    doc_lens = [len(d) for d in docs]
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    query_terms = set(query_tokens)
    df: dict[str, int] = {}
    for term in query_terms:
        df[term] = sum(1 for doc in docs if term in doc)

    scores: list[tuple[float, int]] = []
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
        scores.append((score, i))

    scores.sort(key=lambda x: (-x[0], x[1]))
    return [(sc, memories[idx]) for sc, idx in scores]


def search_memories_scored(
    query: str,
    project_path: str | None = None,
    top_k: int = SEARCH_DEFAULT_TOP_K,
    extra_paths: list[str] | None = None,
    scope: str | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    """Structured counterpart of :func:`search_memories`.

    Returns ``[(score, memory_dict)]`` (score > 0, best-first, capped at
    ``top_k``) so programmatic callers can apply their OWN threshold — the
    charter lesson-router uses it to decide "update the existing memory on
    this topic" vs "create a new one" (dedup), a decision the formatted
    string output cannot express.

    ``scope='project'`` restricts the corpus to project-scope memories BEFORE
    scoring. That matters for threshold callers: scoring against the global
    union lets the server-global corpus inflate a term's df and collapse its
    IDF, making the same dedup question answer differently on different
    machines — project-local scoring is deterministic.
    """
    from lib.memory.storage import get_eligible_memories

    memories = get_eligible_memories(project_path, extra_paths=extra_paths)
    if scope:
        memories = [m for m in memories if m.get('scope') == scope]
    ranked = _score_corpus(query, memories, include_body=True)
    return [(sc, m) for sc, m in ranked if sc > 0][:max(1, min(top_k, 50))]


def search_memories(
    query: str,
    project_path: str | None = None,
    top_k: int = SEARCH_DEFAULT_TOP_K,
    extra_paths: list[str] | None = None,
) -> str:
    """Search memories by BM25 relevance, including body content in scoring.

    Returns a compact index of matching memories (name, description, tags,
    file path). The model can then use read_files to read specific memories
    it finds interesting.

    Args:
        query: Search keywords from the model.
        project_path: Project path for scoped memories.
        top_k: Maximum number of results.
        extra_paths: Additional workspace roots (multi-root session) whose
            memories are unioned in alongside the primary root's.

    Returns:
        Formatted index of matching memories with file paths.
    """
    from lib.memory.storage import get_eligible_memories

    memories = get_eligible_memories(project_path, extra_paths=extra_paths)
    if not memories:
        return 'No memories found. You have no accumulated memories yet.'

    if not query or not query.strip():
        return f'Please provide search keywords. You have {len(memories)} memories available.'

    top_k = max(1, min(top_k, 50))  # Clamp to [1, 50]

    ranked = _score_corpus(query, memories, include_body=True)
    if not ranked:
        return f'No valid search terms after tokenization. You have {len(memories)} memories available.'

    # Filter to only memories with score > 0
    relevant = ranked  # [(score, mem)]
    n = len(memories)
    if not relevant or relevant[0][0] <= 0:
        return f'No memories matched query "{query}".'
    relevant = [(sc, m) for sc, m in relevant if sc > 0]
    if not relevant:
        return f'No memories matched query "{query}".'

    results = relevant[:top_k]
    logger.info('[MemorySearch] query="%.80s" → %d/%d matches (showing top %d)',
                query, len(relevant), n, len(results))

    # Format results — compact index with file paths
    parts = [f'Found {len(relevant)} matching memories (showing top {len(results)}):']
    parts.append('')
    for rank, (sc, mem) in enumerate(results, 1):
        tags = mem.get('tags', [])
        tag_str = f'  tags: {", ".join(tags)}' if tags else ''
        parts.append(
            f'{rank}. **{mem["name"]}** (scope: {mem["scope"]})\n'
            f'   {mem.get("description", "")}\n'
            f'   path: {mem.get("filepath", "")}'
            f'{tag_str}'
        )

    remaining = len(relevant) - len(results)
    if remaining > 0:
        parts.append(f'\n{remaining} more matches not shown. Refine your query for more specific results.')
    parts.append('\nUse read_files to read the full content of any memory you need.')

    return '\n'.join(parts)
