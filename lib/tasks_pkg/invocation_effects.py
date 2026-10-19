"""Exact child tool receipts in the existing registered task-result authority.

Entry: persist_child_effect and inspect_child_effect. Birth identity authorizes
the record; guarded checkpoint CAS preserves parent lifetime and concurrent
updates. No second journal, effect replay, or child Turn is introduced. Receipts
retain only producer coordinates and domain references, never tool arguments.
"""
import copy
import json

from lib.identity import require_user_id
from lib.tasks_pkg.invocation_registration import INVOCATION_COMPLETION_FIELD, owned_child_record


def durable_child(task):
    from lib.tasks_pkg.invocation import is_child_invocation
    return is_child_invocation(task) and not task.get('_invocationStorageFree')


def _rows(value):
    rows = value.get('tool_rounds') or '[]'
    rows = json.loads(rows) if isinstance(rows, str) else rows
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('Child tool receipts are malformed')
    return rows


def persist_child_effect(task, row):
    """Commit the exact frozen occurrence before handler entry/publication.

    Serializes this invocation's parallel tools, not unrelated invocations.
    The existing checkpoint operation detects identical lost-ACK replays and
    refuses conflicting versions. Never reload/rebase a failed write candidate.
    """
    from lib.storage import get_storage_client
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    from lib.conversation_sync.validation import decode
    from lib.tasks_pkg.executor._effects import ToolEffectDurabilityError
    owner = require_user_id(task.get('_userId'), context='child effect receipt')
    lock = task.get('_invocationEffectLock')
    if (lock is None or not durable_child(task) or not task.get('_archiveScope')
            or (row.get('executionEffect') or {}).get('scope') != 'invocation'):
        raise ToolEffectDurabilityError('Child effect requires acknowledged invocation registration')
    receipt = {key: copy.deepcopy(row[key]) for key in (
        'taskId', 'toolName', 'toolCallId', 'roundNum', 'executionEffect') if key in row}
    receipt['taskId'] = task['id']
    decode('ToolExecutionEffect', receipt['executionEffect'])
    operation = receipt['executionEffect']['operationId']
    with lock:
        client = get_storage_client(write=True)
        record = wait_for_persistence(task, lambda: owned_child_record(client, user_id=owner,
            conversation_id=task['convId'], invocation_id=task['id']), label='child tool receipt lookup')
        value = copy.deepcopy(record['value'])
        if INVOCATION_COMPLETION_FIELD in value:
            raise ToolEffectDurabilityError('Completed child cannot acquire new tool effects')
        if (value['child_invocation'].get('parent_task_id') != task.get('_parentTaskId')
                or value['child_invocation']['archive_scope'] != task['_archiveScope']):
            raise ToolEffectDurabilityError('Child producer identity changed')
        rows = _rows(value)
        matches = [index for index, previous in enumerate(rows)
                   if (previous.get('executionEffect') or {}).get('operationId') == operation]
        if len(matches) > 1:
            raise ToolEffectDurabilityError('Child occurrence identity is ambiguous')
        if matches:
            previous = rows[matches[0]]
            old = previous['executionEffect']
            for field in ('taskId', 'toolName', 'toolCallId', 'roundNum'):
                if previous.get(field) != receipt.get(field):
                    raise ToolEffectDurabilityError('Child occurrence producer changed')
            if (old.get('state') != 'entered'
                    or any(old.get(key) != receipt['executionEffect'].get(key)
                           for key in old if key not in ('state',))):
                raise ToolEffectDurabilityError('Child occurrence evidence changed')
            rows[matches[0]] = receipt
        else:
            rows.append(receipt)
        value['tool_rounds'] = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
        payload = dict(key=task['id'], value=value, expected_version=record['version'],
                       guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, require_parent=True)
        result = wait_for_persistence(task, lambda: client.command(
            'task_results.checkpoint', payload, None), label='child tool effect receipt')
        if (not isinstance(result, dict) or result.get('owned') is not True
                or result.get('guard_contract') != TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
                or type(result.get('version')) is not int or result['version'] < record['version']):
            raise ToolEffectDurabilityError('Child tool receipt was not durably acknowledged')


def inspect_child_effect(*, user_id, conversation_id, invocation_id, operation_id):
    """Read exact owned evidence; this grants no takeover or replay authority."""
    from lib.storage import get_storage_client
    from lib.conversation_sync.tool_effects import inspect_effect_rows
    from lib.conversation_sync.validation import decode
    user_id = require_user_id(user_id, context='child effect inspection')
    record = owned_child_record(get_storage_client(), user_id=user_id,
        conversation_id=conversation_id, invocation_id=invocation_id)
    outcome, reason = inspect_effect_rows(_rows(record['value']),
        conversation_id=conversation_id, operation_id=operation_id,
        expected_scope='invocation', expected_task_id=invocation_id)
    return decode('InvocationToolEffectInspection', {
        'ok': True, 'contract': 'tofu.invocation.tool-effect-inspection/v1',
        'conversationId': conversation_id, 'invocationId': invocation_id,
        'operationId': operation_id, 'outcome': outcome, 'reason': reason})


def inspect_child_effect_batch(*, storage, user_id, conversation_id, invocation_id,
        registration_version, occurrences, expected_parent_task_id=None,
        checkpoint_batch_unentered=False):
    """Read one exact version and bind each operation to its original call/round.

    One owned record supplies the whole batch. Recheck its version after domain
    reads so concurrent completion or receipt changes cannot produce mixed proof.
    Only the accepted native recovery owner may supply an uncharged checkpoint
    batch after writer retirement and whole-account adoption. Exact absence of
    its intent then proves no handler entry; any contradictory receipt refuses.
    """
    from lib.conversation_sync.tool_effects import inspect_effect_rows
    from lib.conversation_sync.validation import decode
    user_id = require_user_id(user_id, context='child effect batch inspection')
    if (type(checkpoint_batch_unentered) is not bool
            or checkpoint_batch_unentered and not expected_parent_task_id):
        raise ValueError('Unentered checkpoint inspection requires its original parent')
    def record():
        return owned_child_record(storage, user_id=user_id,
            conversation_id=conversation_id, invocation_id=invocation_id)
    before = record()
    if (type(before.get('version')) is not int or before['version'] < 1
            or (registration_version is None and not expected_parent_task_id)
            or (registration_version is not None and before['version'] != registration_version)
            or (expected_parent_task_id is not None
                and before['value']['child_invocation']['parent_task_id'] != expected_parent_task_id)
            or INVOCATION_COMPLETION_FIELD in before['value']):
        raise ValueError('Child effect registration changed during inspection')
    rows = _rows(before['value'])
    by_operation = {}
    for row in rows:
        operation = (row.get('executionEffect') or {}).get('operationId')
        by_operation.setdefault(operation, []).append(row)
    results = []
    for call_id, operation_id, round_num in occurrences:
        matches = by_operation.get(operation_id, [])
        if checkpoint_batch_unentered:
            if matches:
                raise ValueError('Uncharged checkpoint batch has a contradictory entry receipt')
            result = decode('InvocationToolEffectInspection', dict(ok=True,
                contract='tofu.invocation.tool-effect-inspection/v1', conversationId=conversation_id,
                invocationId=invocation_id, operationId=operation_id, outcome='file_not_published',
                reason='checkpoint_batch_unentered'))
            results.append(dict(call_id=call_id, result=result))
            continue
        if (len(matches) != 1 or matches[0].get('taskId') != invocation_id
                or matches[0].get('toolCallId') != call_id
                or type(matches[0].get('roundNum')) is not int
                or matches[0].get('roundNum') != round_num):
            raise ValueError('Child file occurrence differs from the pending checkpoint')
        outcome, reason = inspect_effect_rows(matches, conversation_id=conversation_id,
            operation_id=operation_id, expected_scope='invocation', expected_task_id=invocation_id)
        result = decode('InvocationToolEffectInspection', dict(ok=True,
            contract='tofu.invocation.tool-effect-inspection/v1', conversationId=conversation_id,
            invocationId=invocation_id, operationId=operation_id, outcome=outcome, reason=reason))
        results.append(dict(call_id=call_id, result=result))
    if record() != before:
        raise ValueError('Child effect registration changed during inspection')
    return results
