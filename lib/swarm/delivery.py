"""Result acknowledgement at the shared verified-provider boundary.

Swarm checkpoints remain the durable outbox. In-memory inbox entries are wake
hints: failed acknowledgement retains pending receipts; finalization restores
unconsumed entries. Delivery is at least once after an uncertain ACK, never
claimed merely because a prompt was assembled. Existing result IDs deduplicate
repeated completion callbacks and acknowledgements.
"""
from __future__ import annotations


def acknowledge_presented_results(task: dict) -> bool:
    pending = task.get('_swarm_inject_pending') or []
    if not pending:
        return True
    from lib.swarm.integration._config import swarm_key_for
    from lib.swarm import persistence
    key = swarm_key_for(task)
    from lib.swarm.integration import get_active_session
    session = get_active_session(key)
    if session is not None and session.user_id != task.get('_userId'):
        raise ValueError('result acknowledgement belongs to another owner')
    if session is not None:
        # Confirmed consumption may retire an older session's local receipt,
        # but it must never mark that ID delivered in the replacement session.
        pending = [item for item in pending if not item.get('swarm_run_id')
                   or item['swarm_run_id'] == session.task_id]
        if not pending:
            task.pop('_swarm_inject_pending', None)
            return True
        task['_swarm_inject_pending'] = pending
    elif any(item.get('swarm_run_id') for item in pending):
        return False  # No current identity witness: retain for reconciliation.
    ids = list(dict.fromkeys(str(item['agent_id']) for item in pending if item.get('agent_id')))
    if ids and not (task.get('_transientRuntime') or task.get('_invocationStorageFree')):
        if persistence.mark_delivered(key, ids) is False:
            return False
    if session is not None:
        session.mark_results_delivered(ids)
    task.pop('_swarm_inject_pending', None)
    return True


def restore_unconsumed_results(task: dict) -> None:
    pending = task.pop('_swarm_inject_pending', None) or []
    if not pending:
        return
    from lib import agent_inbox
    from lib.swarm.integration._config import swarm_key_for
    key = swarm_key_for(task)
    from lib.swarm.integration import get_active_session
    session = get_active_session(key)
    if session is not None and session.user_id == task.get('_userId'):
        session.restore_unconsumed_results([item['agent_id'] for item in pending
            if item.get('agent_id') and (not item.get('swarm_run_id')
            or item['swarm_run_id'] == session.task_id)])
    for item in pending:
        agent_inbox.enqueue(key, item.get('value') or '', mode='swarm-update',
                            priority=item.get('priority') or 'later',
                            agent_id=item.get('agent_id') or '',
                            extra={'status': item.get('status') or '',
                                   'swarm_run_id': item.get('swarm_run_id') or ''})


def stage_tool_results(task: dict, session, payloads: list[dict]) -> None:
    """A tool return reserves live delivery; only provider consumption ACKs storage."""
    if session.user_id != task.get('_userId'):
        raise ValueError('result presentation belongs to another owner')
    from lib.agent_inbox import format_swarm_update
    pending = task.setdefault('_swarm_inject_pending', [])
    known = {(item.get('swarm_run_id'), item.get('agent_id')) for item in pending}
    for row in payloads:
        identity = (session.task_id, row['agent_id'])
        if identity in known:
            continue
        known.add(identity)
        value = format_swarm_update(
            agent_id=row['agent_id'], role=row.get('role') or 'general',
            status=row.get('status') or 'completed', elapsed_seconds=row.get('elapsed') or 0,
            tokens=row.get('tokens') or 0, result=row.get('result') or row.get('final_answer') or '',
            output_file=row.get('output_file') or '', error=row.get('error') or '')
        pending.append({'agent_id': row['agent_id'], 'swarm_run_id': session.task_id,
                        'status': row.get('status') or 'completed', 'value': value, 'priority': 'later'})
