"""Durably admit an accepted conversation without constructing an executor.

The Attempt is the authority for identity and configuration. One storage command
binds it and enqueues its WorkerJob atomically; acknowledgement loss preserves
that same job. Physical preparation belongs only to the claimed worker.
"""
from collections.abc import Mapping

from lib.storage.errors import StorageError


def dispatch_accepted_attempt(attempt_id, *, user_id):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import restore_execution_identity
    attempt = turns.get_attempt(attempt_id, user_id=user_id, include_execution_config=True)
    if not attempt or attempt['status'] != 'pending':
        return
    if attempt.get('taskId') == 'conversation-attempt:' + attempt_id:
        # Accepted replay reads its existing binding even if the execution
        # grant has since been revoked. The worker settles that revocation;
        # acknowledging an existing reference grants no new execution.
        return
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        raise ValueError('Accepted attempt config must be an object')
    try:
        principal, _attended = restore_execution_identity(dict(config, _turnOwnerUserId=user_id))
    except (PermissionError, TypeError, ValueError) as error:
        from lib.conversation_sync.command_service import AttemptStartFailure, attempt_start_error_envelope
        envelope = attempt_start_error_envelope(error)
        # This is an unbound authorization refusal. The storage command still
        # refuses an unfenced failure if another writer bound the job first.
        turns.fail_start(attempt_id, envelope, user_id=user_id)
        raise AttemptStartFailure(turns.get_turn(
            attempt['conversationId'], attempt['turnId'], user_id=user_id), envelope) from error
    try:
        return turns.dispatch_attempt_to_worker(principal, attempt_id)
    except StorageError as error:
        if error.code == 'database_conflict':
            # Stop or supersession can commit between our read and the atomic
            # binding. It is a refusal to launch, never a failed model call.
            current = turns.get_attempt(attempt_id, user_id=user_id)
            if current is None or current['status'] != 'pending':
                return
        # A transport failure may follow COMMIT. Do not settle the Attempt,
        # launch locally or mint a second task in response to a missing ACK.
        raise
