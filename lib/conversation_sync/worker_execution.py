"""Reconstruct claimed conversation work through the ordinary task executor.

Entry: execute_claimed_attempt, a synchronous durable-worker handler. Turn and
task-result authorities retain settlement; the shared invocation owns model,
tool, child and account lifecycles. Pending attempts enter once before any
preparation effects. Running attempts require domain recovery evidence and
are never blindly replayed after lease takeover. conversation_registration joins
the common durable service; admission remains the command service's concern.
"""
from collections.abc import Mapping

from lib.durable_worker import (
    WorkerDomainSettlementUnknown, WorkerExecutionContext, WorkerJobOutcome,
    WorkerProtocolError,
)
from lib.log import get_logger

logger = get_logger(__name__)


def _accepted(context, payload):
    from lib.turn_lifecycle import get_attempt, get_turn
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('conversation execution requires WorkerExecutionContext')
    if not isinstance(payload, Mapping) or dict(payload) != dict(context.claim.payload):
        raise WorkerProtocolError('conversation payload differs from the owned claim')
    if (context.claim.task_kind != 'conversation-attempt'
            or payload.get('contract') != 'tofu.conversation-attempt-job/v1'):
        raise WorkerProtocolError('unsupported conversation worker contract')
    owner = context.principal.require_owner(context='conversation worker execution')
    attempt = get_attempt(payload.get('attemptId'), user_id=owner, include_execution_config=True)
    if (not isinstance(attempt, Mapping)
            or attempt.get('taskId') != context.claim.task_id
            or context.claim.task_id != f"conversation-attempt:{attempt.get('attemptId')}"
            or any(attempt.get(key) != payload.get(key) for key in (
                'attemptId', 'turnId', 'conversationId', 'operation', 'baseProjectionRevision'))):
        raise WorkerProtocolError('conversation worker binding differs from accepted authority')
    turn = get_turn(attempt['conversationId'], attempt['turnId'], user_id=owner)
    if not isinstance(turn, Mapping):
        raise WorkerProtocolError('conversation worker Turn disappeared')
    return owner, attempt, turn


def _settled_outcome(context, attempt, *, task=None):
    """A terminal Turn alone does not discharge task/account persistence debt."""
    context.checkpoint(allow_cancelled=True)
    status = attempt.get('status')
    if status not in ('completed', 'truncated', 'failed', 'interrupted'):
        raise WorkerDomainSettlementUnknown('Conversation invocation has no terminal Turn receipt')
    if task is not None and task.get('_terminalPersistencePending'):
        raise WorkerDomainSettlementUnknown('Conversation terminal persistence is still pending')
    if attempt.get('startedAt') is None:
        from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_CONFIG_KEY
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        if (status not in ('failed', 'interrupted')
                or WORKER_EXECUTION_CONFIG_KEY in (attempt.get('config') or {})
                or task is not None or chat_task_runtime.get(context.claim.task_id) is not None):
            raise WorkerDomainSettlementUnknown('Unentered conversation has inconsistent execution evidence')
        # No executor or effect boundary was entered. Its terminal Turn is the
        # complete domain receipt; inventing a task result/exit witness would
        # falsely claim a physical invocation existed.
        return WorkerJobOutcome(status='cancelled' if status == 'interrupted' else 'failed',
            result_ref=context.claim.task_id, replay_cursor=context.replay_cursor,
            error={} if status == 'interrupted' else {'code': 'task_start_failed', 'retryable': False})
    from lib.storage import get_storage_client
    result = get_storage_client().query('task_results.replay_get', {
        'key': context.claim.task_id,
        'user_id': context.principal.require_owner(context='conversation worker settlement'),
        'include_metadata': True,
    })
    expected = {'completed': ('done',), 'truncated': ('done',),
                'failed': ('error', 'done'), 'interrupted': ('aborted', 'done', 'interrupted')}
    metadata = (result.get('metadata') or {}) if isinstance(result, Mapping) else {}
    if (not isinstance(result, Mapping) or not isinstance(metadata, Mapping)
            or result.get('status') not in expected[status]
            or metadata.get('attemptId') != attempt['attemptId']
            or metadata.get('turnId') != attempt['turnId']):
        raise WorkerDomainSettlementUnknown('Conversation task result has no matching terminal receipt')
    from lib.conversation_sync.worker_recovery import has_exit_receipt, record_joined_exit
    if not has_exit_receipt(attempt, metadata):
        from lib.turn_lifecycle import get_turn
        turn = get_turn(attempt['conversationId'], attempt['turnId'],
                        user_id=context.principal.owner_user_id)
        record_joined_exit(context, attempt, turn, task)
    if task is not None:
        # The durable receipt now holds the exit evidence this carrier retained.
        task.pop('_exitReceiptPending', None)
    from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_EXIT_KEY
    unknown = (metadata.get(WORKER_EXECUTION_EXIT_KEY) or {}).get('settlement') == 'unknown'
    return WorkerJobOutcome(
        status=('cancelled' if context.cancellation_requested else 'failed' if unknown else
                'succeeded' if status in ('completed', 'truncated') else
                'cancelled' if context.cancellation_requested else 'failed'),
        result_ref=f"conversation-attempt:{attempt['attemptId']}",
        error=({'code': 'worker_effect_outcome_unknown', 'retryable': False} if unknown else {}),
        replay_cursor=context.replay_cursor,
    )


def execute_claimed_attempt(context, payload, *, application=None):
    """Run one accepted invocation in the claim owner's physical thread.

    A transport replay may reuse a settled result only with the known carrier's
    physical-exit evidence. An entered attempt is an unknown effect frontier
    until the existing domain recovery owner proves it; neither an expired
    lease nor missing process-local task proves that a call is safe to repeat.
    Detached Swarm work the attempt spawned keeps this claim after its Turn
    settles: the job releases only once that work has ended, outside the
    attempt's dispatch lock (``swarm/turn_handoff.own_detached_swarm``).
    """
    outcome, resume_detached = _run_claimed_attempt(context, payload, application=application)
    from lib.swarm.turn_handoff import own_detached_swarm
    own_detached_swarm(context, resume=resume_detached)
    return outcome


def _run_claimed_attempt(context, payload, *, application=None):
    """Return the settled outcome and whether takeover should resume detached work."""
    from lib.turn_lifecycle import attempt_dispatch_lock, get_attempt, mark_task_started
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('conversation execution requires WorkerExecutionContext')
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    context.checkpoint(allow_cancelled=True)
    with attempt_dispatch_lock(context.claim.task_id):
        owner, attempt, turn = _accepted(context, payload)
        if attempt['status'] != 'pending':
            if attempt.get('startedAt') is None:
                return _settled_outcome(context, attempt), False
            from lib.conversation_sync.worker_recovery import recover_exited_attempt
            task = chat_task_runtime.get(context.claim.task_id)
            attempt = recover_exited_attempt(context, attempt, turn, task=task)
            outcome = _settled_outcome(context, attempt, task=task)
            if not context.cancellation_requested:
                from lib.goal_runs.worker_recovery import resume_after_worker_exit
                resume_after_worker_exit(context, attempt, application=application)
            return outcome, not context.cancellation_requested
        if turn.get('currentAttemptId') != attempt['attemptId']:
            raise WorkerProtocolError('conversation attempt was superseded before execution')
        from lib.conversation_sync.command_service import restore_accepted_attempt_config
        from lib.conversation_sync.execution_identity import restore_execution_identity
        from lib.tasks_pkg.worker_binding import worker_task_identity
        config = restore_accepted_attempt_config(turn, attempt, user_id=owner)
        principal, _attended = restore_execution_identity(config)
        task_id = worker_task_identity(context, conversation_id=turn['conversationId'],
            attempt_id=attempt['attemptId'], turn_id=turn['turnId'], principal=principal)
        if chat_task_runtime.get(task_id) is not None:
            raise WorkerDomainSettlementUnknown('Conversation already has a physical task carrier')
        from lib.project_dispatch.admission import KEY, TrustedDispatch
        if KEY in config:
            # Recovery can enqueue before the accepting coordinator finishes
            # binding. Its captured grant authorizes the same idempotent
            # association; binding still precedes preparation and task birth.
            TrustedDispatch(config[KEY]).continue_execution(turn['turnId'], attempt['attemptId'])

        # Commit entry BEFORE translation, prompt hooks, workspace preparation
        # or any other potentially external effect in the common start path.
        from lib.process_owner import process_owner
        process = process_owner()
        started = mark_task_started(attempt['attemptId'], task_id, user_id=owner,
            worker_claim=context.publication_claim(), worker_process=process)
        if not started or started.get('status') != 'running':
            raise WorkerProtocolError('conversation worker entry was refused')
        from lib.storage.worker_execution import execution_witness, WORKER_EXECUTION_CONFIG_KEY
        entered = get_attempt(attempt['attemptId'], user_id=owner, include_execution_config=True)
        if ((entered or {}).get('config') or {}).get(WORKER_EXECUTION_CONFIG_KEY) != execution_witness(
                process, context.publication_claim()):
            raise WorkerProtocolError('conversation worker process entry was not acknowledged')
        registered_task = None

        def registered(identity):
            nonlocal registered_task
            if identity != task_id:
                raise WorkerProtocolError('conversation executor changed the claimed task identity')
            registered_task = chat_task_runtime.get(identity)
            if registered_task is None:
                raise WorkerProtocolError('conversation executor did not retain its task carrier')

        from lib.conversation_sync.dispatch_contract import ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY
        from lib.conversation_sync.task_start import start_conversation_attempt_executor
        accepted_at = (attempt.get('config') or {}).get(
            ATTEMPT_DISPATCH_REQUEST_STARTED_AT_MS_CONFIG_KEY, attempt['createdAt'])
        abort_after_ts = float(accepted_at) / 1000
        if config.get('_goalRecoverySourceRunId') and config.get('_goalRecoveryAbortAfterTs'):
            abort_after_ts = min(abort_after_ts, float(config['_goalRecoveryAbortAfterTs']))
        try:
            identity, reason = start_conversation_attempt_executor(
                turn['conversationId'], config, abort_after_ts=abort_after_ts,
                on_task_registered=registered, worker_context=context,
                **({'application': application} if application is not None else {}))
            if identity != task_id or reason is not None:
                raise WorkerDomainSettlementUnknown('Conversation executor startup did not settle')
        except BaseException as error:
            from lib.conversation_sync.worker_recovery import retain_preparation_exit
            retain_preparation_exit(context, entered, turn, error=error, config=config)
            raise
        settled = get_attempt(attempt['attemptId'], user_id=owner, include_execution_config=True)
        return _settled_outcome(context, settled, task=registered_task), False


def _refused_receipt_outcome(context, refusal, *, status, task):
    """Settle the job whose exit receipt the authority refuses on every claim.

    The receipt follows the terminal Turn, so only the receipt is missing.
    Reporting the refusal as unknown would lapse the lease and re-claim the
    job every lease forever. The terminal job keeps the refusal as its durable
    reason; overall settlement stays unknown and nothing is replayed.
    """
    logger.error('[ConversationWorker] exit receipt refused; job settles %s task=%s: %s',
                 status, context.claim.task_id, refusal)
    from lib.conversation_sync.worker_recovery import release_refused_exit_carrier
    release_refused_exit_carrier(context, task)
    return WorkerJobOutcome(status=status, result_ref=context.claim.task_id,
        error={'code': 'worker_exit_receipt_refused', 'retryable': False, 'detail': str(refusal)},
        replay_cursor=context.replay_cursor)


def settle_claimed_failure(context, payload, error):
    """The runner's failure port only reconciles entered work; it never reruns it."""
    from lib.turn_lifecycle import attempt_dispatch_lock
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.conversation_sync.worker_recovery import WorkerExitReceiptRefused, recover_exited_attempt
    context.checkpoint(allow_cancelled=True)
    with attempt_dispatch_lock(context.claim.task_id):
        owner, attempt, turn = _accepted(context, payload)
        if attempt.get('startedAt') is None:
            from lib import turn_lifecycle as turns
            from lib.conversation_sync.command_service import executor_start_error_envelope
            if attempt['status'] == 'pending':
                envelope = executor_start_error_envelope(error, attempt.get('config') or {})
                carrier = dict(turn.get('projection') or {})
                carrier.update(id=context.claim.task_id, convId=attempt['conversationId'],
                    _attemptId=attempt['attemptId'], _turnId=attempt['turnId'], _userId=owner,
                    _principalContext=context.principal.to_payload(), _conversationWorkerContext=context,
                    config=dict(attempt.get('config') or {}), status='error', error=envelope,
                    finishReason='error')
                turns.record_task_event(carrier, {'type': 'error', 'error': envelope})
                attempt = turns.get_attempt(attempt['attemptId'], user_id=owner,
                                            include_execution_config=True)
            return _settled_outcome(context, attempt)
        task = chat_task_runtime.get(context.claim.task_id)
        try:
            attempt = recover_exited_attempt(context, attempt, turn, task=task)
            return _settled_outcome(context, attempt, task=task)
        except WorkerExitReceiptRefused as refusal:
            return _refused_receipt_outcome(context, refusal, status='failed', task=task)


def conversation_registration(*, application=None):
    """Register the common invocation and its fenced domain recovery ports.

    Cross-authority entry, descendants, model/tool admission, Stop, physical
    exit and unknown settlement specifications live beside this adapter. An
    unprovable outcome remains unconfirmed, never a fresh invocation.
    """
    from lib.durable_worker import WorkerKindRegistration, WorkerKindSafety
    from functools import partial
    handler = partial(execute_claimed_attempt, application=application)
    return WorkerKindRegistration('conversation-attempt', handler,
        WorkerKindSafety(True, True, True, True), cancel_handler=cancel_claimed_attempt,
        failure_handler=settle_claimed_failure)


def cancel_claimed_attempt(context, payload):
    """Settle an unentered cancelled attempt without constructing an executor.

    The durable entry fence is the no-effects proof. Entered work must join and
    settle through its original task or the existing domain recovery owner.
    """
    from lib.turn_lifecycle import attempt_dispatch_lock, get_attempt, record_task_event
    from lib.tasks_pkg.worker_binding import worker_task_identity
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('conversation cancellation requires WorkerExecutionContext')
    context.checkpoint(allow_cancelled=True)
    if not context.cancellation_requested:
        raise WorkerProtocolError('conversation cancellation has no durable request')
    with attempt_dispatch_lock(context.claim.task_id):
        owner, attempt, turn = _accepted(context, payload)
        if attempt.get('startedAt') is not None:
            from lib.conversation_sync.worker_recovery import WorkerExitReceiptRefused, recover_exited_attempt
            task = chat_task_runtime.get(context.claim.task_id)
            try:
                attempt = recover_exited_attempt(context, attempt, turn, task=task)
                return _settled_outcome(context, attempt, task=task)
            except WorkerExitReceiptRefused as refusal:
                return _refused_receipt_outcome(context, refusal, status='cancelled', task=task)
        if attempt['status'] == 'pending':
            config = dict(attempt.get('config') or {}, _turnOwnerUserId=owner)
            # Stop drains accepted work; it never reauthorizes the grant that
            # may have been revoked since admission.
            principal = context.principal
            worker_task_identity(context, conversation_id=turn['conversationId'],
                attempt_id=attempt['attemptId'], turn_id=turn['turnId'],
                principal=principal, allow_cancelled=True)
            projection = turn.get('projection') or {}
            task = {key: projection[key] for key in (
                'content', 'thinking', 'toolRounds', 'segments', 'model', 'usage',
                'usageComplete') if key in projection}
            task.update(id=context.claim.task_id, convId=turn['conversationId'],
                _attemptId=attempt['attemptId'], _turnId=turn['turnId'], _userId=owner,
                _principalContext=principal.to_payload(), _conversationWorkerContext=context,
                config=config, status='aborted', aborted=True, finishReason='aborted')
            from lib.conversation_sync.worker_control import decode_cancellation
            reason, detail = decode_cancellation(context.cancellation_reason)
            if reason:
                task['_abort_reason'] = reason
                task['_abortReasonDetail'] = detail
            record_task_event(task, {'type': 'aborted'})
            attempt = get_attempt(attempt['attemptId'], user_id=owner)
        if attempt['status'] not in ('interrupted', 'failed') or attempt.get('startedAt') is not None:
            raise WorkerDomainSettlementUnknown('Unentered cancellation has no terminal receipt')
        return WorkerJobOutcome(status='cancelled', result_ref=context.claim.task_id,
                                replay_cursor=context.replay_cursor)
