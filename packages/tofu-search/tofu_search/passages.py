"""Source-preserving, query-focused passages for ranking and tool context.

Scan the complete extracted body; pack complementary lexical evidence into the
requested output budget. No model calls, network, caches or host dependencies.
"""

from __future__ import annotations

import heapq
import re
from collections import Counter

from tofu_search.lexical import query_subject, tokenize

__all__ = ['select_relevant_passages']

_LINKS_HEADER = '--- Page Links ---'
_GAP = '\n\n[... omitted ...]\n\n'


def _without_link_inventory(text: str) -> str:
    return text.split(_LINKS_HEADER, 1)[0].strip()


def _chunk_spans(text: str, chunk_chars: int) -> list[tuple[int, int]]:
    """Paragraph windows with overlap for long paragraphs; offsets stay exact."""
    paragraphs = []
    start = 0
    for gap in re.finditer(r'\n\s*\n+', text):
        paragraphs.append((start, gap.start()))
        start = gap.end()
    paragraphs.append((start, len(text)))
    chunks: list[tuple[int, int]] = []
    current = None
    for start, end in paragraphs:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        if end - start > chunk_chars:
            if current is not None:
                chunks.append(current)
                current = None
            step = chunk_chars - min(120, chunk_chars // 8)
            for offset in range(start, end, step):
                chunks.append((offset, min(end, offset + chunk_chars)))
                if offset + chunk_chars >= end:
                    break
        elif current is not None and end - current[0] <= chunk_chars:
            current = (current[0], end)
        else:
            if current is not None:
                chunks.append(current)
            current = (start, end)
    if current is not None:
        chunks.append(current)
    return chunks


def _merge_spans(spans: list[tuple[int, int]], text: str) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and (start <= merged[-1][1] or (
                start - merged[-1][1] <= 4 and not text[merged[-1][1]:start].strip())):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _render(spans: list[tuple[int, int]], text: str) -> str:
    return _GAP.join(text[start:end] for start, end in _merge_spans(spans, text))


def _fallback_excerpt(text: str, max_chars: int, chunk_chars: int, edges: bool) -> str:
    if not edges or max_chars < len(_GAP) + 2:
        return text[:max_chars].rstrip()
    edge_chars = min(max(80, chunk_chars), (max_chars - len(_GAP)) // 2)
    return _render([(0, edge_chars), (len(text) - edge_chars, len(text))], text).rstrip()


def select_relevant_passages(
    text: str,
    query: str,
    max_chars: int,
    *,
    chunk_chars: int = 1_200,
    include_lead: bool = False,
    fallback_edges: bool = False,
) -> str:
    """Pack diverse query evidence into a hard character budget, in source order.

    Chunks compete on coverage, density and phrase matches. Newly covered terms
    win space over repeated aspects; exact duplicate chunks consume no extra
    budget. Chosen spans retain original text and explicit gap markers.
    ``include_lead`` reserves at most one third for page identity (login/error
    detection), leaving room for relevant evidence. With no lexical evidence,
    return the lead: a lexical miss is not proof of irrelevance. Callers that
    need source identity and trailing context can set ``fallback_edges`` to
    retain up to one chunk from each edge instead, within the same budget.
    """
    if not text or max_chars <= 0:
        return ''
    body = _without_link_inventory(text)
    if len(body) <= max_chars:
        return body
    query_tokens = tokenize(query_subject(query))
    query_terms = set(query_tokens)
    if not query_terms:
        return _fallback_excerpt(body, max_chars, chunk_chars, fallback_edges)

    # Leave space for a second aspect even when the output budget is smaller
    # than the normal scoring window.
    chunk_chars = max(80, min(chunk_chars, max_chars // 2))
    spans = _chunk_spans(body, chunk_chars)
    phrase = ' '.join(query_tokens)
    candidates = []
    seen = set()
    for idx, (start, end) in enumerate(spans):
        chunk = body[start:end]
        # Only exact text repeats are removed. Different numbers, qualifiers
        # and conflicting observations must remain eligible.
        identity = ' '.join(chunk.split())
        if identity in seen:
            continue
        seen.add(identity)
        tokens = tokenize(chunk)
        counts = Counter(tokens)
        hits = query_terms & counts.keys()
        if not hits:
            continue
        coverage = len(hits) / len(query_terms)
        density = sum(min(3, counts[term]) for term in hits) / max(1, len(tokens))
        phrase_match = float(phrase in ' '.join(tokens))
        heading_hits = len(query_terms & set(tokenize(chunk.split('\n', 1)[0])))
        score = coverage * 8 + density * 12 + phrase_match * 2 + heading_hits * 0.25
        candidates.append((score, idx, hits, set(counts)))
    if not candidates:
        return _fallback_excerpt(body, max_chars, chunk_chars, fallback_edges)

    chosen: list[tuple[int, int]] = []
    covered: set[str] = set()
    chosen_terms: list[set[str]] = []
    if include_lead and max_chars >= 3:
        lead_end = min(spans[0][1], max_chars // 3)
        chosen.append((0, lead_end))
        covered.update(query_terms & set(tokenize(body[:lead_end])))

    def priority(score, hits, terms):
        similarity = max((len(terms & previous) / max(1, len(terms | previous))
                          for previous in chosen_terms), default=0.0)
        return score * (1 - 0.65 * similarity) + 6 * len(hits - covered) / len(query_terms)

    pending = [(-priority(score, hits, terms), idx, score, hits, terms)
               for score, idx, hits, terms in candidates]
    heapq.heapify(pending)
    while pending:
        _priority, idx, score, hits, terms = heapq.heappop(pending)
        start, end = spans[idx]
        trial = _render(chosen + [(start, end)], body)
        accepted = False
        if len(trial) <= max_chars:
            chosen.append((start, end))
            covered.update(hits)
            chosen_terms.append(terms)
            accepted = True
        else:
            # Give new evidence available space before a weaker shorter passage
            # can take it. Contiguous slicing preserves source evidence.
            remaining = max_chars - len(_render(chosen, body)) - (len(_GAP) if chosen else 0)
            if remaining > 0 and (not covered or hits - covered):
                end = min(end, start + remaining)
                chosen.append((start, end))
                selected_terms = set(tokenize(body[start:end]))
                covered.update(query_terms & selected_terms)
                chosen_terms.append(selected_terms)
                accepted = True
        if max_chars - len(_render(chosen, body)) <= len(_GAP):
            break
        if accepted:
            # Recompute only when selected evidence changes. Popping unfit
            # candidates remains O(log n), avoiding a quadratic full-page scan.
            pending = [(-priority(s, h, t), i, s, h, t) for _, i, s, h, t in pending]
            heapq.heapify(pending)

    return _render(chosen, body)[:max_chars].rstrip()
