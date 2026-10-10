"""Restart eligibility and checkpoint safety; no provider calls or transport.

Continue grants a new execution, never resurrects the dead process. The Turn
repository remains cancellation authority; swarm rows are recovery evidence.
"""
from __future__ import annotations

import threading

_RESTART_REASONS = frozenset({
    'server_restart', 'server_shutdown', 'process_restart', 'process_killed',
    'manual_restart', 'boot_recovery',
})
_RECOVERY_LOCKS = tuple(threading.RLock() for _ in range(64))


def read_recovery_turns(conv_id: str, *, user_id: int, lane_id: str = 'main',
                        after_ordinal: int | None = None, limit: int = 2000) -> dict:
    """Read only the required suffix at one authority-owned sync sequence.

    The first stale page returns the current head without hydrating history.
    Every subsequent page uses that head; a changed head restarts the read.
    Only identity/status headers survive each page, never tool transcripts.
    """
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.identity import require_user_id
    from lib.storage import get_storage_client
    from lib.storage.errors import StorageError

    user_id = require_user_id(user_id, context='swarm recovery authority')
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('Swarm recovery limit must be between 1 and 2000')
    if after_ordinal is not None and (type(after_ordinal) is not int or after_ordinal < 0):
        raise ValueError('Swarm recovery ordinal must be a nonnegative integer')
    repository = SidecarConversationSyncRepository(client_factory=get_storage_client)
    sequence = 0
    page_limit = min(64, limit)
    before = None
    turns = []
    changes = 0
    while True:
        page = repository.turn_page(conv_id, user_id, lane_id=lane_id,
            expected_sync_sequence=sequence, before_ordinal=before,
            limit=min(page_limit, limit - len(turns)))
        if page is None:
            raise StorageError('database_not_found', 'Swarm recovery conversation is unavailable')
        if page.get('stale'):
            head = page.get('syncSequence')
            if type(head) is not int or head < 0 or changes >= 3:
                raise StorageError('database_busy',
                    'Swarm recovery authority changed during paging', True)
            sequence, before, turns = head, None, []
            changes += 1
            continue
        rows = page.get('turns')
        if (not isinstance(rows, list) or len(rows) > min(page_limit, limit - len(turns))
                or type(page.get('syncSequence')) is not int
                or type(page.get('hasMore')) is not bool
                or page['syncSequence'] != sequence):
            raise StorageError('database_protocol_error', 'Invalid swarm recovery page')
        for row in reversed(rows):
            ordinal = row.get('ordinal') if isinstance(row, dict) else None
            if (type(ordinal) is not int or ordinal < 0
                    or row.get('laneId') != lane_id
                    or not isinstance(row.get('status'), str) or not row['status']
                    or (before is not None and ordinal >= before)
                    or (turns and ordinal >= turns[-1]['ordinal'])):
                raise StorageError('database_protocol_error', 'Invalid swarm recovery Turn order')
            if after_ordinal is not None and ordinal <= after_ordinal:
                return {'turns': list(reversed(turns))}
            turns.append({key: row[key] for key in (
                'turnId', 'laneId', 'ordinal', 'status', 'currentAttemptId', 'settlement')
                if key in row})
        if len(turns) >= limit or not page.get('hasMore'):
            return {'turns': list(reversed(turns))}
        cursor = page.get('nextBeforeOrdinal')
        if not rows or type(cursor) is not int or cursor != rows[0]['ordinal']:
            raise StorageError('database_protocol_error', 'Invalid swarm recovery cursor')
        before = cursor
        page_limit = min(page_limit, len(rows))
        del page, rows  # Release hydrated transcripts before fetching another page.


def recovery_lock(key: str):
    return _RECOVERY_LOCKS[hash(key) % len(_RECOVERY_LOCKS)]


def restart_source_attempt(sess: dict, *, user_id: int, receipt: dict | None = None) -> dict | None:
    """Require exact attempt state and authority-written predecessor evidence.

    Attempt queries intentionally do not expose the mutable Turn settlement.
    Cold pause reads it only while that attempt still owns the Turn; accepted
    Continue instead carries its transactional, immutable `_resumeSource`.
    """
    from lib.turn_lifecycle import get_attempt, get_turn
    config = sess.get('config') or {}
    parent = config.get('parent_cfg') or {}
    if (config.get('user_id') != user_id or sess.get('status') != 'running'
            or not parent.get('_swarmParentTurnId')
            or not parent.get('_swarmParentAttemptId')):
        return None
    source = get_attempt(parent['_swarmParentAttemptId'], user_id=user_id)
    task_id = parent.get('_swarmParentTaskId') or sess.get('task_id')
    if (source.get('turnId') != parent['_swarmParentTurnId']
            or source.get('taskId') != task_id):
        return None
    if receipt is None:
        turn = get_turn(sess['conv_id'], parent['_swarmParentTurnId'], user_id=user_id)
        if turn.get('currentAttemptId') != parent['_swarmParentAttemptId']:
            return None
        receipt = {'attemptId': parent['_swarmParentAttemptId'], 'taskId': task_id,
                   'status': turn.get('status'),
                   'cause': (turn.get('settlement') or {}).get('cause')}
    if (not isinstance(receipt, dict)
            or receipt.get('attemptId') != parent['_swarmParentAttemptId']
            or receipt.get('taskId') != task_id
            or receipt.get('status') != source.get('status')):
        return None
    if source.get('status') == 'interrupted' and receipt.get('cause') in _RESTART_REASONS:
        return source
    failed = parent.get('_swarmRecoveryFailedAttempt') or {}
    if (source.get('status') == 'failed' and receipt.get('cause')
            and failed == {'attemptId': parent['_swarmParentAttemptId'], 'taskId': task_id}):
        # Only this exact server-recorded failed activation may retry. It did
        # not authorize crossing an intervening Stop/regenerate/supersession.
        return source
    return None


def detached_recovery_allowed(sess: dict, *, user_id: int) -> bool:
    """Completed spawning authority survives restart until Stop or replacement.

    The session must still own unfinished work; completion alone never grants
    fresh work to a terminated swarm. Reads stay scoped to the original owner.
    """
    from lib.turn_lifecycle import get_attempt, get_turn
    config = sess.get('config') or {}
    parent = config.get('parent_cfg') or {}
    turn_id = parent.get('_swarmParentTurnId')
    attempt_id = parent.get('_swarmParentAttemptId')
    conv_id = sess.get('conv_id')
    task_id = parent.get('_swarmParentTaskId') or sess.get('task_id')
    if (config.get('user_id') != user_id or sess.get('status') != 'running'
            or not conv_id or not turn_id or not attempt_id or not task_id):
        return False
    source = get_attempt(attempt_id, user_id=user_id)
    if (source.get('attemptId') != attempt_id
            or source.get('conversationId') != conv_id
            or source.get('turnId') != turn_id or source.get('taskId') != task_id
            or source.get('status') != 'completed'):
        return False
    turn = get_turn(conv_id, turn_id, user_id=user_id)
    ordinal = turn.get('ordinal')
    if (turn.get('turnId') != turn_id or turn.get('laneId') != 'main'
            or turn.get('currentAttemptId') != attempt_id
            or turn.get('status') != 'completed'
            or (turn.get('settlement') or {}).get('cause') != 'provider_finished'
            or not isinstance(ordinal, int) or isinstance(ordinal, bool)):
        return False
    page = read_recovery_turns(conv_id, user_id=user_id, lane_id='main',
                      after_ordinal=ordinal, limit=2000)
    turns = page.get('turns')
    return (isinstance(turns, list) and len(turns) < 2000
            and all(isinstance(item, dict)
                    and item.get('status') in ('pending', 'running', 'completed')
                    for item in turns))


def continue_recovery_allowed(sess: dict, task: dict, *, user_id: int,
                              allow_parent_completion: bool = False) -> bool:
    """Require accepted lineage; only admitted detached work may outlive completion."""
    statuses = ('pending', 'running', 'completed') if allow_parent_completion else ('pending', 'running')
    from lib.turn_lifecycle import get_attempt, get_turn
    cfg = task.get('config') or {}
    persisted = sess.get('config') or {}
    parent = persisted.get('parent_cfg') or {}
    if (task.get('aborted') or cfg.get('_turnOperation') not in ('continue', 'checkpoint_resume')
            or persisted.get('user_id') != user_id or sess.get('status') != 'running'
            or not task.get('convId') or sess.get('conv_id') != task['convId']
            or not cfg.get('_turnId') or parent.get('_swarmParentTurnId') != cfg['_turnId']
            or not cfg.get('_attemptId') or not parent.get('_swarmParentAttemptId')):
        return False
    accepted = get_attempt(cfg['_attemptId'], user_id=user_id, include_execution_config=True)
    if (accepted.get('attemptId') != cfg['_attemptId']
            or accepted.get('conversationId') != task['convId']
            or accepted.get('turnId') != cfg['_turnId']
            or accepted.get('taskId') != task.get('id')
            or accepted.get('operation') not in ('continue', 'checkpoint_resume')
            or accepted.get('operation') != cfg['_turnOperation']
            or accepted.get('status') not in statuses):
        return False
    receipt = (accepted.get('config') or {}).get('_resumeSource')
    if receipt is None or restart_source_attempt(sess, user_id=user_id, receipt=receipt) is None:
        return False
    current = get_turn(task['convId'], cfg['_turnId'], user_id=user_id)
    if (current.get('turnId') != cfg['_turnId']
            or current.get('laneId') != 'main'
            or current.get('currentAttemptId') != cfg['_attemptId']
            or current.get('status') not in statuses
            or not isinstance(current.get('ordinal'), int)
            or isinstance(current.get('ordinal'), bool)):
        return False
    if allow_parent_completion and accepted.get('status') == current.get('status') == 'completed':
        # Natural parent completion detaches, rather than cancels, its children.
        # Same-Turn replacement was fenced above; a new ordinary Turn is not Stop.
        return True
    page = read_recovery_turns(task['convId'], user_id=user_id, lane_id='main',
                      after_ordinal=current['ordinal'], limit=1)
    return isinstance(page.get('turns'), list) and not page['turns']


def checkpoint_recovery_error(agent: dict) -> str:
    """Refuse unresolved tools; an absent receipt is never proof of rollback."""
    result = agent.get('result') or {}
    if result.get('checkpoint_phase') == 'tools_in_flight':
        return 'restart_tool_outcome_unknown'
    messages = agent.get('messages') or []
    if not isinstance(messages, list) or (not messages and agent.get('rounds_used')):
        return 'restart_checkpoint_invalid'
    pending = set()
    for message in messages:
        if not isinstance(message, dict):
            return 'restart_checkpoint_invalid'
        if message.get('role') == 'assistant':
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list) or pending:
                return 'restart_checkpoint_invalid'
            seen = set()
            for call in calls:
                call_id = call.get('id') if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    return 'restart_checkpoint_invalid'
                seen.add(call_id)
                pending.add(call_id)
        elif message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            if not isinstance(call_id, str) or call_id not in pending:
                return 'restart_checkpoint_invalid'
            pending.remove(call_id)
    return 'restart_tool_outcome_unknown' if pending else ''
