"""Create an explicit turn-native continuation for idle Goal Mode.

The empty-Enter gesture is a human command, not permission for a hidden
standalone state machine.  It therefore creates a normal durable input/output
turn pair and lets the ordinary GoalRun + Flow dispatch path own execution.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.goal_runs.objective import objective_from_task


GOAL_CONTINUATION_TEXT = (
    'Continue toward the current objective. First verify the real current '
    'state, then make the next concrete long-term, root-cause improvement.'
)

_EPHEMERAL_TURN_CONFIG_KEYS = frozenset({
    'excludeLast',
    'toolHistory',
    'contentPrefix',
    'resumePrefill',
    'checkpointToolRounds',
    'checkpointUsage',
    'checkpointApiRounds',
    'checkpointModifiedFiles',
    'checkpointModifiedFileList',
    'checkpointImages',
    'assistantMsgId',
    'msgId',
    '_turnId',
    '_attemptId',
    '_turnActor',
    '_turnKind',
    '_turnOwnerUserId',
    '_goalRunId',
    '_goalRunStatus',
    '_goalRunReason',
    '_goalRunPolicy',
    '_goalContinuationCommand',
})


def continue_goal_mode(
    conversation_id: str,
    config: Mapping[str, Any] | None,
    *,
    user_id: int,
    command_service=None,
    queue_if_busy: bool = False,
) -> dict:
    """Start one durable continuation turn or return a stable refusal code."""
    conversation_id = str(conversation_id or '').strip()
    if not conversation_id:
        return {'taskId': None, 'error': 'conv_id_required'}

    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    busy = False
    for task in chat_task_runtime.snapshot_owned(user_id=int(user_id)):
        if (
            task.get('convId') == conversation_id
            and task.get('status') in ('pending', 'running')
        ):
            busy = True
            break
    if busy and not queue_if_busy:
        return {'taskId': None, 'error': 'task_already_running'}

    from lib.tasks_pkg.conv_message_builder.api import (
        build_api_messages_from_db,
    )
    from lib.tasks_pkg.plan_mode import (
        normalize_interaction_mode_runtime_config,
    )

    runtime_config = normalize_interaction_mode_runtime_config(config)
    for ephemeral_key in _EPHEMERAL_TURN_CONFIG_KEYS:
        runtime_config.pop(ephemeral_key, None)
    runtime_config['planMode'] = False
    runtime_config['activeFlow'] = ''
    runtime_config['autopilot'] = True
    runtime_config['autopilotEnabled'] = True
    for flow_key in ('flowDefinition', 'flowBuiltin', 'flowId'):
        runtime_config.pop(flow_key, None)

    prior_messages = build_api_messages_from_db(
        conversation_id, runtime_config, user_id=int(user_id))
    if prior_messages is None:
        return {'taskId': None, 'error': 'conversation_not_found'}
    if not prior_messages:
        return {'taskId': None, 'error': 'conversation_empty'}
    objective = objective_from_task({'messages': prior_messages})
    if not objective:
        return {'taskId': None, 'error': 'goal_objective_unavailable'}

    if command_service is None:
        from lib.conversation_sync.runtime import conversation_turn_commands
        command_service = conversation_turn_commands

    command_id = 'goal-continuation-' + uuid.uuid4().hex
    projection = {
        'role': 'user',
        'content': GOAL_CONTINUATION_TEXT,
        'timestamp': int(time.time() * 1000),
        '_msgId': command_id,
        '_isVirtualUser': True,
        '_goalContinuation': True,
    }
    command_body = {
        'commandId': command_id,
        'config': runtime_config,
        'inputTurn': projection,
        'inputActor': 'virtual_user',
        'inputKind': 'continuation',
        'actor': 'assistant',
        'kind': 'reply',
    }
    if busy and queue_if_busy:
        command_body['injectMode'] = 'queue'
    try:
        outcome = command_service.create_turn(
            conversation_id,
            int(user_id),
            command_body,
            request_started_at=time.time(),
            trusted_goal_objective=objective,
        )
    except Exception as error:
        from lib.conversation_sync.command_service import AttemptStartFailure
        from lib.turn_lifecycle import LifecycleConflict

        if isinstance(error, LifecycleConflict):
            return {'taskId': None, 'error': error.code}
        if isinstance(error, AttemptStartFailure):
            return {'taskId': None, 'error': 'executor_start_failed'}
        raise
    value = outcome.value
    if isinstance(value, dict) and value.get('queued'):
        return {
            'taskId': None,
            'queued': True,
            'queueId': value.get('queueId'),
            'position': value.get('position'),
            'goalObjective': objective,
        }
    attempt = value.get('attempt') if isinstance(value, dict) else None
    task_id = str((attempt or {}).get('taskId') or '')
    if not task_id:
        return {'taskId': None, 'error': 'executor_start_failed'}
    return {
        'taskId': task_id,
        'goalObjective': objective,
        'submittedTurn': value.get('submittedTurn'),
        'turn': value.get('turn'),
    }


__all__ = ['GOAL_CONTINUATION_TEXT', 'continue_goal_mode']
