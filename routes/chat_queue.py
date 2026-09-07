"""Owner-scoped message-queue and autopilot HTTP endpoints."""

from lib.log import audit_log, get_logger
from lib.api_response import api_not_found, api_ok
from lib.request_parser import parse_body
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402
from routes.api_v1.auth import request_user_id as _request_user_id, require_scope

logger = get_logger(__name__)

# Legacy POST /api/chat/queue (manual enqueue) was deleted 2026-05-29.
# Conversation Sync v3 turn commands now select steer/queue delivery while a
# lane is busy, so the manual enqueue endpoint has no remaining callers.


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
    user_id = _request_user_id()
    try:
        queue = get_queue(conv_id, user_id=user_id)
    except Exception as e:
        logger.warning('[chat_queue_get] get_queue failed for conv=%s: %s — returning empty list',
                       conv_id, e)
        return api_ok({'items': []})
    # Coordinated bare-array migration (batch 21): the queue array moves
    # under ``items``; Api.chat.queueGet unwraps with a fallback.
    return api_ok({'items': queue})


@api_v1_chat_bp.route('/api/v1/chat/queue/<conv_id>/<queue_id>', methods=['DELETE'], endpoint='ui_chat_queue_remove')
@require_scope('chat')
def chat_queue_remove(conv_id, queue_id):
    """Remove a specific message from the queue."""
    from lib.message_queue import remove_from_queue
    removed = remove_from_queue(
        conv_id, queue_id, user_id=_request_user_id())
    if not removed:
        return api_not_found('Not found')
    return api_ok()
@api_v1_chat_bp.route('/api/v1/chat/queue/<conv_id>', methods=['DELETE'], endpoint='ui_chat_queue_clear')
@require_scope('chat')
def chat_queue_clear(conv_id):
    """Clear all queued messages for a conversation."""
    from lib.message_queue import clear_queue
    count = clear_queue(conv_id, user_id=_request_user_id())
    return api_ok({'cleared': count})


@api_v1_chat_bp.route('/api/v1/chat/autopilot/arm', methods=['POST'], endpoint='ui_chat_autopilot_arm')
@require_scope('chat')
def chat_autopilot_arm():
    """Enable Goal Mode without activating the retired standalone loop.

    The persisted setting governs subsequent accepted turns.  An already-live
    GoalRun remains owned by FlowExecutor; an ordinary in-flight turn is not
    mutated into a different execution mode at its terminal boundary.

    Body: ``{convId}``.

    Returns ``{armed, taskIds, continuationQueued, queueId, deferred}``.
    ``continuationQueued`` means the explicit successor is durable behind the
    current turn; ``deferred`` means the next accepted/idle command starts it.
    """
    data = parse_body()
    user_id = _request_user_id()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')

    from lib.goal_runs.control import request_goal_mode_arm
    result = request_goal_mode_arm(conv_id, user_id=user_id)
    audit_log('autopilot_arm_request', conv_id=conv_id, armed=result['armed'])
    if not result.get('settingPersisted'):
        if result.get('error') == 'conversation_not_found':
            return api_not_found('conversation_not_found')
        from lib.api_response import api_service_unavailable
        return api_service_unavailable(
            result.get('error') or 'goal_setting_unavailable')
    if not result['armed']:
        from lib.api_response import api_conflict
        extras = {
            key: value for key, value in result.items()
            if key not in {'error', 'armed'}
        }
        return api_conflict(
            result.get('error') or 'goal_continuation_not_accepted',
            armed=False,
            **extras,
        )
    return api_ok(result)


@api_v1_chat_bp.route('/api/v1/chat/autopilot/disarm', methods=['POST'], endpoint='ui_chat_autopilot_disarm')
@require_scope('chat')
def chat_autopilot_disarm():
    """Disable Goal Mode and cooperatively cancel its live Flow run.

    The Flow terminal boundary records the typed ``cancelled/human_stop``
    GoalRun transition. Legacy queue/config controls are only neutralized so
    they cannot resurrect the removed second interpreter.

    Body: ``{convId}``. Returns ``{disarmed, markerCleared,
    queuedContinuationsCleared, taskIds}``.
    """
    data = parse_body()
    user_id = _request_user_id()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')

    from lib.goal_runs.control import request_goal_mode_disarm
    result = request_goal_mode_disarm(conv_id, user_id=user_id)
    audit_log('autopilot_disarm_request', conv_id=conv_id,
              disarmed=result['disarmed'])
    return api_ok(result)


@api_v1_chat_bp.route('/api/v1/chat/autopilot/kick', methods=['POST'], endpoint='ui_chat_autopilot_kick')
@require_scope('chat')
def chat_autopilot_kick():
    """Create a durable GoalRun continuation on an idle conversation.

    Use case: the user chatted with autopilot ON, the turn ended, and they
    want the virtual user to keep the conversation going WITHOUT typing — the
    empty-Enter gesture on a conversation that is no longer streaming.  The
    command creates an explicit turn-native continuation and dispatches the
    same Flow-backed GoalRun used by an ordinary Goal Mode send; there is no
    carrier task or post-terminal parent event tunnel.

    Body: ``{convId, config?}`` — ``config`` is the resolved per-conversation
    send config (model, tools, …); when omitted the conversation defaults are
    used by ``build_api_messages_from_db``.

    Returns ``{taskId}`` on success.  Returns 409 with ``{error}`` when there
    is nothing to kick — a task is already running for the conv (the caller
    should ARM instead), the conversation is missing, or its history is empty.
    """
    data = parse_body()
    user_id = _request_user_id()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')
    config = data.get('config') or {}

    from lib.goal_runs.continuation import continue_goal_mode
    result = continue_goal_mode(conv_id, config, user_id=user_id)
    audit_log('autopilot_kick_request', conv_id=conv_id,
              task_id=result.get('taskId'), error=result.get('error'))
    if not result.get('taskId'):
        from lib.api_response import api_conflict
        return api_conflict(result.get('error') or 'cannot_kick',
                            taskId=None)
    return api_ok(result)
