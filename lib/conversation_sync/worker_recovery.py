"""Physical-exit evidence on the existing conversation result authority.

Entry points: record_joined_exit and recover_exited_attempt. The immutable
Attempt entry identifies its original process. Existing TaskRuntime observation
or process_owner proves physical exit; task_results retains one bounded receipt.
Unknown external effects retain domain accounting debt and never grant replay.
"""
from collections.abc import Mapping
import hashlib
import json
import time
import uuid

from lib.durable_worker import WorkerDomainSettlementUnknown, WorkerProtocolError
from lib.storage.generated_worker_job_contract import (
    WORKER_EXECUTION_CONFIG_KEY, WORKER_EXECUTION_EXIT_FORMAT,
    WORKER_EXECUTION_EXIT_KEY, WORKER_EXECUTION_EXIT_PROOFS,
    WORKER_EXECUTION_EXIT_SETTLEMENTS,
)


def _entry(attempt):
    from lib.storage.worker_execution import execution_witness
    witness = (attempt.get('config') or {}).get(WORKER_EXECUTION_CONFIG_KEY)
    if not isinstance(witness, Mapping):
        raise WorkerDomainSettlementUnknown('Entered conversation requires process recovery evidence')
    if witness != execution_witness(witness.get('process'), witness.get('claim')):
        raise WorkerProtocolError('Conversation entry evidence is malformed')
    return witness


def _receipt(attempt, proof, *, unknown=False):
    witness = _entry(attempt)
    encoded = json.dumps(witness, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    return {'format': WORKER_EXECUTION_EXIT_FORMAT,
            'entryDigest': hashlib.sha256(encoded).hexdigest(), 'proof': proof,
            'settlement': 'unknown' if unknown else 'confirmed'}


def has_exit_receipt(attempt, metadata):
    receipt = metadata.get(WORKER_EXECUTION_EXIT_KEY) if isinstance(metadata, Mapping) else None
    return (isinstance(receipt, Mapping) and receipt.get('proof') in WORKER_EXECUTION_EXIT_PROOFS
            and receipt.get('settlement') in WORKER_EXECUTION_EXIT_SETTLEMENTS
            and (receipt['proof'] == 'joined' or receipt['settlement'] == 'unknown')
            and receipt == _receipt(attempt, receipt['proof'],
                                    unknown=receipt['settlement'] == 'unknown'))


def _result_record(context, attempt):
    """Read one recovery snapshot; validate ownership before exposing its value."""
    from lib.storage import get_storage_client
    record = get_storage_client().query('record.get', {
        'namespace': 'task_results', 'key': context.claim.task_id})
    if record is None:
        return {}, {}, 0
    value = record['value']
    if (not isinstance(value, Mapping)
            or str(value.get('user_id')) != str(context.principal.owner_user_id)
            or value.get('task_id') != context.claim.task_id
            or value.get('conv_id') != attempt['conversationId']):
        raise WorkerProtocolError('Conversation recovery result ownership differs')
    metadata = value.get('metadata') or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if (not isinstance(metadata, Mapping)
            or any(metadata.get(key, attempt[key]) != attempt[key]
                   for key in ('attemptId', 'turnId'))):
        raise WorkerProtocolError('Conversation recovery result identity differs')
    return dict(value), dict(metadata), record['version']


def _save_exit(context, attempt, turn, *, proof, unknown, known_terminal=False):
    from lib.storage import get_storage_client
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    context.checkpoint(allow_cancelled=True)
    value, metadata, version = _result_record(context, attempt)
    if has_exit_receipt(attempt, metadata):
        return
    now_ms = time.time_ns() // 1_000_000
    projection = turn.get('projection') or {}
    for key in ('content', 'thinking'):
        value.setdefault(key, projection.get(key) or '')
    value.update(task_id=context.claim.task_id, conv_id=attempt['conversationId'],
                 user_id=context.principal.require_owner(context='conversation exit receipt'))
    value.setdefault('created_at', attempt['createdAt'])
    if unknown:
        value['status'] = {'completed': 'done', 'truncated': 'done',
                           'interrupted': 'aborted', 'failed': 'error'}[attempt['status']]
        value['completed_at'] = now_ms
        if not known_terminal:
            metadata['usageComplete'] = False
        error = (turn.get('settlement') or {}).get('error')
        if error is not None:
            value['error'] = json.dumps(error, ensure_ascii=False)
    metadata.update(attemptId=attempt['attemptId'], turnId=attempt['turnId'])
    metadata[WORKER_EXECUTION_EXIT_KEY] = _receipt(attempt, proof, unknown=unknown)
    value['metadata'] = json.dumps(metadata, ensure_ascii=False)
    result = get_storage_client(write=True).command('task_results.checkpoint', {
        'key': context.claim.task_id, 'value': value, 'expected_version': version,
        'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, 'require_parent': True,
        'worker_claim': context.publication_claim(terminal=True),
    }, 'conversation-worker-exit:' + uuid.uuid4().hex)
    if (not isinstance(result, Mapping) or result.get('owned') is not True
            or result.get('guard_contract') != TASK_RESULT_CHECKPOINT_GUARD_CONTRACT):
        raise WorkerDomainSettlementUnknown('Conversation exit receipt was not acknowledged')


def record_joined_exit(context, attempt, turn, task):
    """Called only after the common invocation, cleanup and task persistence exit."""
    if (task is None or task.get('_activeWorkerCount') != 0
            or task.get('_terminalPersistencePending')):
        raise WorkerDomainSettlementUnknown('Conversation physical exit or persistence remains pending')
    receipt = _receipt(attempt, 'joined')
    task['_conversationWorkerExit'] = receipt
    _save_exit(context, attempt, turn, proof='joined', unknown=False)


def retain_preparation_exit(context, attempt, turn, *, error=None, config=None):
    """Retain an unwound preparer on the existing task owner until its receipt commits.

    This is called only from the entered synchronous start exception boundary.
    A task that already entered a worker retains its ordinary physical counter
    and persistence debt; preparation evidence cannot override either.
    """
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    task = chat_task_runtime.get(context.claim.task_id)
    if task is not None and '_activeWorkerCount' in task:
        return
    if task is None:
        task = chat_task_runtime.create(principal=context.principal, task_id=context.claim.task_id)
    if (task.get('_userId') != context.principal.owner_user_id
            or task.get('_conversationWorkerContext') not in (None, context)):
        raise WorkerProtocolError('Preparation carrier belongs to another execution')
    task.update(convId=attempt['conversationId'], _attemptId=attempt['attemptId'],
        _turnId=attempt['turnId'], config=dict(attempt.get('config') or {}),
        _conversationWorkerContext=context)
    task['_conversationPreparationExit'] = _receipt(attempt, 'joined', unknown=True)
    if error is not None:
        from lib.conversation_sync.command_service import executor_start_error_envelope
        task['_conversationPreparationError'] = executor_start_error_envelope(
            error, config or attempt.get('config') or {})
    task['_terminalPersistencePending'] = True
    task['status'] = 'error'


def recover_exited_attempt(context, attempt, turn, *, task=None):
    """Converge an exited worker without invoking any model, tool or preparer."""
    from lib.process_owner import owner_state, process_owner
    from lib import turn_lifecycle as turns
    witness = _entry(attempt)
    _value, metadata, _version = _result_record(context, attempt)
    if has_exit_receipt(attempt, metadata):
        return attempt
    task_context = task.get('_conversationWorkerContext') if task is not None else None
    task_claim = getattr(task_context, 'claim', None)
    preparation_exited = (task is not None
                          and task.get('_conversationPreparationExit') == _receipt(
                              attempt, 'joined', unknown=True))
    joined = (task is not None and (task.get('_activeWorkerCount') == 0 or preparation_exited)
              and task.get('id') == context.claim.task_id
              and task.get('_userId') == context.principal.owner_user_id
              and witness['claim'] == {
                  'worker_id': getattr(task_claim, 'worker_id', None),
                  'fencing_token': getattr(task_claim, 'fencing_token', None)}
              and witness['process'] in (None, process_owner()))
    if joined and task.get('_terminalPersistencePending') and not preparation_exited:
        raise WorkerDomainSettlementUnknown('Conversation terminal persistence is still pending')
    if joined:
        proof = 'joined'
    elif owner_state(witness.get('process')) == 'dead':
        proof = 'process_dead'
    else:
        raise WorkerDomainSettlementUnknown('Entered conversation requires physical-exit recovery evidence')
    known_terminal = (attempt['status'] not in ('pending', 'running')
                      and _value.get('status') in ('done', 'error', 'aborted', 'interrupted')
                      and metadata.get('attemptId') == attempt['attemptId']
                      and metadata.get('turnId') == attempt['turnId'])
    if attempt['status'] in ('pending', 'running'):
        from lib.error_envelope import make_envelope
        error = make_envelope('internal', context='conversation-worker-recovery',
            source='conversation-worker', retryable=False,
            message='Execution ended with an unknown external outcome / 执行已结束，外部效果尚未确认',
            hint='Inspect saved tool and provider receipts before continuing / 请先核对已保存的工具与服务回执',
            extensions={'code': 'worker_effect_outcome_unknown',
                        'retryPolicy': {'mode': 'manual', 'automatic': False}})
        if preparation_exited and isinstance(task.get('_conversationPreparationError'), dict):
            # Preserve the observed startup cause. It does not prove that
            # earlier preparation/translation had no external effects.
            error = dict(task['_conversationPreparationError'],
                         retryable=False, code='worker_effect_outcome_unknown',
                         retryPolicy={'mode': 'manual', 'automatic': False})
            error['hint'] = str(error.get('hint') or '') + (
                '\nInspect saved preparation/provider receipts before retrying / '
                '重试前请核对已保存的准备阶段与服务商回执')
        carrier = dict(turn.get('projection') or {})
        carrier.update(id=context.claim.task_id, convId=attempt['conversationId'],
            _attemptId=attempt['attemptId'], _turnId=attempt['turnId'],
            _userId=context.principal.require_owner(context='conversation recovery'),
            _principalContext=context.principal.to_payload(), _conversationWorkerContext=context,
            config=dict(attempt.get('config') or {}), status='error', error=error,
            finishReason='error', _executionActualsComplete=False)
        event_type = 'error'
        if proof == 'process_dead' and not context.cancellation_requested:
            # A dead execution owner is an interruption, not a human Stop or
            # a failed invocation. Domain checkpoint recovery may continue in
            # a new accepted Attempt; this receipt never authorizes replay.
            carrier.update(status='aborted', finishReason='interrupted',
                           _abort_reason='server_shutdown')
            event_type = 'aborted'
        turns.record_task_event(carrier, {'type': event_type, 'error': error})
        attempt = turns.get_attempt(attempt['attemptId'], user_id=context.principal.owner_user_id,
                                    include_execution_config=True)
        turn = turns.get_turn(attempt['conversationId'], attempt['turnId'],
                              user_id=context.principal.owner_user_id)
    unknown = not known_terminal or proof == 'process_dead'
    _save_exit(context, attempt, turn, proof=proof, unknown=unknown,
               known_terminal=known_terminal)
    if joined:
        task['_conversationWorkerExit'] = _receipt(attempt, proof, unknown=unknown)
    if preparation_exited:
        task.pop('_terminalPersistencePending', None)
        task.pop('_terminalPersistenceRetryReady', None)
        task.update(status='error', finishReason='error',
                    error=(turn.get('settlement') or {}).get('error'),
                    _executionActualsComplete=False)
    return attempt
