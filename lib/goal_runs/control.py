"""Human control operations for live Flow-backed GoalRuns.

These helpers coordinate owner-scoped live tasks, explicit queued Goal Mode
commands and legacy compatibility markers. Durable GoalRun cancellation is
recorded by the Flow terminal boundary after its cooperative abort check.
"""

from __future__ import annotations

import time

from lib.log import get_logger


logger = get_logger(__name__)


def request_goal_mode_arm(conversation_id: str, *, user_id: int) -> dict:
    """Persist Goal Mode selection, then accept its live/queued control."""
    from lib.conversations import set_conversation_settings

    try:
        settings = set_conversation_settings(
            conversation_id,
            {'autopilotEnabled': True},
            user_id=int(user_id),
        )
    except Exception as error:
        logger.warning(
            '[GoalMode] arm setting write failed conv=%s: %s',
            conversation_id[:8], error,
        )
        return {
            'armed': False,
            'taskIds': [],
            'deferred': True,
            'continuationQueued': False,
            'queueId': None,
            'error': 'goal_setting_unavailable',
            'markerAdded': False,
            'markerCleared': False,
            'settingPersisted': False,
            'modeEnabled': False,
        }
    if settings is None:
        return {
            'armed': False,
            'taskIds': [],
            'deferred': True,
            'continuationQueued': False,
            'queueId': None,
            'error': 'conversation_not_found',
            'markerAdded': False,
            'markerCleared': False,
            'settingPersisted': False,
            'modeEnabled': False,
        }
    result = arm_goal_mode(conversation_id, user_id=int(user_id))
    result.update({'settingPersisted': True, 'modeEnabled': True})
    return result


def request_goal_mode_disarm(conversation_id: str, *, user_id: int) -> dict:
    """Disable future Goal turns and always attempt live/queued cancellation."""
    from lib.conversations import set_conversation_settings

    setting_error = None
    try:
        settings = set_conversation_settings(
            conversation_id,
            {'autopilotEnabled': False},
            user_id=int(user_id),
        )
        setting_persisted = settings is not None
        if not setting_persisted:
            setting_error = 'conversation_not_found'
    except Exception as error:
        setting_persisted = False
        setting_error = 'goal_setting_unavailable'
        logger.warning(
            '[GoalMode] disarm setting write failed conv=%s: %s',
            conversation_id[:8], error,
        )
    result = cancel_goal_mode(conversation_id, user_id=int(user_id))
    result.update({
        'settingPersisted': setting_persisted,
        'modeEnabled': False if setting_persisted else None,
        'error': setting_error,
    })
    return result


def arm_goal_mode(conversation_id: str, *, user_id: int) -> dict:
    """Arm Goal Mode through an existing run or a durable queued command."""
    from lib.message_queue import clear_autopilot_marker
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    # Old queue sentinels drove the standalone post-turn state machine.  They
    # must not survive the cutover and reactivate that interpreter later.
    try:
        marker_cleared = bool(clear_autopilot_marker(
            conversation_id, user_id=int(user_id)))
    except Exception as error:
        marker_cleared = False
        logger.warning(
            '[GoalMode] legacy arm marker cleanup failed conv=%s: %s',
            conversation_id[:8], error,
        )
    live_tasks = [
        task
        for task in chat_task_runtime.snapshot_owned(user_id=int(user_id))
        if task.get('convId') == conversation_id
        and task.get('status') in ('pending', 'running')
    ]
    active_goal_ids = [
        str(task.get('id') or '') for task in live_tasks
        if bool(task.get('_goalRunId')) or (
            task.get('flow_mode')
            and (task.get('config') or {}).get('autopilot') is True
        )
    ]
    queued = None
    if live_tasks and not active_goal_ids:
        # A mode change cannot mutate the interpreter of an accepted turn.
        # Queue an explicit turn command behind it instead; lane settlement
        # will dispatch that command through the normal GoalRun/Flow path.
        from lib.goal_runs.continuation import continue_goal_mode

        queued = continue_goal_mode(
            conversation_id,
            live_tasks[0].get('config') or {},
            user_id=int(user_id),
            queue_if_busy=True,
        )
    continuation_queued = bool((queued or {}).get('queued'))
    continuation_task_id = str((queued or {}).get('taskId') or '')
    task_ids = list(active_goal_ids)
    if continuation_task_id and continuation_task_id not in task_ids:
        task_ids.append(continuation_task_id)
    continuation_error = str((queued or {}).get('error') or '')
    armed = not (
        bool(live_tasks)
        and not task_ids
        and not continuation_queued
    )
    return {
        'armed': armed,
        'taskIds': task_ids,
        'deferred': not bool(task_ids or continuation_queued),
        'continuationQueued': continuation_queued,
        'queueId': (queued or {}).get('queueId'),
        'error': continuation_error or None,
        'markerAdded': False,
        'markerCleared': marker_cleared,
    }


def cancel_goal_mode(conversation_id: str, *, user_id: int) -> dict:
    """Cooperatively cancel live GoalRuns and neutralize legacy loop state."""
    from lib.message_queue import (
        KIND_GOAL_CONTINUATION,
        clear_autopilot_marker,
        clear_queue_kind,
    )
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    owner_user_id = int(user_id)
    try:
        marker_cleared = bool(clear_autopilot_marker(
            conversation_id, user_id=owner_user_id))
    except Exception as error:
        marker_cleared = False
        logger.warning(
            '[GoalMode] legacy disarm marker cleanup failed conv=%s: %s',
            conversation_id[:8], error,
        )
    try:
        queued_continuations_cleared = clear_queue_kind(
            conversation_id,
            KIND_GOAL_CONTINUATION,
            user_id=owner_user_id,
        )
    except Exception as error:
        queued_continuations_cleared = 0
        logger.warning(
            '[GoalMode] queued continuation cleanup failed conv=%s: %s',
            conversation_id[:8], error,
        )
    cancelled_ids: list[str] = []
    legacy_config_ids: list[str] = []
    for task in chat_task_runtime.snapshot_owned(user_id=owner_user_id):
        if task.get('convId') != conversation_id:
            continue
        config = task.get('config')
        if isinstance(config, dict) and config.get('autopilot') is True:
            updated_config = dict(config)
            updated_config['autopilot'] = False
            if chat_task_runtime.update_fields(
                str(task.get('id') or ''),
                fields={'config': updated_config},
                only_if_status=('pending', 'running'),
            ):
                legacy_config_ids.append(str(task.get('id') or ''))
        is_goal_flow = bool(task.get('_goalRunId')) or (
            task.get('flow_mode')
            and (isinstance(config, dict) and config.get('autopilot') is True)
        )
        if not is_goal_flow or task.get('status') not in ('pending', 'running'):
            continue
        task_id = str(task.get('id') or '')
        chat_task_runtime.abort_owned(task_id, user_id=owner_user_id)
        chat_task_runtime.update_fields(
            task_id,
            fields={
                'aborted': True,
                '_abort_timestamp': time.time(),
                '_abort_reason': 'human_stop',
            },
            only_if_status=('pending', 'running'),
        )
        cancelled_ids.append(task_id)

    return {
        'disarmed': bool(
            cancelled_ids
            or legacy_config_ids
            or marker_cleared
            or queued_continuations_cleared
        ),
        'markerCleared': marker_cleared,
        'queuedContinuationsCleared': queued_continuations_cleared,
        'taskIds': cancelled_ids,
        'legacyConfigTaskIds': legacy_config_ids,
    }


__all__ = [
    'arm_goal_mode',
    'cancel_goal_mode',
    'request_goal_mode_arm',
    'request_goal_mode_disarm',
]
