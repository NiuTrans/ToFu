"""lib/search/format.py — Formatting of search results for the model.

Every search result with fetched full content is included in full — no
summary-only tier.  Irrelevant pages are stripped upstream (executor) and
never reach this formatter.
"""

__all__ = ['format_search_for_tool_response']


def format_search_for_tool_response(results, search_diag=None):
    """格式化搜索结果给模型 — 全量输出。

    All results that have ``full_content`` get the complete text included.
    Results without full content (fetch failed) still show title/URL/snippet
    so the model can decide to call ``fetch_url()`` manually.

    Args:
        results: List of search result dicts (irrelevant pages already removed).
        search_diag: Optional diagnostic dict from perform_web_search when
            0 results were found.  Contains 'reason' and 'reason_detail'.
    """
    if not results:
        if search_diag:
            reason = search_diag.get('reason', 'unknown')
            detail = search_diag.get('reason_detail', '')
            if reason == 'network_error':
                return ("Search failed: all search engines encountered network errors. "
                        "The server may have limited internet connectivity. "
                        "You can try using fetch_url on a known URL, or ask the user to check network.")
            elif reason == 'partial_network_error':
                return ("Search returned 0 results. %s "
                        "Try rephrasing the query or using fetch_url on a specific URL." % detail)
            elif reason == 'exception':
                return "Search failed due to an internal error. %s" % detail
            else:
                return ("Search returned 0 results — no matching content was found across all engines. "
                        "Try rephrasing with different keywords, using fewer/broader terms, "
                        "or searching in a different language.")
        return "No search results found."

    parts = []
    for i, r in enumerate(results, 1):
        entry = (f"[{i}] {r['title']}\n"
                 f"    URL: {r['url']}\n"
                 f"    Source: {r['source']}")

        if r.get('full_content'):
            # Always include full content — no cap, no preview/summary tier
            entry += (f"\n\n    ──── Full Page Content "
                      f"({len(r['full_content']):,} chars) ────\n"
                      f"{r['full_content']}")
        else:
            # Fetch failed — show snippet so the model can retry with fetch_url
            entry += (f"\n    Summary: {r['snippet']}"
                      f'\n    (Full content not available — call fetch_url("{r["url"]}") to read this page.)')

        parts.append(entry)

    header = "Search results:\n\n"
    return header + "\n\n════════════════════\n\n".join(parts)
