"""Register and start the executor bound to an accepted conversation attempt.

The registration callback is invoked before worker start, making durable
attempt-to-task binding the dispatch handshake. Model execution and flow
selection remain internal; public identity is always turnId + attemptId. This
application module has no HTTP dependencies.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.manager import (
    cleanup_old_tasks,
    create_task,
)

logger = get_logger(__name__)


# The serving-loop runtime already sweeps finished tasks on its periodic
# reaper (lib/server_loop_runtime.py). These per-request calls are only a
# best-effort side-effect sweep, so throttle them so the hot start path is
# usually free. ``cleanup_old_tasks`` is resolved through THIS module's global
# (not a captured local) so tests that monkeypatch
# ``lib.conversation_sync.task_start.cleanup_old_tasks`` keep steering the sweep.
_CLEANUP_TTL = 60.0
_last_cleanup_ts = 0.0
_cleanup_lock = threading.Lock()


def _throttled_cleanup_old_tasks():
    global _last_cleanup_ts
    now = time.time()
    with _cleanup_lock:
        if now - _last_cleanup_ts < _CLEANUP_TTL:
            return
        _last_cleanup_ts = now
    cleanup_old_tasks()


def _discard_unstarted_task(task_id: str, conv_id: str) -> None:
    """Best-effort registry rollback while no worker owns the task."""
    try:
        from lib.tasks_pkg.manager import discard_task

        discard_task(task_id, conv_id)
    except Exception:
        # Preserve the original preparation/submission failure. A failed
        # rollback is still observable, but must not replace the error that
        # tells the command service to settle the accepted attempt.
        logger.exception(
            '[Chat] Failed to discard unstarted task=%s conv=%s',
            task_id[:8], conv_id[:8],
        )


def _prepare_unbound_task_dispatch(
    task: dict[str, Any],
    conv_id: str,
    config: dict[str, Any],
    owner_user_id: int,
    abort_after_ts: float | None,
) -> tuple[dict[str, Any], bool, Any, bool]:
    """Prepare dispatch facts before the durable attempt-to-task binding.

    This boundary deliberately contains every operation that can run after
    registry creation but before binding. The caller owns rollback for any
    exception raised here, so an unbound ``running`` carrier cannot survive.
    """
    task_id = task['id']

    # Send/regen abort-race closer (2026-08-06, conv msftgnt3 incident):
    #   /api/chat/abort-conv sweeps the task registry AND sets a per-conv
    #   marker, but a send/regenerate still inside its synchronous
    #   translate/persist stretch has NO registered task yet — the sweep
    #   finds nothing, and classify_send_intent's one-shot marker check has
    #   already passed. The task then spawns and starts generating seconds
    #   AFTER the user's Stop (the "resumes by itself" half of the report).
    #   Re-check the marker now that the task EXISTS: stamping the abort
    #   pre-spawn lets the prep-phase gates unwind it before any LLM call.
    if abort_after_ts is not None:
        from lib.conversation_sync.pending_abort import was_pending_abort_after
        if was_pending_abort_after(conv_id, owner_user_id, abort_after_ts):
            task['aborted'] = True
            task['_abort_timestamp'] = time.time()
            task['_abort_reason'] = 'send_abort_race'
            logger.info('[Chat] Task %s conv=%s — abort marker predates task '
                        'registration; spawning pre-aborted (unwinds at the '
                        'prep gate before any LLM call)',
                        task_id[:8], conv_id[:8])

    # A user-SELECTED orchestration flow (Mode dropdown) is mutually
    # exclusive with goal mode (autopilot). The flow is the execution mode.
    flow_selected = bool(config.get('flowDefinition') or config.get('flowBuiltin')
                         or config.get('flowId'))
    if flow_selected and config.get('autopilot'):
        logger.info('[Chat] conv=%s autopilot dropped — '
                    'an orchestration flow is selected (flow takes precedence)',
                    conv_id[:8])
        config = dict(config)
        config['autopilot'] = False
        task['config'] = config

    # FlowExecutor dispatch is the orchestration-engine convergence point.
    # None means the caller uses the normal-task lane.
    from lib.orchestration_chat_flow_runner import resolve_chat_flow_entry
    flow_entry = resolve_chat_flow_entry(config)
    return config, flow_entry, flow_selected


def start_conversation_attempt_executor(
    conv_id: str,
    config: dict[str, Any],
    *,
    abort_after_ts: float | None = None,
    on_task_registered: Callable[[str], None] | None = None,
):
    """Build model context, register an executor, and start it.

    Returns ``(task_id, None)`` on success or ``(None, reason)`` before any
    executor starts. The application service turns a reason into its durable
    attempt-start error; HTTP mapping remains in the route layer.

    The accepted attempt, authenticated owner, and target turn must already be
    present in ``config``. Stable attempt identity and projection CAS fence
    concurrent lanes; this entry point never falls back to a messages-array
    transcript or a process-global owner.
    """
    from lib.conversation_sync.attempt_identity import is_conversation_attempt
    from lib.identity import require_user_id

    if not is_conversation_attempt(config):
        raise ValueError('conversation executor requires an accepted attempt')
    owner_user_id = require_user_id(
        config.get('_turnOwnerUserId'),
        context='conversation executor',
    )
    from lib.tasks_pkg.plan_mode import normalize_interaction_mode_runtime_config
    config = normalize_interaction_mode_runtime_config(config)

    _throttled_cleanup_old_tasks()

    from lib.turn_lifecycle import build_api_messages
    api_messages = build_api_messages(
        conv_id,
        config.get('_turnId') or '',
        config,
        user_id=owner_user_id,
    )
    if api_messages is None:
        return None, 'conversation_not_found'
    if not api_messages:
        return None, 'empty_model_context'

    task = create_task(
        conv_id, api_messages, config,
        user_id=owner_user_id,
        supersede=False)
    task['_attended'] = True
    task_id = task['id']
    _cfg_model = config.get('model', '?')

    # Turn-native dispatch is a three-stage handshake: register the in-memory
    # task, durably bind that exact id to the accepted attempt, only then
    # launch billable work. Preparation and binding share one rollback fence:
    # no exception can strand an unbound ``running`` registry carrier.
    try:
        config, _flow_entry, _flow_selected = (
            _prepare_unbound_task_dispatch(
                task,
                conv_id,
                config,
                owner_user_id,
                abort_after_ts,
            )
        )
        if on_task_registered is not None:
            on_task_registered(task_id)
    except BaseException:
        _discard_unstarted_task(task_id, conv_id)
        logger.exception(
            '[Chat] Pre-spawn task preparation/binding failed task=%s conv=%s',
            task_id[:8], conv_id[:8],
        )
        raise

    if _flow_entry is not None:
        # The canonical flow task marker set is shared by every
        # FlowExecutor-backed chat run.
        task['flow_mode'] = True
        # Seed the FIRST SSE state without reading a stored definition twice.
        # A selected flow starts in the neutral working lane so a plannerless
        # graph cannot create a phantom Planner bubble. The worker resolves
        # the definition exactly once, then LaunchSpec atomically replaces
        # these provisional facts with its canonical projection and phase.
        task['_flow_phase'] = 'working'
        task['_flow_iteration'] = 0
        if _flow_selected:
            task['_flow_projection'] = 'flow'
        logger.info('[Chat] Starting FLOW task %s for conv %s model=%s via=%s',
                    task_id[:8], conv_id[:8], _cfg_model, _flow_entry.__name__)
        try:
            threading.Thread(target=_flow_entry, args=(task,), daemon=True).start()
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start flow thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            _discard_unstarted_task(task_id, conv_id)
            return None, 'executor_start_failed'
    else:
        logger.info('[Chat] Starting task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            from lib.tasks_pkg.spawn import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            _discard_unstarted_task(task_id, conv_id)
            return None, 'executor_start_failed'

    return task_id, None


__all__ = ['start_conversation_attempt_executor']
