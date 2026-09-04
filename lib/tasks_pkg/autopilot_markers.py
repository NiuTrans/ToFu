"""Durable autopilot arm/disarm state.

These helpers manipulate the persistent autopilot marker and the live-task
``config['autopilot']`` flag:

  * :func:`arm_autopilot` — runtime-arm gesture: flip
    ``config['autopilot']=True`` on live tasks + persist the queue-lane
    marker. Refuses while a Flow-managed task owns the conversation.
  * :func:`disarm_autopilot` — the inverse: clear the marker + flip
    live-config off + emit the run-concluded record.
  * :func:`_marker_exists` — the marker-probe helper the arm result uses
    to compute the final ``armed`` flag.

The facade module ``lib.tasks_pkg.autopilot`` re-exports these entry points.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

#  slice 3 — ``conclude_run`` now lives in a LEAF module
# (autopilot_run_lifecycle.py), so we can import it at MODULE TOP
# without recreating the cycle slice 2 had to guard with a lazy
# import.  autopilot_run_lifecycle has ZERO dependencies on
# autopilot.py — the dependency graph is now strictly one-way.
from lib.tasks_pkg.autopilot_run_lifecycle import conclude_run

logger = get_logger(__name__)


def arm_autopilot(conv_id: str, *, user_id: int) -> dict:
    """Arm autopilot for a conversation whose task is already in flight.

    Use case: the user chatted with autopilot OFF, then decides to step
    away mid-reply and wants the virtual user to take over at the next
    natural stop.  Toggling the frontend button only affects the NEXT
    task — the in-flight task's ``config['autopilot']`` was frozen at
    creation time, so its end-of-turn hook would never fire.

    This flips ``config['autopilot'] = True`` on every live (status=
    ``running``) task for the conversation.  Because ``_finalize_and_emit_done``
    re-reads ``is_autopilot_enabled(task)`` at finalize, the running task
    will now run the VU hook when it stops.  Mutating ``config`` (rather
    than a side flag) also means the value propagates to autopilot
    follow-ups via ``_start_followup_task``'s ``dict(task['config'])``,
    so the loop continues until the VU emits ``[VU: TASK_DONE]``.

    Flow-managed tasks (``_flow_managed``) are skipped — the live loop must
    not double-drive a task the engine already loops.

    Returns ``{'armed': bool, 'taskIds': [...]}`` — ``armed`` is True iff
    at least one live task was flipped.  When no task is live (the reply
    already finished), ``armed`` is False and the caller should rely on
    the persisted ``autopilotEnabled`` setting to kick off the loop on the
    user's next send.
    """
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    armed_ids: list[str] = []
    marker_cfg: dict = {}
    live_tasks = [
        task
        for task in chat_task_runtime.snapshot_owned(user_id=int(user_id))
        if (task.get('convId') == conv_id
            and task.get('status') in ('pending', 'running'))
    ]
    flow_blocked = any(
        not task.get('_vu_subtask')
        and task.get('_flow_managed')
        for task in live_tasks
    )
    if not flow_blocked:
        for task in live_tasks:
            if task.get('_flow_managed') or task.get('_vu_subtask'):
                continue
            config = task.get('config')
            if not isinstance(config, dict):
                continue
            if not marker_cfg:
                marker_cfg = dict(config)
            if config.get('autopilot'):
                continue
            updated_config = dict(config)
            updated_config['autopilot'] = True
            task_id = str(task.get('id') or '')
            if chat_task_runtime.update_fields(
                task_id,
                fields={'config': updated_config},
                only_if_status=('pending', 'running'),
            ):
                armed_ids.append(task_id)

    if flow_blocked:
        logger.info('[Autopilot] Arm refused for conv=%s — a flow engine run '
                    'is live (mutually exclusive)', conv_id[:8])
        return {'armed': False, 'taskIds': [], 'markerAdded': False}

    # Persist the armed-marker sentinel in the queue so the arm survives a
    # page reload, shows in the queue bar (cancellable), and — critically —
    # keeps autopilot armed even when no task is live (the "I'll step away,
    # take over when the current reply finishes" gesture works whether or not
    # a reply is still streaming).  Idempotent: at most one marker per conv.
    marker_added = False
    try:
        from lib.message_queue import arm_autopilot_marker
        res = arm_autopilot_marker(
            conv_id, marker_cfg, user_id=user_id)
        marker_added = res.get('armed', False)
    except Exception as e:
        logger.warning('[Autopilot] failed to persist armed-marker for '
                       'conv=%s: %s', conv_id[:8], e)

    if armed_ids:
        logger.info('[Autopilot] Armed %d live task(s) for conv=%s: %s '
                    '(marker_added=%s)', len(armed_ids), conv_id[:8],
                    [t[:8] for t in armed_ids], marker_added)
    else:
        logger.info('[Autopilot] Arm requested for conv=%s — no live task to '
                    'flip; persistent marker now governs (marker_added=%s)',
                    conv_id[:8], marker_added)
    audit_log('autopilot_armed', conv_id=conv_id, task_ids=armed_ids,
              marker_added=marker_added)

    # ``armed`` reflects whether autopilot is now armed for the conv — True if
    # a live task was flipped OR a marker is in place.
    armed = (
        bool(armed_ids)
        or marker_added
        or _marker_exists(conv_id, user_id=user_id)
    )
    return {'armed': armed, 'taskIds': armed_ids, 'markerAdded': marker_added}


def _marker_exists(conv_id: str, *, user_id: int) -> bool:
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id, user_id=user_id)
    except Exception as e:
        logger.debug('[Autopilot] _marker_exists probe failed for conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return False


def disarm_autopilot(conv_id: str, *, user_id: int) -> dict:
    """Cancel autopilot for a conversation: clear the marker + live config.

    The inverse of :func:`arm_autopilot`.  Removes the persistent armed-marker
    sentinel AND flips ``config['autopilot']=False`` on any live task so the
    loop stops at the current turn's natural end.  Used by the queue-bar
    cancel button and the toggle-OFF gesture.

    Returns ``{disarmed, markerCleared, taskIds}``.
    """
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    marker_cleared = False
    try:
        from lib.message_queue import clear_autopilot_marker
        marker_cleared = clear_autopilot_marker(
            conv_id, user_id=user_id)
    except Exception as e:
        logger.warning('[Autopilot] disarm: marker clear failed for conv=%s: %s',
                       conv_id[:8], e)

    cleared_ids: list[str] = []
    for task in chat_task_runtime.snapshot_owned(user_id=int(user_id)):
        if task.get('convId') != conv_id or task.get('_vu_subtask'):
            continue
        config = task.get('config')
        if not (isinstance(config, dict) and config.get('autopilot')):
            continue
        updated_config = dict(config)
        updated_config['autopilot'] = False
        task_id = str(task.get('id') or '')
        if chat_task_runtime.update_fields(
            task_id,
            fields={'config': updated_config},
        ):
            cleared_ids.append(task_id)

    # Symmetric close-out — the manual-stop arm of the conclude contract.
    #   Historically disarm was "dumb": it cleared the marker/flag but emitted
    #   NO run-level fact, forcing the frontend to INFER run-end from stream
    #   absence (the inter-turn-gap heuristic behind premature folds). Now we
    #   write the BACKEND-AUTHORITATIVE concluded record (reason=stopped, no
    #   report) so the fold keys on a durable fact — and return it so the
    #   calling client (which may have NO live SSE stream, the idle-disarm
    #   case) can fold instantly without a reload. Self-guards: no run id →
    #   None (nothing was ever an autopilot run to conclude).
    #
    #  slice 3: ``conclude_run`` was moved into a leaf module
    # (``autopilot_run_lifecycle``), so we import it at MODULE TOP now.
    # The cycle slice 2 guarded via lazy-import no longer exists — the
    # dependency graph is strictly one-way (see autopilot_run_lifecycle
    # docstring for the full picture).
    concluded = None
    try:
        concluded = conclude_run(
            conv_id, user_id=user_id, reason='stopped')
    except Exception as e:
        logger.warning('[Autopilot] disarm: conclude_run failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)

    logger.info('[Autopilot] Disarmed conv=%s (markerCleared=%s, tasks=%s, concluded=%s)',
                conv_id[:8], marker_cleared, [t[:8] for t in cleared_ids],
                bool(concluded))
    audit_log('autopilot_disarmed', conv_id=conv_id,
              marker_cleared=marker_cleared, task_ids=cleared_ids,
              concluded=bool(concluded))
    result = {'disarmed': marker_cleared or bool(cleared_ids),
              'markerCleared': marker_cleared, 'taskIds': cleared_ids}
    if concluded is not None:
        result['runConcluded'] = concluded
    return result


__all__ = ['arm_autopilot', 'disarm_autopilot', '_marker_exists']
