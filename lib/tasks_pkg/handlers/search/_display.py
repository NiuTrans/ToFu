# HOT_PATH
"""Display formatting helpers: frontend result rows + vertical SSE/LLM rendering."""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _format_fetch_display(item, _short_url) -> dict:
    """Build the display dict (frontend result row) for one fetch item."""
    target_url = item['url']
    if item['error_msg']:
        return {
            'title': f'Rejected: {_short_url(target_url)}',
            'snippet': item['error_msg'], 'url': target_url,
            'source': 'N/A', 'fetched': False, 'fetchedChars': 0,
        }
    filtered_chars = item['filtered_chars']
    raw_chars = item['raw_chars']
    page_content = item['page_content']
    is_pdf = item['is_pdf']
    reason = item.get('reason')
    if reason == 'soft_blocked':
        return {
            'title': f'Blocked: {_short_url(target_url)}',
            'snippet': 'Host answered HTTP 200 with a region-unavailable page',
            'url': target_url,
            'source': 'Host Blocked',
            'fetched': False, 'fetchedChars': 0,
        }
    if reason == 'irrelevant':
        return {
            'title': f'Irrelevant: {_short_url(target_url)}',
            'snippet': f'Read {raw_chars:,} chars — filtered out as off-topic',
            'url': target_url,
            'source': 'Filtered',
            'fetched': False, 'fetchedChars': 0,
        }
    if item.get('is_asset'):
        return {
            'title': f'File: {_short_url(target_url)}',
            'snippet': (f'Saved {raw_chars:,} bytes → staging (read via read_files)'
                        if page_content else 'Failed'),
            'url': target_url,
            'source': 'File Asset',
            'fetched': bool(page_content),
            'fetchedChars': filtered_chars,
        }
    return {
        'title': f'{"PDF" if is_pdf else "Page"}: {_short_url(target_url)}',
        'snippet': (
            f'{filtered_chars:,} chars'
            + (f' (filtered from {raw_chars:,})' if filtered_chars < raw_chars else '')
        ) if page_content else 'Failed',
        'url': target_url,
        'source': 'PDF' if is_pdf else 'Direct Fetch',
        'fetched': bool(page_content),
        'fetchedChars': filtered_chars,
    }


def _format_search_display_for_results(results) -> list[dict]:
    """Strip ``full_content`` from raw search results for frontend display."""
    display_results = []
    for r in results:
        dr = {k: v for k, v in r.items() if k != 'full_content'}
        if r.get('full_content'):
            dr['fetched'] = True
            dr['fetchedChars'] = len(r['full_content'])
        display_results.append(dr)
    return display_results


def _vertical_to_sse_payload(vertical_result) -> dict | None:
    """Normalize a vertical record into the structured field the frontend renders.

    Domain-level records already carry ``items`` + ``sources``; legacy type-
    level records get wrapped into the same shape so the frontend has a
    single rendering branch.
    """
    if not vertical_result or not isinstance(vertical_result, dict):
        return None
    domain = vertical_result.get('domain') or 'vertical'
    if 'items' in vertical_result and 'sources' in vertical_result:
        return {
            'domain': domain,
            'sources': vertical_result.get('sources', []),
            'items': vertical_result.get('items', []),
        }
    sub_type = vertical_result.get('type', '')
    source = vertical_result.get('source', sub_type)
    # A type-level handler MAY return rich rows of its own (travel flight/hotel
    # records carry bookable items) — pass them through. Collapsing them into
    # one synthesized headline would silently drop everything but the LLM text.
    # Mirrors registry._structured_items_from_record.
    items = []
    raw_items = vertical_result.get('items')
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it.setdefault('type', sub_type)
            it.setdefault('source', source)
            items.append(it)
    if not items:
        head = vertical_result.get('content', '').splitlines()[:1]
        title = head[0].lstrip('# ').strip() if head else source or sub_type or 'Result'
        items.append({
            'title': title,
            'snippet': '',
            'url': '',
            'type': sub_type,
            'source': source,
        })
    return {
        'domain': domain,
        'sources': [{'type': sub_type, 'source': source,
                     'identifier': vertical_result.get('identifier', '')}],
        'items': items,
    }


def _vertical_header_for_llm(vertical_result) -> str:
    """Render the markdown block prepended to the tool response for the LLM."""
    if not vertical_result:
        return ''
    if 'sources' in vertical_result and isinstance(vertical_result.get('sources'), list):
        names = [s.get('source') or s.get('type') or '?' for s in vertical_result['sources']]
        label = ' + '.join(names)
        return (f'═══ Vertical Search ({vertical_result.get("domain", "vertical")}: {label}) ═══\n\n'
                f'{vertical_result.get("content", "")}\n\n'
                f'═══ Web Search Results ═══\n\n')
    return (f'═══ Vertical Search Result ({vertical_result.get("source", "vertical")}) ═══\n\n'
            f'{vertical_result.get("content", "")}\n\n'
            f'═══ Web Search Results ═══\n\n')
