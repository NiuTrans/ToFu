"""Cheap query-focused passage selection for LLM/tool context budgets.

The search pipeline deliberately fetches more text than a chat model should be
given: broad extraction improves ranking and recall, while copying six entire
pages into an MCP result wastes context on navigation and unrelated sections.
This module bridges those needs without an embedding or LLM call.
"""

from __future__ import annotations

import re

__all__ = ['select_relevant_passages']

_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
_LATIN_RE = re.compile(r'[a-z0-9]+')
_LINKS_HEADER = '--- Page Links ---'
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has',
    'have', 'how', 'in', 'is', 'it', 'of', 'on', 'or', 'that', 'the', 'this',
    'to', 'was', 'what', 'when', 'where', 'which', 'who', 'why', 'with', 'you',
})


def _tokens(text: str) -> list[str]:
    """Small CJK-aware tokenizer kept independent of the search package."""
    lowered = (text or '').lower()
    out = [t for t in _LATIN_RE.findall(_CJK_RE.sub(' ', lowered))
           if len(t) > 1 and t not in _STOP_WORDS]
    cjk = ''.join(_CJK_RE.findall(lowered))
    out.extend(cjk[i:i + 2] for i in range(max(0, len(cjk) - 1)))
    if len(cjk) <= 2:
        out.extend(cjk)
    return out


def _without_link_inventory(text: str) -> str:
    """Drop the extractor's navigation inventory from model-facing passages."""
    idx = text.find(_LINKS_HEADER)
    return text[:idx].rstrip() if idx >= 0 else text.strip()


def _chunks(text: str, chunk_chars: int) -> list[str]:
    """Split on paragraphs, then window unusually large paragraphs."""
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n+', text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append('\n\n'.join(current))
            current = []
            current_len = 0

    for para in paragraphs:
        if len(para) > chunk_chars:
            flush()
            overlap = min(120, chunk_chars // 8)
            step = max(1, chunk_chars - overlap)
            for start in range(0, len(para), step):
                piece = para[start:start + chunk_chars].strip()
                if piece:
                    chunks.append(piece)
                if start + chunk_chars >= len(para):
                    break
            continue
        added = len(para) + (2 if current else 0)
        if current and current_len + added > chunk_chars:
            flush()
        current.append(para)
        current_len += added
    flush()
    return chunks


def select_relevant_passages(
    text: str,
    query: str,
    max_chars: int,
    *,
    chunk_chars: int = 1_200,
    include_lead: bool = False,
) -> str:
    """Return the most query-relevant excerpts within a hard character budget.

    Selection is lexical and deterministic. The first chunk is retained for an
    LLM relevance gate when ``include_lead`` is true, because it exposes 404 /
    login / challenge-page identity. For model-facing search results it is false
    so every character competes on relevance. If no chunk overlaps the query,
    the function fails open to the document lead rather than returning nothing.
    """
    if not text or max_chars <= 0:
        return ''
    body = _without_link_inventory(text)
    if len(body) <= max_chars:
        return body

    chunk_chars = max(240, min(chunk_chars, max_chars))
    chunks = _chunks(body, chunk_chars)
    if not chunks:
        return body[:max_chars].rstrip()

    query_tokens = _tokens(query)
    query_terms = set(query_tokens)
    normalized_query = ' '.join(query_tokens)
    scored: list[tuple[float, int]] = []
    for idx, chunk in enumerate(chunks):
        toks = _tokens(chunk)
        tok_set = set(toks)
        hits = query_terms & tok_set
        occurrences = sum(toks.count(term) for term in hits)
        coverage = len(hits) / max(1, len(query_terms))
        density = occurrences / max(1, len(toks))
        phrase = 1.0 if normalized_query and normalized_query in ' '.join(toks) else 0.0
        heading_hits = len(query_terms & set(_tokens(chunk.split('\n', 1)[0])))
        score = coverage * 8.0 + density * 12.0 + phrase * 2.0 + heading_hits * 0.25
        scored.append((score, idx))

    chosen: set[int] = set()
    if include_lead:
        chosen.add(0)
    positive = [(score, idx) for score, idx in scored if score > 0]
    if not positive:
        chosen.add(0)

    used = sum(len(chunks[i]) for i in chosen)
    for _score, idx in sorted(positive, key=lambda item: (-item[0], item[1])):
        separator_cost = 18 if chosen else 0
        if idx in chosen:
            continue
        if used + separator_cost + len(chunks[idx]) > max_chars:
            continue
        chosen.add(idx)
        used += separator_cost + len(chunks[idx])

    # A single chunk can be larger than a small remaining budget.
    if not chosen:
        return chunks[0][:max_chars].rstrip()

    ordered = sorted(chosen)
    parts: list[str] = []
    previous = None
    for idx in ordered:
        if previous is not None and idx != previous + 1:
            parts.append('[... omitted ...]')
        parts.append(chunks[idx])
        previous = idx
    return '\n\n'.join(parts)[:max_chars].rstrip()
