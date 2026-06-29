"""lib/memory/relevance.py — BM25-based memory relevance scoring.

Lightweight BM25 scorer that ranks memories by relevance to a query string.
No external dependencies — uses only stdlib math.

Used to reduce the number of memories injected per turn from 100+ to ~30,
cutting context consumption while preserving discoverability.
"""

import math
import re
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['filter_relevant_memories', 'search_memories', 'score_items']

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

DEFAULT_TOP_K = 30
BM25_K1 = 1.5
BM25_B = 0.75

# Common English stop words — excluded from both query and document tokens
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'who', 'new', 'get', 'set', 'one', 'two', 'any',
})

# Regex: split on whitespace + common punctuation (also treats every
# non-Latin char — including CJK — as a delimiter; CJK is handled separately).
_TOKENIZE_RE = re.compile(r'[^a-z0-9_]+')

# Contiguous runs of CJK characters: unified ideographs + extension-A
# (mirrors lib/text_lang._CJK_RE) plus Japanese kana, so Chinese/Japanese
# memories are searchable. Latin tokenization above strips these out, so
# without dedicated handling every CJK-only description was invisible to BM25.
_CJK_RUN_RE = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+')


# ═══════════════════════════════════════════════════════
#  Tokenizer
# ═══════════════════════════════════════════════════════

def _cjk_tokens(text: str) -> list[str]:
    """Tokenize CJK text into overlapping bigrams (segmenter-free).

    Chinese/Japanese has no word boundaries, so a pure word-split yields
    nothing. Overlapping character bigrams are the standard lightweight
    substitute for a segmenter: ``中文海报`` → ``中文 文海 海报``. Applied
    symmetrically to query and documents, this gives robust partial-match
    recall. Isolated single CJK chars fall back to a unigram.
    """
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens, removing stop words.

    Splits on whitespace and punctuation. Also splits snake_case and
    kebab-case identifiers into sub-tokens (e.g. 'flask_migration' →
    ['flask', 'migration']). CJK runs are tokenized into overlapping
    bigrams via :func:`_cjk_tokens` so Chinese/Japanese is searchable.
    """
    lowered = text.lower()
    # Replace hyphens and underscores with spaces for sub-token splitting
    lowered = lowered.replace('-', ' ').replace('_', ' ')
    tokens = _TOKENIZE_RE.split(lowered)
    out = [t for t in tokens if t and t not in _STOP_WORDS and len(t) > 1]
    out.extend(_cjk_tokens(lowered))
    return out


# ═══════════════════════════════════════════════════════
#  BM25 Scorer
# ═══════════════════════════════════════════════════════

def _build_memory_doc(mem: dict[str, Any], include_body: bool = False) -> list[str]:
    """Build a token list from a memory's metadata (name + description + tags).

    Args:
        mem: Memory dict.
        include_body: If True, also tokenize the memory body for deeper matching.
    """
    parts = [
        mem.get('name', ''),
        mem.get('description', ''),
    ]
    tags = mem.get('tags', [])
    if isinstance(tags, list):
        parts.extend(tags)
    if include_body:
        body = mem.get('body', '')
        if body:
            # Limit body to first 2000 chars to keep tokenization fast
            parts.append(body[:2000])
    return _tokenize(' '.join(parts))


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
#  score_items — BM25 over arbitrary text snippets
# ═══════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════
#  search_memories — Tool-callable search with body content
# ═══════════════════════════════════════════════════════

SEARCH_DEFAULT_TOP_K = 30


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

    query_tokens = _tokenize(query)
    if not query_tokens:
        return f'No valid search terms after tokenization. You have {len(memories)} memories available.'

    n = len(memories)
    # Build document token lists WITH body content for deeper matching
    docs = [_build_memory_doc(m, include_body=True) for m in memories]
    doc_lens = [len(d) for d in docs]
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    query_terms = set(query_tokens)
    df: dict[str, int] = {}
    for term in query_terms:
        count = sum(1 for doc in docs if term in doc)
        df[term] = count

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

    # Filter to only memories with score > 0
    relevant = [(sc, idx) for sc, idx in scores if sc > 0]
    if not relevant:
        return f'No memories matched query "{query}".'

    results = relevant[:top_k]
    logger.info('[MemorySearch] query="%.80s" → %d/%d matches (showing top %d)',
                query, len(relevant), n, len(results))

    # Format results — compact index with file paths
    parts = [f'Found {len(relevant)} matching memories (showing top {len(results)}):']
    parts.append('')
    for rank, (sc, idx) in enumerate(results, 1):
        mem = memories[idx]
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
