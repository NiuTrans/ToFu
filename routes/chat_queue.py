"""routes/chat_queue.py — Server-side message queue endpoints.

Extracted from ``routes/chat.py`` so the queue API has its own module.
The handlers register on the same ``chat_bp`` Blueprint (imported here)
to keep the public URLs unchanged.
"""

import json

from flask import jsonify

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import audit_log, get_logger
from lib.api_response import api_not_found, api_ok
from lib.request_parser import parse_body
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402
from routes.api_v1.auth import require_scope
from routes.common import DEFAULT_USER_ID

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


@api_v1_chat_bp.route('/api/v1/chat/autopilot/arm', methods=['POST'], endpoint='ui_chat_autopilot_arm')
@require_scope('chat')
def chat_autopilot_arm():
    """Arm autopilot for a conversation mid-stream ("take over from here").

    Two effects, in order:
      1. Persist ``autopilotEnabled=true`` into the conversation's settings
         so every SUBSEQUENT turn (including a manual send) keeps looping.
      2. Flip ``config['autopilot']=True`` on any LIVE task for this conv
         via ``arm_autopilot`` so the reply currently streaming hands off
         to the virtual user when it stops — without the user re-sending.

    Body: ``{convId}``.

    Returns ``{armed, taskIds}`` — ``armed`` is True iff a live task was
    flipped.  When False (the reply already finished), the persisted
    setting still ensures the loop starts on the user's next send (design
    option A: no auto-spawn at the finish boundary).
    """
    data = parse_body()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')

    # 1. Persist the setting (best-effort — a conv with no row yet just
    #    means nothing was sent; the frontend toggle state covers that).
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID),
        ).fetchone()
        if row:
            try:
                settings = json.loads(row[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot arm] settings parse failed for '
                             'conv=%s: %s', conv_id[:8], e)
                settings = {}
            settings['autopilotEnabled'] = True
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
                (json.dumps(settings, ensure_ascii=False), conv_id,
                 DEFAULT_USER_ID),
            )
    except Exception as e:
        logger.warning('[Autopilot arm] failed to persist autopilotEnabled '
                       'for conv=%s: %s', conv_id[:8], e)

    # 2. Arm any live task so the in-flight reply hands off to the VU.
    from lib.tasks_pkg.autopilot import arm_autopilot
    result = arm_autopilot(conv_id)
    audit_log('autopilot_arm_request', conv_id=conv_id, armed=result['armed'])
    return api_ok(result)


@api_v1_chat_bp.route('/api/v1/chat/autopilot/disarm', methods=['POST'], endpoint='ui_chat_autopilot_disarm')
@require_scope('chat')
def chat_autopilot_disarm():
    """Cancel autopilot for a conversation ("stop taking over").

    The inverse of arm: clears the persistent armed-marker sentinel from the
    queue AND flips ``config['autopilot']=False`` on any live task so the loop
    stops at the current turn's natural end.  Also persists
    ``settings.autopilotEnabled=false`` so a later manual send does not relaunch
    the loop.  Backs the queue-bar cancel button and the toggle-OFF gesture.

    Body: ``{convId}``.  Returns ``{disarmed, markerCleared, taskIds}``.
    """
    data = parse_body()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')

    # Persist autopilotEnabled=false (best-effort).
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID),
        ).fetchone()
        if row:
            try:
                settings = json.loads(row[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot disarm] settings parse failed for '
                             'conv=%s: %s', conv_id[:8], e)
                settings = {}
            settings['autopilotEnabled'] = False
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
                (json.dumps(settings, ensure_ascii=False), conv_id,
                 DEFAULT_USER_ID),
            )
    except Exception as e:
        logger.warning('[Autopilot disarm] failed to persist autopilotEnabled '
                       'for conv=%s: %s', conv_id[:8], e)

    from lib.tasks_pkg.autopilot import disarm_autopilot
    result = disarm_autopilot(conv_id)
    audit_log('autopilot_disarm_request', conv_id=conv_id,
              disarmed=result['disarmed'])
    return api_ok(result)


@api_v1_chat_bp.route('/api/v1/chat/autopilot/summarize', methods=['POST'], endpoint='ui_chat_autopilot_summarize')
@require_scope('chat')
def chat_autopilot_summarize():
    """Generate an on-demand close-out report for a concluded autopilot run.

    For runs that ended WITHOUT a clean ``[VU: TASK_DONE]`` (the user clicked
    Stop or sent a new message) no auto-summary is produced; this backs the
    "Summarize this run" affordance so the user can request one after the fact.

    Body: ``{convId, runId?, config?}``. Returns ``{ok, summary, runId}`` on
    success (``summary`` is the sidecar record — human-only, NOT a chat
    message), or 409/400 with ``{error}`` when there is nothing to summarize.
    """
    data = parse_body()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')
    run_id = (data.get('runId') or '').strip()
    config = data.get('config') or {}

    from lib.tasks_pkg.autopilot import summarize_run
    result = summarize_run(conv_id, run_id, config)
    audit_log('autopilot_summarize_request', conv_id=conv_id,
              run_id=result.get('runId'), ok=result.get('ok'))
    if not result.get('ok'):
        from lib.api_response import api_conflict
        return api_conflict(result.get('error') or 'cannot_summarize')
    return api_ok(result)


@api_v1_chat_bp.route('/api/v1/chat/autopilot/kick', methods=['POST'], endpoint='ui_chat_autopilot_kick')
@require_scope('chat')
def chat_autopilot_kick():
    """Start the virtual-user loop on a FINISHED conversation ("push it forward").

    Use case: the user chatted with autopilot ON, the turn ended, and they
    want the virtual user to keep the conversation going WITHOUT typing — the
    empty-Enter gesture on a conversation that is no longer streaming.  Because
    the autopilot hook only runs at a turn's natural stop (no live task once
    the reply finished), this spawns a thin carrier task whose ``run_task``
    short-circuits straight to the VU hook (``_run_autopilot_kick``).

    Body: ``{convId, config?}`` — ``config`` is the resolved per-conversation
    send config (model, tools, …); when omitted the conversation defaults are
    used by ``build_api_messages_from_db``.

    Returns ``{taskId}`` on success.  Returns 409 with ``{error}`` when there
    is nothing to kick — a task is already running for the conv (the caller
    should ARM instead), the conversation is missing, or its history is empty.
    """
    data = parse_body()
    conv_id = (data.get('convId') or '').strip()
    if not conv_id:
        from lib.api_response import api_bad_request
        return api_bad_request('convId is required', field='convId')
    config = data.get('config') or {}

    from lib.tasks_pkg.autopilot import kick_autopilot
    result = kick_autopilot(conv_id, config)
    audit_log('autopilot_kick_request', conv_id=conv_id,
              task_id=result.get('taskId'), error=result.get('error'))
    if not result.get('taskId'):
        from lib.api_response import api_conflict
        return api_conflict(result.get('error') or 'cannot_kick',
                            taskId=None)
    return api_ok(result)
