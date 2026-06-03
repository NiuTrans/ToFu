"""routes/chat_queue.py — Server-side message queue endpoints.

Extracted from ``routes/chat.py`` so the queue API has its own module.
The handlers register on the same ``chat_bp`` Blueprint (imported here)
to keep the public URLs unchanged.
"""

from flask import jsonify

from lib.log import get_logger
from lib.api_response import api_not_found, api_ok
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402
from routes.api_v1.auth import require_scope

logger = get_logger(__name__)


# Legacy POST /api/chat/queue (manual enqueue) deleted 2026-05-29.
# /api/v1/chat/send now auto-detects whether to start immediately or
# enqueue, so the manual enqueue endpoint had no remaining callers.


@api_v1_chat_bp.route('/api/v1/chat/queue/<conv_id>', methods=['GET'], endpoint='ui_chat_queue_get')
@require_scope('chat')
def chat_queue_get(conv_id):
    """Get all queued messages for a conversation.

    This endpoint is polled frequently by the frontend.  When the DB
    connection pool is saturated (e.g. during startup / burst traffic)
    ``get_queue`` can raise ``psycopg.OperationalError: timeout expired``.
    Bubbling that to a 500 produces scary stack traces in ``error.log``
    and breaks the frontend poll loop.  Since "empty queue" is a safe
    degraded response for a polling endpoint, we catch DB-side failures
    here and return ``[]`` with a warning log; the next poll will retry
    cleanly once the pool frees up.
    """
    from lib.message_queue import get_queue
    try:
        queue = get_queue(conv_id)
    except Exception as e:
        logger.warning('[chat_queue_get] get_queue failed for conv=%s: %s — returning empty list',
                       conv_id, e)
        return jsonify([])
    return jsonify(queue)


@api_v1_chat_bp.route('/api/v1/chat/queue/<conv_id>/<queue_id>', methods=['DELETE'], endpoint='ui_chat_queue_remove')
@require_scope('chat')
def chat_queue_remove(conv_id, queue_id):
    """Remove a specific message from the queue."""
    from lib.message_queue import remove_from_queue
    removed = remove_from_queue(conv_id, queue_id)
    if not removed:
        return api_not_found('Not found')
    return api_ok()
@api_v1_chat_bp.route('/api/v1/chat/queue/<conv_id>', methods=['DELETE'], endpoint='ui_chat_queue_clear')
@require_scope('chat')
def chat_queue_clear(conv_id):
    """Clear all queued messages for a conversation."""
    from lib.message_queue import clear_queue
    count = clear_queue(conv_id)
    return jsonify({'cleared': count})
