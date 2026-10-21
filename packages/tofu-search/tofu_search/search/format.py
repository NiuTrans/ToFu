"""Formatting of search results for a model or MCP client."""

from tofu_search.passages import select_relevant_passages
from tofu_search.search.authority import authority_label

__all__ = ['format_search_for_tool_response']

_SIMILAR_HEADING = ('Similar search candidates for follow-up '
                    '(not verified duplicates or independent corroboration):')


def _similar_identity(candidate):
    text = f"  Candidate URL: {candidate['url']}"
    if candidate.get('title'):
        text += f"\n    Title: {candidate['title']}"
    if candidate.get('relevance'):
        text += f"\n    Listing relevance: {candidate['relevance']} (not a factual verification)."
    return text


def _similar_omission(result):
    count = result.get('similar_snippets_omitted') or 0
    return f'  {count} further similar listings omitted by the candidate budget.' if count else ''


def format_search_for_tool_response(
    results,
    search_diag=None,
    query='',
    *,
    max_chars_per_result=None,
    max_total_content_chars=None,
    fetch_tool_name='fetch_url',
):
    """Format search results, optionally under a query-focused content budget.

    The standalone library remains backwards compatible: with no budget the
    complete fetched content is included. MCP callers pass a total budget so
    each source contributes relevant excerpts without flooding model context.

    Args:
        results: List of search result dicts (irrelevant pages already removed).
        search_diag: Optional coverage/rejection diagnostics from perform_web_search.
        max_chars_per_result: Optional hard cap on each body or snippet payload.
        max_total_content_chars: Optional shared cap on all body/snippet payloads;
            source identity and provenance metadata are never truncated.
    """
    search_diag = search_diag or getattr(results, '_search_diag', None)
    quality_lines = []
    if search_diag:
        failed = sorted((search_diag.get('engine_errors') or {}).keys())
        empty = search_diag.get('engine_empty') or []
        active = search_diag.get('engine_ok') or []
        unavailable = sorted((search_diag.get('engine_unavailable') or {}).keys())
        if failed or empty or unavailable:
            quality_lines.append(
                f"Search coverage: result-bearing engines={','.join(active) or 'none'}; "
                f"failed={','.join(failed) or 'none'}; unavailable={','.join(unavailable) or 'none'}; empty={','.join(empty) or 'none'}. "
                "Coverage is not exhaustive.")
        if search_diag.get('engine_warnings'):
            details = '; '.join(f"{engine}={','.join(codes)}"
                                for engine, codes in sorted(search_diag['engine_warnings'].items()))
            quality_lines.append(
                f"Partial engine coverage: {details}. Coverage is not exhaustive.")
        if search_diag.get('constraint_rejected'):
            quality_lines.append(
                f"Excluded {search_diag['constraint_rejected']} candidates violating site constraints; "
                "this is not evidence of absence.")
        if search_diag.get('weak_match_count'):
            quality_lines.append(
                f"Weak-match candidates: {search_diag['weak_match_count']} "
                "(query relevance uncertain; not established evidence).")
        if search_diag.get('fallback_attempts'):
            quality_lines.append(
                f"Recovery attempted: {', '.join(search_diag['fallback_attempts'])}; "
                "coverage remains non-exhaustive.")
        if search_diag.get('unverified_count'):
            quality_lines.append(f"Unverified candidates: {search_diag['unverified_count']} (not evidence of relevance or absence).")
            for candidate in search_diag.get('unverified_candidates') or []:
                quality_lines.append(f"  Candidate for follow-up: {candidate['url']}")
                if candidate.get('title'):
                    quality_lines.append(f"    Title: {candidate['title']}")
                related = candidate.get('similar_snippets') or []
                if related:
                    quality_lines.append(_SIMILAR_HEADING)
                    quality_lines.extend(_similar_identity(row) for row in related)
                if _similar_omission(candidate):
                    quality_lines.append(_similar_omission(candidate))
        if search_diag.get('reason_detail'):
            quality_lines.append(f"Search diagnostic: {search_diag['reason_detail']}")
    quality = ('\n'.join(quality_lines) + '\n\n') if quality_lines else ''
    if not results:
        if search_diag:
            reason = search_diag.get('reason', 'unknown')
            detail = search_diag.get('reason_detail', '')
            if reason == 'no_usable_evidence':
                return quality + ('Search found no usable evidence for this query. '
                    'This does not establish that the subject does not exist. '
                    f'Try a narrower query or {fetch_tool_name} on an authoritative index or known URL.')
            if reason == 'network_error':
                return quality + ("Search failed: all search engines encountered network errors. "
                        "The server may have limited internet connectivity. "
                        f"You can try using {fetch_tool_name} on a known URL, "
                        "or ask the user to check network.")
            elif reason == 'partial_network_error':
                return quality + ("Search returned 0 results. %s "
                        "Try rephrasing the query or using %s on a specific URL."
                        % (detail, fetch_tool_name))
            else:
                return quality + ("Search returned 0 results from the available engines; this does not prove absence. "
                        "Try rephrasing with different keywords, using fewer/broader terms, "
                        "or searching in a different language.")
        return quality + "No search results found; search coverage is unknown."

    parts = []
    remaining = max_total_content_chars
    content_left = sum(1 for r in results if r.get('full_content') or r.get('snippet'))
    bounded = max_total_content_chars is not None or max_chars_per_result is not None
    for i, r in enumerate(results, 1):
        entry = (f"[{i}] {r['title']}\n"
                 f"    URL: {r['url']}\n"
                 f"    Source: {r['source']}")

        _auth = authority_label(r.get('url') or '', query)
        if _auth:
            entry += f"\n    Authority: {_auth}"

        if r.get('source_rank') is not None:
            entry += f"\n    Source rank: {r['source_rank']} (original listing; not relevance confidence)."
        # Optional provider/host metadata: never infer dates from URLs, stamp
        # the current time, or pretend a missing body proves a failed fetch.
        for key, label in (('published_date', 'Published'), ('publish_date', 'Published'),
                           ('modified_date', 'Modified'), ('retrieved_at', 'Retrieved at'),
                           ('fetch_status', 'Fetch status')):
            if r.get(key):
                entry += f"\n    {label}: {r[key]}"

        if r.get('relevance'):
            entry += f"\n    Relevance: {r['relevance']} (not a factual verification)."
            if r['relevance'] in ('weak_match', 'unverified'):
                entry += ' Query relevance is uncertain; do not treat this as established evidence.'
            evidence = r.get('relevance_evidence') or {}
            if 'matched_terms' in evidence and 'query_terms' in evidence:
                entry += (f" Lexical overlap: {len(evidence['matched_terms'])}/"
                          f"{len(evidence['query_terms'])} query terms.")

        sources = list(dict.fromkeys(r.get('sources') or []))
        if len(sources) > 1:
            entry += f"\n    Provider listings: {', '.join(sources)} (may share underlying engines)."
        upstream = list(dict.fromkeys(r.get('upstream_engines') or []))
        if upstream:
            entry += f"\n    SearXNG upstream listings: {', '.join(upstream)}"
        duplicates = list(dict.fromkeys(r.get('duplicate_urls') or []))
        if duplicates:
            entry += f"\n    Duplicate/mirror URLs: {', '.join(duplicates)} (not additional corroboration)."

        raw = r.get('full_content') or r.get('snippet') or ''
        budget = len(raw)
        if remaining is not None:
            budget = min(budget, max(0, remaining // max(1, content_left)))
        if max_chars_per_result is not None:
            budget = min(budget, max(0, max_chars_per_result))
        shown = select_relevant_passages(raw, query, budget) if bounded else raw
        if remaining is not None:
            remaining = max(0, remaining - len(shown))
        if raw:
            content_left -= 1

        if r.get('full_content'):
            excerpted = len(shown) < len(raw)
            label = 'Query-Focused Excerpts' if excerpted else 'Full Page Content'
            if excerpted:
                entry += (f"\n\n    ──── {label} "
                          f"({len(shown):,} of {len(raw):,} chars) ────\n{shown}")
                entry += '\n    [... remaining page omitted to preserve tool context budget ...]'
            else:
                # Unexcerpted: "N of N chars" is pure noise — one number.
                entry += (f"\n\n    ──── {label} "
                          f"({len(shown):,} chars) ────\n{shown}")
        else:
            entry += f"\n    Summary: {shown}"
            if len(shown) < len(raw):
                entry += '\n    [... remaining summary omitted to preserve tool context budget ...]'
            entry += (f'\n    (Snippet-only; full content not available — call {fetch_tool_name}'
                      ' with the URL above to read this page.)')

        parts.append(entry)

    # Primary evidence receives its existing allocation first. Related snippet
    # payloads share only the remaining budget; exact discovery identities stay
    # visible even when their optional excerpts do not fit.
    related_left = sum(bool(row.get('snippet')) for result in results
                       for row in result.get('similar_snippets') or [])
    for index, result in enumerate(results):
        related = result.get('similar_snippets') or []
        lines = [_SIMILAR_HEADING] if related else []
        for row in related:
            lines.append(_similar_identity(row))
            raw = row.get('snippet') or ''
            budget = len(raw)
            if remaining is not None:
                budget = min(budget, max(0, remaining // max(1, related_left)))
            if max_chars_per_result is not None:
                budget = min(budget, max(0, max_chars_per_result))
            shown = select_relevant_passages(raw, query, budget) if bounded else raw
            if shown:
                lines.append(f'    Search snippet: {shown}')
            if remaining is not None:
                remaining = max(0, remaining - len(shown))
            if raw:
                related_left -= 1
        if _similar_omission(result):
            lines.append(_similar_omission(result))
        if lines:
            parts[index] += '\n\n' + '\n'.join(lines)

    if getattr(results, '_deadline_hit', False):
        header = ("Search results (PARTIAL: the wall-clock deadline expired; "
                  "do not treat source coverage as exhaustive):\n\n")
    else:
        header = "Search results:\n\n"
    return quality + header + "\n\n────\n\n".join(parts)
