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


class WorkerExitReceiptRefused(RuntimeError):
    """The authority answered the exit receipt with a refusal, not an unknown.

    Every claim rebuilds the same receipt from the same durable state, so a
    re-claim can only repeat the refusal. Transport loss, transient storage
    and conflicts stay unknown and are retried by the next claim.
    """


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
    from lib.storage.errors import StorageError, describe_storage_failure, is_transient_storage_error
    from lib.task_replay.task_result_checkpoint_contract import (
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD, TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
    )
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
    # These fields requested cache-settings publication on the original
    # checkpoint. An exit receipt changes metadata, not those already committed
    # settings; replaying the candidates would also overwrite newer cache facts.
    value.pop(TASK_RESULT_CACHE_PREFIX_HWM_FIELD, None)
    value.pop(TASK_RESULT_LAST_TURN_CACHE_READ_FIELD, None)
    try:
        result = get_storage_client(write=True).command('task_results.checkpoint', {
            'key': context.claim.task_id, 'value': value, 'expected_version': version,
            'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, 'require_parent': True,
            'worker_claim': context.publication_claim(terminal=True),
        }, 'conversation-worker-exit:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.retryable or is_transient_storage_error(error) or error.code == 'database_conflict':
            raise
        raise WorkerExitReceiptRefused(describe_storage_failure(error)) from error
    if not isinstance(result, Mapping) or result.get('guard_contract') != TASK_RESULT_CHECKPOINT_GUARD_CONTRACT:
        raise WorkerExitReceiptRefused('Conversation exit receipt acknowledgement is not a guarded checkpoint')
    if result.get('owned') is not True:
        # The guarded authority disowns a write whose parent conversation is no
        # longer active or whose record another owner or Attempt holds.
        raise WorkerExitReceiptRefused('Conversation exit receipt was disowned by its task result record')


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


_RECOVERY_RETENTION_KEYS = ('_exitReceiptPending', '_terminalTurnPending',
                            '_terminalPersistencePending', '_terminalPersistenceRetryReady')


def _release_superseded_carrier(task, task_context):
    """Fence a claim a newer fencing token replaced and drop what it retained.

    Storage refuses every write under that claim, so its retained terminal
    debt and pending events can never commit; carrier eviction never frees an
    event lane. Marking the claim lost stops every holder still borrowing it.
    """
    task_context.mark_authority_lost()
    lane = task.get('_eventDeliveryLane')
    if lane is not None:
        from lib.tasks_pkg.event_delivery import coordinator
        coordinator().revoke(lane)
    for key in _RECOVERY_RETENTION_KEYS:
        task.pop(key, None)


def release_refused_exit_carrier(context, task):
    """Release a joined carrier's recovery retention once a refusal settles its job.

    The failure/Stop port settles a refused receipt's job once, so no later
    claim records this exit. The carrier stays registered, for a job completion
    the runner may still have to retry, but becomes ordinarily reclaimable; a
    claim the recovering claim replaced is fenced first.
    """
    if task is None:
        return
    task_claim = getattr(task.get('_conversationWorkerContext'), 'claim', None)
    if task_claim is not None and (task_claim.worker_id, task_claim.fencing_token) != (
            context.claim.worker_id, context.fencing_token):
        _release_superseded_carrier(task, task['_conversationWorkerContext'])
    for key in _RECOVERY_RETENTION_KEYS:
        task.pop(key, None)


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
    # Storage refuses every write made under a replaced claim, so nothing the
    # original carrier still retains can commit once a newer claim recovers it.
    claim_replaced = bool(joined and witness['claim'] != {
        'worker_id': context.claim.worker_id, 'fencing_token': context.fencing_token})
    fenced_debt = bool(claim_replaced and not preparation_exited
                       and task.get('_terminalPersistencePending'))
    if (joined and task.get('_terminalPersistencePending')
            and not preparation_exited and not fenced_debt):
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
        # Stopping the service ends every claim it ran: that is a restart, not
        # a lost lease. Otherwise the original claim's authority ended, or its
        # replacement fenced the result the carrier had not yet saved.
        stopped_by_shutdown = bool(joined and not preparation_exited
                                   and task_context.shutdown_event is not None
                                   and task_context.shutdown_event.is_set())
        lease_lost = bool(joined and not preparation_exited and not stopped_by_shutdown
                          and (fenced_debt or task_context.authority_lost))
        error = make_envelope('storage_fence' if lease_lost else 'internal',
            context='conversation-worker-recovery',
            source='conversation-worker', retryable=False,
            message=("Execution's worker lease ended before its result was saved; "
                     'external outcome is unknown / 执行租约在结果保存前已结束，外部效果尚未确认'
                     if lease_lost else
                     'Execution ended with an unknown external outcome / 执行已结束，外部效果尚未确认'),
            detail=(f"Unsaved terminal result: status={task.get('status')} "
                    f"finishReason={task.get('finishReason')}" if fenced_debt else ''),
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
        if lease_lost:
            # Only the joined original carrier proves this cause; reclaiming
            # a lease alone does not establish why the previous worker exited.
            carrier['_abort_reason'] = 'worker_lease_lost'
        if (proof == 'process_dead' or stopped_by_shutdown) and not context.cancellation_requested:
            # A dead or stopped execution owner is an interruption, not a human
            # Stop or a failed invocation. Domain checkpoint recovery may continue
            # in a new accepted Attempt; this receipt never authorizes replay.
            carrier.update(status='aborted', finishReason='interrupted',
                           _abort_reason='server_shutdown')
            event_type = 'aborted'
        turns.record_task_event(carrier, {'type': event_type, 'error': error})
        attempt = turns.get_attempt(attempt['attemptId'], user_id=context.principal.owner_user_id,
                                    include_execution_config=True)
        turn = turns.get_turn(attempt['conversationId'], attempt['turnId'],
                              user_id=context.principal.owner_user_id)
    # Fenced debt means a required terminal write never acknowledged.
    unknown = not known_terminal or proof == 'process_dead' or fenced_debt
    _save_exit(context, attempt, turn, proof=proof, unknown=unknown,
               known_terminal=known_terminal)
    if joined:
        task['_conversationWorkerExit'] = _receipt(attempt, proof, unknown=unknown)
    if claim_replaced:
        # The recorded receipt settles the attempt, so no later claim needs
        # this carrier as exit evidence. Retire it: readers and retries then
        # reach only the durable settlement, never the stale producer's state.
        _release_superseded_carrier(task, task_context)
        from lib.tasks_pkg.manager import discard_task
        discard_task(context.claim.task_id, attempt['conversationId'])
    if preparation_exited:
        task.pop('_terminalPersistencePending', None)
        task.pop('_terminalPersistenceRetryReady', None)
        task.update(status='error', finishReason='error',
                    error=(turn.get('settlement') or {}).get('error'),
                    _executionActualsComplete=False)
    return attempt
