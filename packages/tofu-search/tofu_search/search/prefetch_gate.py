"""tofu_search.search.prefetch_gate — Cheap, fail-open relevance gate run
BEFORE a search result is fetched.

Motivation
----------
The orchestrator fires engines and submits every returned URL to the fetch
pool the instant an engine responds (the "1+4 merged" overlap). Relevance is
only judged afterwards, by the *optional* LLM content filter (step 5) — which
runs **after** the expensive page fetch. So a junk SERP result (e.g. a
consumer-health page returned for an academic query) is fetched in full,
wastes the fetch budget, floods a host's browser/transport, and only then gets
dropped. On a weak/niche query a single bad engine can dispatch a dozen
irrelevant fetches.

This module adds a *pre-fetch* gate: a pure-Python, no-network, no-LLM lexical
check on the result's ``title + snippet`` against the query. It is deliberately
**fail-open** — its only job is to skip results that are *obviously* unrelated
(zero query-term overlap), never to make fine-grained relevance calls (that is
still the LLM filter's + BM25 rerank's job downstream).

Safety rails (so recall is not harmed):
  * Only engages for **substantive queries** (>= ``min_query_terms`` meaningful
    tokens). Short queries ("numpy", "时间") fetch everything.
  * Always admits the **first ``min_fetch`` candidates** (a recall floor —
    engines already rank by relevance, so the head is usually good).
  * Only SKIPS a candidate whose title+snippet shares **zero** query terms.
    Any single shared term ⇒ fetched.
  * A skipped candidate is **not dropped** — the caller keeps it as a
    snippet-only result so rerank/format still see it. We only decline to pay
    for its page fetch.

Reuses the BM25 tokenizer from ``rerank.py`` so the token model (CJK bigrams +
Latin stop-word removal) is identical to what reranking uses — no second,
divergent notion of "query terms".
"""

from tofu_search.log import get_logger
from tofu_search.search.rerank import _tokenize

logger = get_logger(__name__)

__all__ = ['should_fetch_result', 'partition_fetchable']


def query_terms(query: str) -> set[str]:
    """Return the set of meaningful (tokenized, stop-word-stripped) query terms."""
    return set(_tokenize(query or ''))


def result_terms(result: dict) -> set[str]:
    """Tokenize a result's title + snippet into the same term space as the query."""
    title = (result.get('title') or '')
    snippet = (result.get('snippet') or '')
    return set(_tokenize(f'{title} {snippet}'))


def should_fetch_result(result: dict, q_terms: set[str]) -> bool:
    """Decide whether a single result is worth fetching (lexical overlap > 0).

    Fail-open: returns True whenever the query has too few terms to judge, or
    the result shares at least one query term. Returns False only for a
    substantive query against a result with ZERO shared terms.
    """
    if not q_terms:
        return True
    return bool(q_terms & result_terms(result))


def partition_fetchable(query: str, results: list[dict], *,
                        min_query_terms: int = 2,
                        min_fetch: int = 3) -> tuple[list[dict], list[dict]]:
    """Split a batch of fresh engine results into (to_fetch, skipped).

    Args:
        query: The search query (its terms define relevance).
        results: New, URL-deduped engine results in engine-rank order.
        min_query_terms: Below this many meaningful query terms the gate is a
            no-op (everything is fetched) — too little signal to judge.
        min_fetch: Always admit at least this many leading candidates (recall
            floor), regardless of overlap.

    Returns:
        (to_fetch, skipped) — ``skipped`` results have zero query-term overlap
        and are beyond the recall floor. They are NOT dropped by this function;
        the caller keeps them as snippet-only candidates.
    """
    if not results:
        return [], []

    q_terms = query_terms(query)
    # Too little query signal → fetch everything (fail-open).
    if len(q_terms) < min_query_terms:
        return list(results), []

    to_fetch: list[dict] = []
    skipped: list[dict] = []
    for idx, r in enumerate(results):
        if idx < min_fetch or should_fetch_result(r, q_terms):
            to_fetch.append(r)
        else:
            skipped.append(r)

    if skipped:
        logger.info('[PrefetchGate] skipped %d/%d off-topic result(s) (0 query-term '
                    'overlap, beyond recall floor=%d) query=%r — e.g. %s',
                    len(skipped), len(results), min_fetch, query[:60],
                    ', '.join(r.get('url', '')[:60] for r in skipped[:3]))
    return to_fetch, skipped
