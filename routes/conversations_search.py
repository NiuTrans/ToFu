"""routes/conversations_search.py — Full-text conversation search endpoint.

Extracted from ``routes/conversations.py``. Registers on the same
``conversations_bp`` Blueprint via side-effect import in
``routes/__init__.py``.

Storage: the Sidecar owns the search scan (``conversation.search`` op) —
backend-portable LIKE matching over head-capped
``storage_search_turns.search_text`` projection rows, with snippets projected
inside the Sidecar so GiB-scale transcript/header blobs never cross the RPC.
"""

import time

from quart import request

from lib.api_response import api_internal_error, api_ok
from lib.log import get_logger
from lib.storage.errors import StorageError
from routes.api_v1.auth import request_user_id as _request_user_id
from routes.conversations import conversations_bp
from routes.conversation_turn_errors import storage_failure_response

logger = get_logger(__name__)

#: Searches slower than this (seconds) are logged at WARNING so a regression
#: in the index path is visible in app.log without flipping to DEBUG. Fast
#: searches log at DEBUG to keep the steady-state log quiet.
_SLOW_SEARCH_THRESHOLD_S = 0.3


def _log_search_timing(query: str, n_results: int, elapsed: float) -> None:
    """Log search latency — WARNING when slow, DEBUG otherwise."""
    if elapsed >= _SLOW_SEARCH_THRESHOLD_S:
        logger.warning('[search_convs] SLOW query=%r results=%d elapsed=%.3fs '
                       '(>= %.1fs threshold — check Phase-1 index path)',
                       query, n_results, elapsed, _SLOW_SEARCH_THRESHOLD_S)
    else:
        logger.debug('[search_convs] query=%r results=%d elapsed=%.3fs',
                     query, n_results, elapsed)


@conversations_bp.route('/api/v1/conversations/search', methods=['GET'])
async def search_convs():
    """Server-side full-text search through conversation messages.

    The Sidecar owns the scan (``conversation.search``): backend-portable
    LIKE matching over head-capped ``search_text`` with snippets projected
    inside the Sidecar, so full search blobs never cross the RPC frame.
    """
    query = (request.args.get('q') or '').strip().lower()
    if not query or len(query) < 2:
        return api_ok({'items': []})

    t0 = time.monotonic()

    from lib.storage import get_storage_client
    try:
        items = get_storage_client().query('conversation.search', {
            # Ownership is resolved at the HTTP boundary; the storage/query
            # contract remains explicit and is ready for multi-user authority.
            'user_id': _request_user_id(),
            'query': query,
            'limit': 50,
            'snippet_radius': 40,
        }) or []
    except StorageError as exc:
        # An overloaded/wedged sidecar is not "zero matching conversations".
        # Preserve its typed 503 + Retry-After contract so callers can retry
        # without turning a transient outage into a misleading empty result.
        return storage_failure_response(
            exc, operation='conversation.search')
    except Exception as exc:
        logger.error('[search_convs] unexpected search failure: %s', exc,
                     exc_info=True)
        return api_internal_error('internal_error')

    results = [
        {
            'id': item.get('id'),
            'matchField': 'content',
            'matchSnippet': item.get('snippet') or '',
            'matchRole': 'assistant',
        }
        for item in items
        if isinstance(item, dict) and item.get('id')
    ]

    elapsed = time.monotonic() - t0
    _log_search_timing(query, len(results), elapsed)
    # Coordinated bare-array migration (batch 20): array under ``items``;
    # Api.conversations.search unwraps with an Array.isArray fallback.
    return api_ok({'items': results})
