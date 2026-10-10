"""Open a Flow phase before streaming, using the ordinary Turn authority.

Only the current child identity is retained here. Completed messages belong to
the existing persistence port; model context never reads this display state.
"""

from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.orchestration.visible_identity import visible_phase_identity


def begin_live_flow_turn(task: dict, event: dict) -> None:
    if event.get("flowProjection") != "autopilot" or not is_conversation_attempt(task):
        return
    from lib.tasks_pkg.event_delivery import detach_task_event_delivery
    detach_task_event_delivery(task)
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.storage import get_storage_client, StorageError

    completed = task.get('_flow_turns') or []
    index = len(completed)
    if index and task.get('_flowSyncedCount') != index:
        from lib.turn_lifecycle import sync_visible_run_turns
        # Settlement may retire its carrier. Keep that local mutation private
        # until the next writable phase has also been admitted.
        settled_task = dict(task)
        sync_visible_run_turns(settled_task, completed)
        if settled_task.get('_flowSyncedCount') != index:
            raise StorageError('database_conflict', 'Previous Flow phase has not committed')
    role = event.get('turnRole') or 'worker'
    message = {
        'role': event.get('emits') or 'assistant',
        'content': '', 'thinking': '', '_flowLivePhase': True,
        '_flowIteration': event.get('iteration'),
    }
    if role == 'virtual_user':
        message.update(_isVirtualUser=True, _autopilotRunId=event.get('autopilotRunId'))
    elif message['role'] == 'user':
        message['_isFlowReview'] = True
    elif event.get('phase') == 'planning':
        message['_isFlowPlanner'] = True
    result = get_storage_client(write=True).command('turn.visible.sync', {
        'conversation_id': task['convId'], 'user_id': task_user_id(task),
        'attempt_id': task['_attemptId'], 'root_turn_id': task['_turnId'],
        'messages': [message], 'start_index': index, 'live': True,
    }, f"flow-begin:{task['_attemptId']}:{index}")
    if not result:
        raise StorageError('database_conflict', 'Flow no longer owns its conversation attempt')
    if index and task.get('_flowSyncedCount') != index:
        task['_flowSyncedCount'] = settled_task['_flowSyncedCount']
        task['_turnVisibleRunTurnIds'] = settled_task['_turnVisibleRunTurnIds']
        task.pop('_turnProjectionState', None)
    if index == 0:
        task.pop('_flowLiveTurn', None)
        return  # Root admission was checked by the same storage fence.
    turn_id, attempt_id = visible_phase_identity(task['_turnId'], task['_attemptId'], index)
    task['_flowLiveTurn'] = {'turnId': turn_id, 'attemptId': attempt_id}


def project_live_flow_task(task: dict) -> dict:
    """Borrow task facts for one write while isolating its projection baseline."""
    live = task['_flowLiveTurn']
    projected = dict(task)
    projected.update(_turnId=live['turnId'], _attemptId=live['attemptId'],
                     _flowRootAttemptId=task['_attemptId'],
                     _turnVisibleRunTurnIds=[])
    projected.pop('_turnProjectionState', None)
    if live.get('projectionState') is not None:
        projected['_turnProjectionState'] = live['projectionState']
    return projected
