"""Host search presentation: compact previews, lossless received evidence.

Serial handlers and authoritative streaming prefetch share this owner. The
package owns query-focused excerpt selection; the host owns the automatic
per-tool budget and captures observations before any presentation work.
fetch_failure_text and join_fetch_results preserve canonical fetch failures
across ordinary, streaming and paper adapters via the common effect owner.
"""
from __future__ import annotations

import json

from lib.tasks_pkg.handlers.search._display import _vertical_header_for_llm
from lib.tools.result_value import (
    CapturedToolResult, ResultPart, TrustedResultText, tool_result_failure_verdict,
)

SEARCH_PREVIEW_CONTENT_CHARS = 20_000


def fetch_failure_text(item, *, task=None, round_entry=None):
    """Preserve failed source facts; only the common policy owner suggests replay."""
    message = f"Failed to fetch {item['url']}."
    facts = item.get('http_failure')
    if (isinstance(task, dict) and isinstance(facts, dict)
            and type(facts.get('status')) is int and 400 <= facts['status'] <= 599):
        from lib.tasks_pkg.executor._effects import tool_http_response_result
        return tool_http_response_result(task, 'fetch_url', round_entry or {},
            status=facts['status'], retry_after=facts.get('retry_after'),
            message=message + ' ' + str(facts.get('detail') or f"HTTP {facts['status']}"))
    if item.get('error_msg'):
        message += f" ({item['error_msg']})"
    # An irrelevant readable page is a semantic verdict, not failed IO.
    status = 'ok' if item.get('reason') == 'irrelevant' else 'error'
    return TrustedResultText(message, canonical=CapturedToolResult(
        (ResultPart('text', message),), execution_status=status))


def join_fetch_results(parts, separator='\n\n'):
    """A completed mixed batch retains every source; all-failed reads stay errors."""
    text = separator.join(parts)
    if parts and all(tool_result_failure_verdict(part) for part in parts):
        return TrustedResultText(text, canonical=CapturedToolResult(
            (ResultPart('text', text),), execution_status='error'))
    return text


def search_observation(query, results, diagnostics=None, engine_breakdown=None,
                       vertical=None):
    """Keep source list sidecars alongside the exact received result records."""
    return {'query': query, 'results': results,
            'unverified_candidates': getattr(results, '_unverified_candidates', []),
            'diagnostics': diagnostics or getattr(results, '_search_diag', None),
            'engine_breakdown': engine_breakdown, 'vertical': vertical,
            'deadline_hit': bool(getattr(results, '_deadline_hit', False))}


def format_search_observations(observations, formatter):
    """Capture first; budget body text across queries, never trim diagnostics.

    Coverage remains unknown: retaining everything received does not establish
    exhaustive engine/web coverage. Metadata, vertical headers and failures are
    outside the body budget and remain visible, including query boundaries.
    """
    original = {'observations': [
        {**item, 'results': list(item['results'])} for item in observations]}
    canonical = CapturedToolResult(
        (ResultPart('json', json.dumps(original, ensure_ascii=False)),),
        source_coverage='unknown')
    parts = []
    count = len(observations)
    budget = SEARCH_PREVIEW_CONTENT_CHARS // max(1, count)
    for item in observations:
        query = item['query']
        if item.get('failure'):
            text = item['failure']
        else:
            text = formatter(item['results'], search_diag=item['diagnostics'],
                             query=query, max_total_content_chars=budget)
            if item['vertical']:
                text = _vertical_header_for_llm(item['vertical']) + text
        if count > 1:
            text = f'=== Search: {query} ===\n{text}'
        parts.append(text)
    return TrustedResultText('\n\n'.join(parts), canonical=canonical,
                             source_omitted=True)


def failed_search_observation(query):
    item = search_observation(query, [])
    item['failure'] = f'Search failed for "{query}": internal error (see logs)'
    return item
