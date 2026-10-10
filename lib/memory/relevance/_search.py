"""lib/memory/relevance/_search.py — memory-facing BM25 entrypoints.

The two functions that operate over memory dicts:

  * :func:`filter_relevant_memories` — per-turn prefetch gate (metadata only).
  * :func:`search_memories` — tool-callable search that includes body content
    in scoring, lazily pulling the corpus from ``lib.memory.storage``.
"""

from collections.abc import Iterable
from typing import Any

from lib.log import get_logger
from lib.memory.contracts import (
    MEMORY_SEARCH_BODY_MAX_CHARS,
    MEMORY_SEARCH_TOP_K_DEFAULT,
    normalize_memory_search,
)
from lib.memory.relevance._score import _score_token_documents
from lib.memory.relevance._tokenize import (
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

    scores = [
        (score, index)
        for index, score in _score_token_documents(
            query, (_build_memory_doc(memory) for memory in memories))
    ]
    if not scores:
        return memories

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

SEARCH_DEFAULT_TOP_K = MEMORY_SEARCH_TOP_K_DEFAULT


def _score_corpus(
    query: str,
    memories: Iterable[dict[str, Any]],
    *,
    include_body: bool = True,
    retain_body: bool = True,
) -> list[tuple[float, dict[str, Any]]]:
    """Score ``memories`` against ``query`` (BM25), best-first.

    The single scoring core shared by ``search_memories`` (formatted tool
    output) and ``search_memories_scored`` (structured API for programmatic
    callers such as the charter lesson-router). Returns ``[(score, mem)]``
    sorted by score desc, stable on original index; empty list when the
    query has no usable terms.
    """
    if not query or not query.strip():
        return []

    retained_memories: list[dict[str, Any]] = []

    def token_documents():
        for memory in memories:
            tokens = _build_memory_doc(memory, include_body=include_body)
            if include_body and not retain_body and memory.get('body'):
                memory = dict(memory)
                memory['body'] = ''
            retained_memories.append(memory)
            yield tokens

    scores = _score_token_documents(query, token_documents())
    scores.sort(key=lambda row: (-row[1], row[0]))
    return [
        (score, retained_memories[index])
        for index, score in scores
    ]


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
    query, top_k = normalize_memory_search(query, top_k)
    if not query.strip():
        return []

    from lib.memory.storage import iter_eligible_memories
    memories = iter_eligible_memories(
        project_path,
        extra_paths=extra_paths,
        body_char_limit=MEMORY_SEARCH_BODY_MAX_CHARS,
        scope=scope,
    )
    ranked = _score_corpus(
        query, memories, include_body=True, retain_body=False)
    return [(sc, m) for sc, m in ranked if sc > 0][:top_k]


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
    query, top_k = normalize_memory_search(query, top_k)
    if not query.strip():
        return 'Please provide search keywords.'
    if not _tokenize(query):
        return 'No valid search terms after tokenization.'

    from lib.memory.storage import iter_eligible_memories
    memories = iter_eligible_memories(
        project_path,
        extra_paths=extra_paths,
        body_char_limit=MEMORY_SEARCH_BODY_MAX_CHARS,
    )
    ranked = _score_corpus(
        query, memories, include_body=True, retain_body=False)
    if not ranked:
        return 'No memories found. You have no accumulated memories yet.'

    # Filter to only memories with score > 0
    relevant = ranked  # [(score, mem)]
    n = len(ranked)
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
