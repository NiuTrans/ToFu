"""Formatting of search results for a model or MCP client."""

from tofu_search.passages import select_relevant_passages
from tofu_search.search.authority import authority_label

__all__ = ['format_search_for_tool_response']


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
        search_diag: Optional diagnostic dict from perform_web_search when
            0 results were found.  Contains 'reason' and 'reason_detail'.
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
        if search_diag.get('constraint_rejected'):
            quality_lines.append(f"Excluded {search_diag['constraint_rejected']} candidates violating site constraints.")
        if search_diag.get('unverified_count'):
            quality_lines.append(f"Unverified candidates: {search_diag['unverified_count']} (not evidence of relevance or absence).")
            for candidate in search_diag.get('unverified_candidates') or []:
                quality_lines.append(f"  Candidate for follow-up: {candidate['url']}")
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
    content_left = sum(1 for r in results if r.get('full_content'))
    for i, r in enumerate(results, 1):
        entry = (f"[{i}] {r['title']}\n"
                 f"    URL: {r['url']}\n"
                 f"    Source: {r['source']}")

        _auth = authority_label(r.get('url') or '', query)
        if _auth:
            entry += f"\n    Authority: {_auth}"

        if r.get('relevance'):
            entry += f"\n    Relevance: {r['relevance']} (not a factual verification)."

        sources = r.get('sources') or []
        if len(sources) > 1:
            entry += (f"\n    Engine consensus: {', '.join(sources)} "
                      f"({len(sources)} independent listings)")
        upstream = r.get('upstream_engines') or []
        if len(upstream) > 1:
            entry += (f"\n    SearXNG upstream consensus: {', '.join(upstream)} "
                      f"({len(upstream)} engines)")

        if r.get('full_content'):
            raw = r['full_content']
            budget = len(raw)
            if remaining is not None:
                budget = min(budget, max(0, remaining // max(1, content_left)))
            if max_chars_per_result is not None:
                budget = min(budget, max_chars_per_result)
            shown = select_relevant_passages(raw, query, budget)
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
            if remaining is not None:
                remaining = max(0, remaining - len(shown))
            content_left -= 1
        else:
            # Fetch failed — the entry's own URL line is three rows up, so
            # restating it here would be a second copy per unfetched result.
            entry += (f"\n    Summary: {r['snippet']}"
                      f'\n    (Full content not available — call {fetch_tool_name}'
                      ' with the URL above to read this page.)')

        parts.append(entry)

    if getattr(results, '_deadline_hit', False):
        header = ("Search results (PARTIAL: the wall-clock deadline expired; "
                  "do not treat source coverage as exhaustive):\n\n")
    else:
        header = "Search results:\n\n"
    return quality + header + "\n\n────\n\n".join(parts)
