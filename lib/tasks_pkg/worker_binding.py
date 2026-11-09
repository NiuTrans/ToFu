"""Bind a claimed conversation job to the existing invocation and Turn owners.

Entry points: worker_task_identity before registration, bind_conversation_worker
after registration, publication_arguments at domain writes, and
share/inherit_conversation_worker for parent carriers and children. Only an
explicitly injected WorkerExecutionContext grants a claim; configuration is
never a port.
The invocation control scope propagates cancellation to models/tools/children
and is joined by the physical worker. Storage still revalidates each write.
"""
from lib.durable_worker import WorkerExecutionContext
from lib.identity import PrincipalContext


def worker_task_identity(context, *, conversation_id, attempt_id, turn_id, principal,
                         allow_cancelled=False):
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('conversation worker requires WorkerExecutionContext')
    claim = context.claim
    payload = claim.payload
    if (claim.task_kind != 'conversation-attempt'
            or payload.get('contract') != 'tofu.conversation-attempt-job/v1'
            or not isinstance(attempt_id, str) or not attempt_id
            or claim.task_id != f'conversation-attempt:{attempt_id}'
            or payload.get('attemptId') != attempt_id
            or payload.get('turnId') != turn_id
            or payload.get('conversationId') != conversation_id):
        raise ValueError('conversation worker identity differs from accepted attempt')
    if (not isinstance(principal, PrincipalContext) or principal != claim.principal
            or payload.get('principal') != principal.to_payload()):
        raise PermissionError('conversation worker principal differs from accepted identity')
    context.checkpoint(allow_cancelled=allow_cancelled)
    return claim.task_id


def bind_conversation_worker(task, context):
    """Install one inherited control source without replacing another owner."""
    principal = PrincipalContext.from_payload(task['_principalContext'])
    if task.get('_userId') != principal.require_owner(context='conversation worker'):
        raise PermissionError('conversation worker task owner differs from principal')
    identity = worker_task_identity(context, conversation_id=task.get('convId'),
        attempt_id=task.get('_attemptId'), turn_id=task.get('_turnId'), principal=principal)
    if task.get('id') != identity:
        raise ValueError('conversation worker task identity differs from claim')
    if any(key in task for key in (
            '_conversationWorkerContext', '_invocationAbortCheck',
            '_executionModelDispatchAuthority', '_executionBudget')):
        raise ValueError('conversation worker cannot replace existing invocation control')
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope

    def aborted():
        # Losing execution authority is an interrupted invocation, not a human
        # Stop. The shared invocation owner retains that exception as its cause.
        context.checkpoint(allow_cancelled=True)
        if not context.cancellation_requested:
            return False
        from lib.conversation_sync.worker_control import decode_cancellation
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        reason, detail = decode_cancellation(context.cancellation_reason)
        chat_task_runtime.abort_owned(identity, user_id=task['_userId'], expected_task=task,
            source='durable_conversation_cancel', reason=reason or None, reason_detail=detail or None)
        return True

    task['_conversationWorkerContext'] = context
    # Recovery joins this carrier to prove physical exit; reclamation must
    # wait until a durable exit receipt records that evidence.
    task['_exitReceiptPending'] = True
    task['_invocationAbortCheck'] = ChildCancellationScope(aborted)
    task['_executionModelDispatchAuthority'] = lambda actor: admit_model_dispatch(actor, context)


def admit_model_dispatch(task, context):
    """Revalidate the committed root entry at the final shared model boundary.

    The entered Attempt is the durable possibility-of-effects witness. Each
    request borrows its current claim, including descendants, after all domain
    account observers. This admits no replay and owns no usage or cost ledger.
    """
    from lib.durable_worker import WorkerCancellationRequested, WorkerLeaseLost
    from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused
    from lib.storage.errors import StorageError
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    from lib.turn_lifecycle import mark_task_started
    if (task.get('_conversationWorkerContext') is not context
            or task.get('_userId') != context.principal.require_owner(context='model execution authority')
            or task.get('convId') != context.claim.payload['conversationId']):
        raise ModelAdmissionRefused('Model execution authority differs from its invocation')
    try:
        context.checkpoint()
        def admit():
            return mark_task_started(context.claim.payload['attemptId'], context.claim.task_id,
                user_id=task['_userId'], worker_claim=context.publication_claim())
        try:
            result = wait_for_persistence(task, admit, label='worker model admission', effect_free=True)
        except StorageError as error:
            if error.code == 'database_conflict':
                # Read the existing authoritative verdict only to distinguish
                # human Stop from lost execution ownership; this grants nothing.
                _observe_current_claim(task, context)
            raise
        if result is None and task.get('id') != context.claim.task_id:
            # A detached Swarm descendant outlives its settled root Turn while
            # the spawning attempt's job still holds this claim
            # (swarm/turn_handoff.hold_detached_swarm). The settled Attempt can
            # no longer acknowledge entry, so admit only on the current,
            # unstopped claim itself.
            _observe_current_claim(task, context)
            return
        if (not isinstance(result, dict) or result.get('status') != 'running'
                or result.get('taskId') != context.claim.task_id
                or result.get('attemptId') != context.claim.payload['attemptId']):
            raise ModelAdmissionRefused('Model execution entry was not acknowledged')
        context.checkpoint()
    except WorkerCancellationRequested as error:
        raise AbortedError(str(error)) from error
    except (WorkerLeaseLost, StorageError) as error:
        raise ModelAdmissionRefused('Model execution authority ended') from error


def _observe_current_claim(task, context):
    from lib.durable_worker import StorageWorkerJobStore
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    import time
    store = StorageWorkerJobStore()
    state = wait_for_persistence(task,
        lambda: store.claim_state(context.claim, now_ms=time.time_ns() // 1_000_000),
        label='worker claim proof', effect_free=True)
    context.observe_claim_state(state)
    context.checkpoint()


def share_conversation_worker(parent, carrier):
    """Lend the parent's root claim to a parent-shaped carrier of the same task.

    Swarm sessions and wave assignments build their own parent dicts. A birth
    under a durable attempt is refused without that attempt's current claim,
    and the carrier keeps the parent's exact task, owner and conversation, so
    it gains nothing the parent did not already hold.
    """
    context = parent.get('_conversationWorkerContext')
    if context is None:
        return
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('invalid conversation worker context')
    if (carrier.get('id') != parent.get('id') or carrier.get('convId') != parent.get('convId')
            or carrier.get('_userId') != parent.get('_userId')):
        raise ValueError('Worker claim carrier differs from its parent identity')
    carrier['_conversationWorkerContext'] = context


def inherit_conversation_worker(parent, child):
    """Share the trusted root claim; the existing child scope owns cancellation."""
    context = parent.get('_conversationWorkerContext')
    if context is None:
        return
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('invalid conversation worker context')
    principal = PrincipalContext.from_payload(parent['_principalContext'])
    child_principal = PrincipalContext.from_payload(child['_principalContext'])
    if (principal != context.principal or child_principal != principal
            or child.get('_userId') != principal.require_owner(context='worker child')):
        raise PermissionError('Worker child principal differs from its root')
    if (child.get('_parentTaskId') != parent.get('id') or child.get('id') == parent.get('id')
            or child.get('convId') != parent.get('convId')
            or parent.get('convId') != context.claim.payload.get('conversationId')):
        raise ValueError('Worker child lineage differs from its parent')
    if '_conversationWorkerContext' in child:
        raise ValueError('Worker child cannot replace existing execution authority')
    context.checkpoint()
    child['_conversationWorkerContext'] = context


def publication_arguments(task, *, terminal=False):
    context = task.get('_conversationWorkerContext')
    if context is None:
        return {}
    if not isinstance(context, WorkerExecutionContext):
        raise TypeError('invalid conversation worker context')
    return {'worker_claim': context.publication_claim(terminal=terminal, draining=True)}


def replay_publication_arguments(task, *, terminal=False):
    proof = publication_arguments(task, terminal=terminal)
    if not proof:
        return {}
    return {**proof, 'user_id': task['_conversationWorkerContext'].principal.require_owner(
        context='conversation replay publication')}
