"""Bind a claimed conversation job to the existing invocation and Turn owners.

Entry points: worker_task_identity before registration, bind_conversation_worker
after registration, publication_arguments at domain writes. Only an explicitly
injected WorkerExecutionContext grants a claim; configuration is never a port.
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
    if '_conversationWorkerContext' in task or '_invocationAbortCheck' in task:
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
    task['_invocationAbortCheck'] = ChildCancellationScope(aborted)


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
