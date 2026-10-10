"""Paged conversation search HTTP adapter.

The application service returns independent-index hits plus one owner-scoped
metadata batch. No transcript crosses the search endpoint or gets decoded on
the HTTP event loop. Authority: contracts/conversation_search_v2.json.
"""

import asyncio
import time

from quart import request

from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.conversations.search import search_conversation_page
from lib.log import get_logger
from lib.storage.errors import StorageError
from routes.api_v1.auth import request_user_id as _request_user_id
from routes.conversations import conversations_bp
from routes.conversation_turn_errors import storage_failure_response

logger = get_logger(__name__)
_SLOW_SEARCH_THRESHOLD_S = 0.3


def _log_search_timing(query: str, n_results: int, elapsed: float) -> None:
    """Record bounded timing evidence without retaining users' search text."""
    if elapsed >= _SLOW_SEARCH_THRESHOLD_S:
        logger.warning('[search_convs] SLOW query_chars=%d results=%d elapsed=%.3fs',
                       len(query), n_results, elapsed)
    else:
        logger.debug('[search_convs] query_chars=%d results=%d elapsed=%.3fs',
                     len(query), n_results, elapsed)


@conversations_bp.route('/api/v1/conversations/search', methods=['GET'])
async def search_convs():
    query = request.args.get('q') or ''
    cursor = request.args.get('cursor') or None
    user_id = _request_user_id()
    t0 = time.monotonic()
    from lib.storage import get_storage_client

    def search():
        return search_conversation_page(
            get_storage_client(), user_id=user_id, query=query, cursor=cursor)

    try:
        page = await asyncio.to_thread(search)
    except ValueError as exc:
        return api_bad_request(str(exc))
    except StorageError as exc:
        if exc.code == 'database_protocol_error':
            return api_bad_request(exc.message)
        return storage_failure_response(exc, operation='conversation.search')
    except Exception:
        logger.exception('[search_convs] unexpected search failure')
        return api_internal_error('internal_error')
    _log_search_timing(query, len(page['items']), time.monotonic() - t0)
    return api_ok(page)
