"""Conversation cancellation, command interruption, and flow-trace routes.

Conversation Sync v3 is the only transcript/status read protocol.
"""

from __future__ import annotations

import json
import time

from lib.api_response import api_not_found, api_ok
from lib.log import audit_log, get_logger
from lib.tasks_pkg.manager.runtime import chat_task_runtime
from routes.api_v1.auth import current_auth, require_scope
from routes.api_v1.chat import api_v1_chat_bp
from lib.conversation_sync.pending_abort import mark_pending_abort

logger = get_logger(__name__)


def _load_persisted_task_result(task_id, *, user_id: int):
    """Read one owner-scoped task result from the storage authority."""
    from lib.storage import get_storage_client
    record = get_storage_client().query(
        'record.get', {'namespace': 'task_results', 'key': task_id})
    value = (record or {}).get('value')
    if not isinstance(value, dict) or int(value.get('user_id') or 0) != int(user_id):
        return None
    return value

@api_v1_chat_bp.route('/api/v1/chat/abort-conv/<conv_id>', methods=['POST'], endpoint='ui_chat_abort_conv')
@require_scope('chat')
def chat_abort_conv(conv_id):
    """Abort all running tasks for a conversation by conv ID.

    Used when the frontend aborts during translation and never received a
    taskId — the server may have already started a task that needs to be
    killed.  This is the convId-based counterpart of ``/api/chat/abort/<task_id>``.

    Also records a per-conv abort marker so any /api/chat/send still
    blocked inside auto-translate can detect the abort and bail out
    before persisting / enqueueing / dispatching the message.
    """
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv
    from routes.api_v1.auth import request_user_id

    owner_user_id = int(request_user_id())
    mark_pending_abort(conv_id, owner_user_id)
    aborted = abort_running_tasks_for_conv(
        conv_id, user_id=owner_user_id)
    # ③-3 (): also tombstone running DB rows the registry has
    #   LOST — the in-registry sweep above structurally cannot reach them
    #   (measured 2026-08-01: abort-conv returned 0 while a live task spun).
    from lib.tasks_pkg.manager import (
        plant_abort_tombstones_for_conv as _plant_conv,
    )
    tombstoned = _plant_conv(
        conv_id, source='api_chat_abort_conv', user_id=owner_user_id)
    if aborted or tombstoned:
        logger.info('[Chat] Abort-by-conv conv=%s — aborted %d task(s), '
                    'tombstoned %d registry-lost',
                    conv_id[:8], aborted, tombstoned)
    else:
        logger.debug('[Chat] Abort-by-conv conv=%s — no running tasks found', conv_id[:8])
    return api_ok({'aborted': aborted, 'tombstoned': tombstoned})
@api_v1_chat_bp.route('/api/v1/chat/abort/<task_id>', methods=['POST'], endpoint='ui_chat_abort')
@require_scope('chat')
def chat_abort(task_id):
    """Abort a running task by id.

    Sets ``task['aborted']`` (the orchestrator polls this between rounds),
    SIGTERMs any spawned ``run_command`` subprocess, and signals the external
    backend if one is in use. Idempotent — a duplicate abort logs at WARNING
    and returns ok.

    This is the single, authoritative abort handler — it carries the real
    subprocess / external-backend kill logic. The previous duplicate stub in
    ``routes/api_v1/chat.py`` (which only flipped ``aborted``) was removed.
    """
    from routes.api_v1.auth import request_user_id
    owner_user_id = int(request_user_id())
    task = chat_task_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        # ③-3 (): a registry miss no longer means “no such
        #   task” — the 2026-08-01 evaporation family proved a LIVE task can
        #   be missing here (abort 404'd while the worker kept cycling).
        #   When a running DB row exists, plant an abort tombstone: the
        #   worker's abort_check consumes it at its next retry poll.
        from lib.tasks_pkg.manager import plant_abort_tombstone as _plant
        if _plant(
                task_id, source='api_chat_abort', user_id=owner_user_id):
            return api_ok(taskId=task_id, status='abort_signaled',
                          note='task absent from registry; abort tombstone '
                               'planted — the live worker consumes it at its '
                               'next abort poll')
        return api_not_found('Not found')
    task_owner_id = owner_user_id
    was_already_aborted = task.get('aborted', False)
    chat_task_runtime.abort_owned(task_id, user_id=owner_user_id)
    chat_task_runtime.update_fields(
        task_id,
        fields={'aborted': True, '_abort_timestamp': time.time()},
    )
    audit_log('api_chat_abort',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    # Log comprehensive abort context
    _status = task.get('status', '?')
    _elapsed = time.time() - task.get('created_at', time.time())
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _model = task.get('model', '?')
    _conv_id = task.get('convId', '?')
    if was_already_aborted:
        logger.warning('[Chat] Task %s abort DUPLICATE — already aborted. conv=%s status=%s',
                       task_id, _conv_id, _status)
    else:
        logger.info('[Chat] Task %s ABORT RECEIVED — conv=%s model=%s status=%s '
                    'elapsed=%.1fs content=%dchars thinking=%dchars',
                    task_id, _conv_id, _model, _status, _elapsed, _content_len, _thinking_len)
    # ── Kill any running subprocess (run_command) ──
    _sub_pid = task.get('_subprocess_pid')
    if _sub_pid:
        try:
            import os as _os
            import signal as _signal
            _pgid = task.get('_subprocess_pgid')
            if _pgid:
                _os.killpg(_pgid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess process group pgid=%d',
                            task_id[:8], _pgid)
            else:
                _os.kill(_sub_pid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess pid=%d',
                            task_id[:8], _sub_pid)
        except (OSError, ProcessLookupError) as e:
            logger.debug('[Chat] Task %s — subprocess kill skipped: %s', task_id[:8], e)

    # ── User-Stop busy-projection broadcast ──
    # The busy projection (snapshot_running_by_conv → conv_has_work_in_flight)
    # already EXCLUDES an aborted task by design ("aborted always wins: the
    # instant the user presses Stop the conversation must read idle"), but a
    # frame only leaves the server when someone EMITS it — and this seam
    # never did. Without it the originating tab's authoritative busy Set
    # still holds this tid after finishStream cleared the local handles
    # (activeStreams + conv.activeTaskId): convIsBusy keeps the composer in
    # Stop shape while Priority-3 of the stop cascade has no handle left, so
    # every further click is a silent no-op until the task fully unwinds and
    # the TERMINAL frame lands (up to a whole tool call later) — the "Stop
    # takes several clicks" report. This is the third emit site of the SAME
    # broadcast: the supersede sweep (manager/_registry.py P3) and
    # The terminal conversation notification already carries the other two.
    # Unconditional: a duplicate abort re-asserts the idle projection for a
    # client that missed the first frame. Fail-open: a notify/import error
    # must never break the abort path (notify_conv_changed is fail-open too).
    if _conv_id and _conv_id != '?':
        try:
            from lib.conversations.change_notifications import notify_conv_changed
            notify_conv_changed(_conv_id, rev=None, user_id=task_owner_id)
        except Exception as _ne:
            logger.warning('[Chat] Task %s abort busy-notify failed: %s',
                           task_id[:8], _ne)

    return api_ok()


@api_v1_chat_bp.route('/api/v1/chat/interrupt-command/<task_id>', methods=['POST'],
                      endpoint='ui_chat_interrupt_command')
@require_scope('chat')
def chat_interrupt_command(task_id):
    """Interrupt the task's CURRENTLY-RUNNING run_command — WITHOUT aborting
    the task (owner directive 2026-08-01, ).

    Sets ``task['_cmd_interrupt']``; the run_command read loop (which polls
    every ~0.2s) consumes it, kills the process tree, and returns the
    PARTIAL output plus the interruption marker as an ordinary tool result —
    so the model sees what the command produced before being stopped and the
    turn continues. This is the per-command counterpart of
    ``/api/v1/chat/abort/<task_id>`` (which stops the WHOLE turn).

    Response shapes (all 200 except a missing task):
      * ``{'interrupted': True, 'pid': N}``                  — flag planted
      * ``{'interrupted': False, 'reason': 'task_not_running'}``
      * ``{'interrupted': False, 'reason': 'no_active_command'}`` — the task
        is not inside a run_command right now (nothing to interrupt)
    """
    from routes.api_v1.auth import request_user_id
    owner_user_id = int(request_user_id())
    task = chat_task_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        return api_not_found('Not found')
    if task.get('status') != 'running' or task.get('aborted'):
        return api_ok({'interrupted': False, 'reason': 'task_not_running'})
    pid = task.get('_subprocess_pid')
    if not pid:
        return api_ok({'interrupted': False, 'reason': 'no_active_command'})
    chat_task_runtime.update_fields(
        task_id,
        fields={'_cmd_interrupt': {
            'source': 'user', 'ts': time.time(), 'note': '', 'pid': pid,
        }},
        only_if_status='running',
    )
    audit_log('api_chat_interrupt_command',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    logger.info('[Chat] Task %s — user interrupt requested for run_command pid=%s',
                task_id[:8], pid)
    return api_ok({'interrupted': True, 'pid': pid})


@api_v1_chat_bp.route('/api/v1/chat/flow-trace/<task_id>', methods=['GET'],
                      endpoint='ui_chat_flow_trace')
@require_scope('chat')
def chat_flow_trace(task_id):
    """Return the per-node run trace for an orchestration-flow chat task.

    The trace is the traceability record FlowExecutor accumulates: one entry
    per executed node carrying the RESOLVED delegation brief (the rendered
    role prompt — "what is this role doing?"), a bounded copy of its effective
    input context, its full bounded output, the message axis / isolation /
    loop iteration, and deliverable counts + timing. Powers the Studio
    canvas/inspector overlay.

    Served from the live in-memory task first (mid-run / just-finished), then
    the persisted ``task_results.metadata.flowTrace`` (survives reload /
    restart). Returns ``{ok, taskId, flowLabel, trace: [...]}``.
    """
    from routes.api_v1.auth import request_user_id
    owner_user_id = int(request_user_id())
    task = chat_task_runtime.get_owned(task_id, user_id=owner_user_id)
    if task is not None:
        return api_ok({
            'taskId': task_id,
            'flowLabel': task.get('_flow_label', ''),
            'trace': task.get('_flow_trace') or [],
        })

    row = _load_persisted_task_result(task_id, user_id=owner_user_id)
    if row and row.get('metadata'):
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] flow-trace %s — metadata parse failed: %s',
                           task_id[:8], e)
            meta = {}
        return api_ok({
            'taskId': task_id,
            'flowLabel': meta.get('flowLabel', ''),
            'trace': meta.get('flowTrace') or [],
        })
    return api_not_found('Task not found')
