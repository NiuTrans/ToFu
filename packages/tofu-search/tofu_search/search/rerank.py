"""lib/search/rerank.py — BM25-based reranking for search results.

Operates on LLM-cleaned full page content (``full_content``) when available,
falling back to ``title — snippet`` for results that were not fetched.

Uses pure-Python BM25 scoring (same algorithm as memory relevance filtering)
instead of calling an external embedding API, eliminating the ~11s embedding
latency from the search pipeline.
"""

import math
import re
import time
from urllib.parse import urlparse

from tofu_search.log import get_logger
from tofu_search.search.authority import (
    AUTHORITY_BOOST,
    brand_tokens,
    classify_authority,
    host_brand_labels,
)

logger = get_logger(__name__)

__all__ = ['rerank_by_bm25']

# ═══════════════════════════════════════════════════════
#  BM25 constants (same as lib/memory/relevance.py)
# ═══════════════════════════════════════════════════════

BM25_K1 = 1.5
BM25_B = 0.75

# Truncate documents to this many chars before tokenizing.
# Keeps reranking fast even for very long pages.
_MAX_RERANK_CHARS = 8_000

# Search-engine rank/consensus and successful extraction are useful weak
# signals, but lexical relevance and authority must remain dominant.
RRF_WEIGHT = 8.0
CONTENT_AVAILABLE_BOOST = 0.25

# Common English stop words
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'who', 'new', 'get', 'set', 'one', 'two', 'any',
    'www', 'http', 'https', 'com', 'org', 'html',
})

# Regex: split on non-alphanumeric (handles CJK via bigrams separately)
_TOKENIZE_RE = re.compile(r'[^a-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+')

# CJK unicode ranges for bigram extraction
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')


# ═══════════════════════════════════════════════════════
#  Tokenizer (handles CJK bigrams + Latin words)
# ═══════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens for BM25 scoring.

    Latin text is split on whitespace/punctuation with stop words removed.
    CJK text is split into character bigrams for better matching.
    """
    lowered = text.lower()
    lowered = lowered.replace('-', ' ').replace('_', ' ')
    raw_tokens = _TOKENIZE_RE.split(lowered)

    tokens = []
    for t in raw_tokens:
        if not t:
            continue
        # Check if token contains CJK characters
        cjk_chars = _CJK_RE.findall(t)
        if cjk_chars:
            # Extract CJK bigrams
            cjk_str = ''.join(cjk_chars)
            for i in range(len(cjk_str) - 1):
                tokens.append(cjk_str[i:i+2])
            # Also keep single CJK chars for short queries
            if len(cjk_str) <= 2:
                for c in cjk_str:
                    tokens.append(c)
            # Extract any Latin parts too
            latin_part = _CJK_RE.sub(' ', t)
            for lt in latin_part.split():
                if lt and lt not in _STOP_WORDS and len(lt) > 1:
                    tokens.append(lt)
        elif t not in _STOP_WORDS and len(t) > 1:
            tokens.append(t)

    return tokens


# ═══════════════════════════════════════════════════════
#  BM25 Reranker
# ═══════════════════════════════════════════════════════

def _build_doc_text(r: dict) -> str:
    """Build rankable text from a search result dict."""
    full = (r.get('full_content') or '').strip()
    title = (r.get('title') or '').strip()
    snippet = (r.get('snippet') or '').strip()

    if full:
        # SERP titles/snippets are compact relevance judgments from the search
        # engines. Repeat them so they are not drowned by an 8k article body.
        text = f'{title}\n{title}\n{title}\n{snippet}\n{snippet}\n{full}'
        return text[:_MAX_RERANK_CHARS]
    else:
        return f'{title} {snippet}' if title else snippet


def _diversify_by_entity(
    query: str, scored: list[tuple[float, int, dict]], top_k: int
) -> list[dict] | None:
    """Coverage-first selection for multi-entity comparison queries.

    ``scored`` is ``[(score, orig_idx, result)]`` already sorted by descending
    score (BM25 + authority boost). When the query names ≥2 distinct entities
    that are actually present in the candidate hosts (e.g. "compare Cloudflare
    Fastly CloudFront …"), plain top-K can fill every slot from the single
    highest-scoring entity, leaving the others unrepresented. This guarantees
    each named entity gets its best candidate into the top-K before remaining
    slots are filled by global score.

    An "entity" is a query brand token (``brand_tokens``) that matches the
    non-generic host label of at least one candidate (``host_brand_labels``).
    Within one entity the candidates are already in global-score order, so the
    authority boost still decides that entity's winner (official/primary over
    aggregator). Returns ``None`` — signalling the caller to use the plain
    global top-K — when the query names fewer than 2 present entities.
    """
    entity_toks = brand_tokens(query)
    if not entity_toks:
        return None

    present: dict[str, list[int]] = {}
    covers_at: dict[int, set[str]] = {}
    for rank, (_score, _idx, result) in enumerate(scored):
        covered = host_brand_labels(result.get('url') or '') & entity_toks
        covers_at[rank] = covered
        for e in covered:
            present.setdefault(e, []).append(rank)

    if len(present) < 2:
        return None

    # Prefer entities with the strongest single candidate first, so that when
    # top_k < #entities we keep the best-supported entities.
    entity_order = sorted(present, key=lambda e: -scored[present[e][0]][0])

    selected_ranks: list[int] = []
    chosen: set[int] = set()
    covered_entities: set[str] = set()
    for e in entity_order:
        if len(selected_ranks) >= top_k:
            break
        if e in covered_entities:
            continue
        for rank in present[e]:  # already in global-score order
            if rank not in chosen:
                selected_ranks.append(rank)
                chosen.add(rank)
                covered_entities |= covers_at[rank]
                break

    for rank in range(len(scored)):
        if len(selected_ranks) >= top_k:
            break
        if rank not in chosen:
            selected_ranks.append(rank)
            chosen.add(rank)

    selected_ranks.sort()  # present in global-score order (best first)
    return [scored[r][2] for r in selected_ranks]


def _domain_key(url: str) -> str:
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception:
        return ''
    labels = [label for label in host.split('.') if label]
    if len(labels) >= 3 and labels[-2] in {'co', 'com', 'org', 'net', 'gov', 'edu', 'ac'} \
            and len(labels[-1]) == 2:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:]) if len(labels) >= 2 else host


def _diversify_by_domain(
    query: str, scored: list[tuple[float, int, dict]], top_k: int,
    max_per_domain: int = 2,
) -> list[dict]:
    """Avoid a top-K monopolized by one host, without reducing result count."""
    if re.search(r'(?i)(?:^|\s)site:', query or ''):
        return [item[2] for item in scored[:top_k]]
    chosen: list[dict] = []
    chosen_ids: set[int] = set()
    counts: dict[str, int] = {}
    for _score, idx, result in scored:
        domain = _domain_key(result.get('url') or '')
        if domain and counts.get(domain, 0) >= max_per_domain:
            continue
        chosen.append(result)
        chosen_ids.add(idx)
        if domain:
            counts[domain] = counts.get(domain, 0) + 1
        if len(chosen) >= top_k:
            return chosen
    # If the candidate pool contains only one or two hosts, fill the remaining
    # slots in score order rather than returning fewer results.
    for _score, idx, result in scored:
        if idx not in chosen_ids:
            chosen.append(result)
            chosen_ids.add(idx)
        if len(chosen) >= top_k:
            break
    return chosen


def rerank_by_bm25(query: str, results: list[dict], top_k: int) -> list[dict]:
    """Rerank search results by BM25 score of query vs document content.

    Uses the ``full_content`` field when available, falling back to
    ``title + snippet`` for results that lack fetched content.

    Pure Python, no external API calls — typically completes in <10ms.

    Args:
        query: The user's search query.
        results: Search result dicts — should already have ``full_content``
                 populated and LLM-cleaned.
        top_k: Number of best results to keep.

    Returns:
        Top-K results sorted by descending BM25 relevance.
    """
    if not results:
        return []
    if len(results) <= top_k:
        return results

    t0 = time.time()

    query_tokens = _tokenize(query)
    if not query_tokens:
        logger.debug('[Rerank] No query tokens after tokenization, returning original order')
        return results[:top_k]

    query_terms = set(query_tokens)

    # Tokenize all documents
    doc_texts = [_build_doc_text(r) for r in results]
    docs = [_tokenize(t) for t in doc_texts]
    doc_lens = [len(d) for d in docs]
    n = len(docs)
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    # Compute document frequency for each query term
    df: dict[str, int] = {}
    for term in query_terms:
        df[term] = sum(1 for doc in docs if term in doc)

    # Score each result with BM25
    scored = []
    for i, (result, doc, dl) in enumerate(zip(results, docs, doc_lens, strict=True)):
        score = 0.0
        # Build term frequency map for query terms only
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

        # Primary-source authority: a modest additive nudge so the vendor's own
        # domain / official docs outrank SEO aggregators when relevance is
        # comparable (never large enough to beat a much stronger lexical match).
        tier = classify_authority(result.get('url') or '', query)
        score += AUTHORITY_BOOST.get(tier, 0.0)

        # Reciprocal-rank fusion is accumulated by the orchestrator when the
        # same canonical URL appears in multiple engine lists. A SearXNG result
        # may also carry its own upstream engine_count.
        score += float(result.get('rrf_score') or 0.0) * RRF_WEIGHT
        engine_count = max(1, int(result.get('engine_count') or 1))
        score += min(0.9, math.log2(engine_count) * 0.3)
        if result.get('full_content'):
            score += CONTENT_AVAILABLE_BOOST

        scored.append((score, i, result))

    # Sort by score descending, break ties by original position
    scored.sort(key=lambda x: (-x[0], x[1]))

    diversified = _diversify_by_entity(query, scored, top_k)
    if diversified is not None:
        selected = diversified
    else:
        selected = _diversify_by_domain(query, scored, top_k)

    elapsed = time.time() - t0
    scores_str = ', '.join(f'#{item[1]}:{item[0]:.3f}' for item in scored[:top_k])
    logger.info('[Rerank] BM25 %d->%d results in %.1fms. div=%s Top-%d (idx:score): %s  query=%r',
                len(results), top_k, elapsed * 1000, diversified is not None,
                top_k, scores_str, query[:60])

    return selected
