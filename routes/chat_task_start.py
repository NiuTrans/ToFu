"""routes/chat_task_start.py — _start_task_for_conv orchestrator (chat.py slice 4).

**Extraction context** (board epic ``pt_04686ac6054a451e``, slice 4):

Moves the ~150-line ``_start_task_for_conv`` helper — the last piece of
non-handler orchestration living in routes/chat.py — into its own
module. It is the shared task-starter that ``chat_send`` /
``chat_regenerate`` / ``chat_continue`` / ``chat_branch_start`` all
delegate to; keeping it in ``chat.py`` was purely historical (it grew
there as the send handler grew).

Behavioural contract preserved BYTE-FOR-BYTE:

  1. Aborts any stale running tasks for the conversation BEFORE building
     new API messages (stale-task-overwrite guard).
  2. Calls ``build_api_messages_from_db(conv_id, config,
     exclude_last=config.get('excludeLast', False))``.
  3. Returns ``(None, api_error(...))`` on missing conv / empty messages.
  4. Creates the task via ``create_task`` and marks it ``_attended=True``.
  5. Resolves flow selection: user-flow > endpoint > autopilot > default.
     Drops mutually-exclusive toggles when a flow is selected.
  6. Spawns the task via ``threading.Thread(target=<flow_entry>, ...)``
     for flow / endpoint tasks, ``spawn_task`` for the default lane.
  7. Records an ``error`` envelope on the task dict when the spawn itself
     raises; returns ``(None, api_error('Failed to start task', 500))``
     to the caller.

Kept ALIGNED with the 3-tier layering the earlier slices established for
chat.py:

    routes/chat_helpers.py       Pure functions, no state, no IO
    routes/chat_state.py         Process-local state + accessors
    routes/chat_side_effects.py  IO into other lib.* packages
    routes/chat_task_start.py    Task-starter orchestrator (this file)

routes/chat.py imports it and re-exports it as ``_start_task_for_conv``
so:
  * 3 existing test files that ``monkeypatch.setattr(
    'routes.chat._start_task_for_conv', ...)`` continue to work
    unchanged (the re-export is the SAME object as the source; a
    monkey-patch rebinds ``routes.chat._start_task_for_conv``, and
    every internal caller inside ``chat.py`` reads that same binding
    via ``LOAD_GLOBAL`` so the patch is seen at every call site);
  * ``routes/chat.py`` shrinks by ~150 lines without changing any
    handler contract.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from lib.api_response import api_error
from lib.log import get_logger
from lib.tasks_pkg import (
    cleanup_old_tasks,
    create_task,
)

logger = get_logger(__name__)


def _start_task_for_conv(conv_id: str, config: dict[str, Any],
                          data: dict[str, Any] | None = None,
                          *, user_msg_id: str = '',
                          abort_after_ts: float | None = None):
    """Build API messages from DB and start a task. Returns (taskId, error_response).

    Automatically routes to endpoint mode (planner → worker → critic loop)
    when ``config['endpointMode']`` is truthy, so callers (chat_send,
    chat_regenerate, etc.) don't need separate routing logic.

    ``user_msg_id`` — the stable ``_msgId`` of the user message that
    TRIGGERED this task (chat_send's newly-appended user_msg, the target of
    chat_regenerate, the last user before the resumed assistant in
    chat_continue). Stamped onto ``task['_userMsgId']`` so
    ``_finalize_and_emit_done`` can ship it on the DONE SSE frame as
    ``userMsgId`` — the frontend uses that to anchor the turn-ctx capsule
    reconcile to the RIGHT user message (never "the last user in the conv",
    which is wrong the moment an autopilot VU turn or a concurrent send
    plants a newer user message).

    ★ CRITICAL: Before starting a new task, all existing running tasks for
    this conversation are auto-aborted. This prevents the "stale task
    overwrites regeneration" bug where an old task's _sync_result_to_conversation
    races with the new task and corrupts the conversation DB.
    """
    from lib.tasks_pkg import abort_running_tasks_for_conv

    # ★ CRITICAL: abort any stale running tasks for this conversation BEFORE
    #   building the new API messages. Without this, the old task's background
    #   thread may still be running (abort is cooperative) and its persist/sync
    #   writes can land BETWEEN the regen truncation and our DB read here —
    #   resurrecting the just-truncated assistant turn (the "U1 A1 U1 A2"
    #   doubled-context bug). Aborting first stamps `_abort_reason` so the
    #   freshness guard in _sync_result_to_conversation rejects those late
    #   writes; building messages afterwards reads a settled DB state.
    # v2 projection CAS fences stale attempts by turn identity, and distinct
    # lanes/visible role turns may legitimately execute concurrently. Legacy
    # tasks still retain their conversation-wide supersede discipline.
    _aborted_count = (0 if config.get('_turnProtocolV2')
                      else abort_running_tasks_for_conv(conv_id))
    if _aborted_count:
        logger.info('[Chat] conv=%s Auto-aborted %d stale task(s) before new task',
                    conv_id[:8], _aborted_count)

    cleanup_old_tasks()

    # ``excludeLast`` is honored so /api/chat/continue can rebuild messages
    # without the assistant message that is about to be regenerated.
    _exclude_last = bool(config.get('excludeLast', False))
    if config.get('_turnProtocolV2'):
        from lib.turn_lifecycle import build_api_messages
        api_messages = build_api_messages(
            conv_id, config.get('_turnId') or '', config)
    else:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        api_messages = build_api_messages_from_db(
            conv_id, config, exclude_last=_exclude_last)
    if api_messages is None:
        return None, api_error('Conversation not found after save', status=500)
    if not api_messages:
        return None, api_error('No messages to process', status=400)

    task = create_task(
        conv_id, api_messages, config,
        supersede=not bool(config.get('_turnProtocolV2')))
    task['_attended'] = True
    if user_msg_id:
        task['_userMsgId'] = user_msg_id
    task_id = task['id']
    _cfg_model = config.get('model', '?')

    # ★ Send/regen abort-race closer (2026-08-06, conv msftgnt3 incident):
    #   /api/chat/abort-conv sweeps the task registry AND sets a per-conv
    #   marker, but a send/regenerate still inside its synchronous
    #   translate/persist stretch has NO registered task yet — the sweep
    #   finds nothing, and classify_send_intent's one-shot marker check has
    #   already passed. The task then spawns and starts generating seconds
    #   AFTER the user's Stop (the "resumes by itself" half of the report).
    #   Re-check the marker now that the task EXISTS: stamping the abort
    #   pre-spawn lets the prep-phase gates unwind it before any LLM call.
    if abort_after_ts is not None:
        from routes.chat_state import _was_aborted_after
        if _was_aborted_after(conv_id, abort_after_ts):
            task['aborted'] = True
            task['_abort_timestamp'] = time.time()
            task['_abort_reason'] = 'send_abort_race'
            logger.info('[Chat] Task %s conv=%s — abort marker predates task '
                        'registration; spawning pre-aborted (unwinds at the '
                        'prep gate before any LLM call)',
                        task_id[:8], conv_id[:8])

    # ★ A user-SELECTED orchestration flow (Mode dropdown) is mutually
    #   exclusive with the endpoint/autopilot toggles — the flow IS the
    #   execution mode. Drop the toggles so we never double-loop, and so the
    #   resolver's flow-wins precedence isn't masked by a stale endpoint flag.
    _flow_selected = bool(config.get('flowDefinition') or config.get('flowBuiltin')
                          or config.get('flowId'))
    if _flow_selected and (config.get('endpointMode') or config.get('autopilot')):
        logger.info('[Chat] conv=%s endpointMode/autopilot dropped — '
                    'an orchestration flow is selected (flow takes precedence)',
                    conv_id[:8])
        config = dict(config)
        config['endpointMode'] = False
        config['autopilot'] = False
        task['config'] = config

    # ★ Endpoint mode: route to the autonomous planner → worker → critic loop
    is_endpoint = config.get('endpointMode', False)

    # ★ Autopilot is mutually exclusive with endpoint mode — both share the
    #   same "model stopped → loop again" boundary, so running them together
    #   would produce a double-loop with confusing semantics.  Endpoint
    #   wins; the autopilot flag is silently dropped.
    if is_endpoint and config.get('autopilot'):
        logger.warning('[Chat] conv=%s autopilot=True dropped — '
                       'endpointMode=True takes precedence',
                       conv_id[:8])
        config = dict(config)
        config['autopilot'] = False
        task['config'] = config

    # ★ FlowExecutor dispatch (the orchestration-engine convergence point):
    #   a user-SELECTED flow (flowDefinition / flowBuiltin / flowId) is always
    #   honored; endpointMode / autopilot route through the engine only when
    #   their respective flags are on (TOFU_ENDPOINT_VIA_FLOW /
    #   TOFU_AUTOPILOT_VIA_FLOW). Returns None when no engine path applies, so
    #   endpoint mode falls back to the live loop and everything else to a
    #   normal task. All flagging/precedence lives in resolve_chat_flow_entry.
    from lib.orchestration_endpoint_runner import resolve_chat_flow_entry
    _flow_entry = resolve_chat_flow_entry(config)

    if _flow_entry is not None or is_endpoint:
        # Endpoint mode without a flow entry → the live planner→worker→critic
        # loop (default + authoritative). Otherwise the chosen engine entry.
        if _flow_entry is None:
            from lib.tasks_pkg.endpoint import run_endpoint_task
            _flow_entry = run_endpoint_task
        task['endpoint_mode'] = True
        # Seed the FIRST SSE state without reading a stored definition twice.
        # A selected flow starts in the neutral working lane so a plannerless
        # graph cannot create a phantom Planner bubble. The worker resolves
        # the definition exactly once, then LaunchSpec atomically replaces
        # these provisional facts with its canonical projection and phase.
        _initial_phase = 'working' if _flow_selected else 'planning'
        if _flow_selected:
            task['flow_mode'] = True
            task['_flow_projection'] = 'flow'
        task['_endpoint_phase'] = _initial_phase
        task['_endpoint_iteration'] = 0
        logger.info('[Chat] Starting FLOW task %s for conv %s model=%s via=%s',
                    task_id[:8], conv_id[:8], _cfg_model, _flow_entry.__name__)
        try:
            threading.Thread(target=_flow_entry, args=(task,), daemon=True).start()
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start flow/endpoint thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start endpoint task thread.',
                model=config.get('model', ''),
                context='endpoint-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, api_error('Failed to start task', status=500)
    else:
        logger.info('[Chat] Starting task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            from lib.tasks_pkg import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start task thread.',
                model=config.get('model', ''),
                context='task-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, api_error('Failed to start task', status=500)

    return task_id, None


__all__ = ['_start_task_for_conv']
