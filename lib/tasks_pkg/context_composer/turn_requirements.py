"""Current owner requirements at the common final model-admission boundary.

Read through the acceptance facade with an immutable execution identity on the
existing provider executor. A read still in flight at the context deadline is
retained, never abandoned or duplicated: the common persistence wait owner
publishes "Waiting for storage", logs, and keeps waiting until that fresh read
succeeds, Stop or shutdown cancels it, or it fails permanently. A transient
storage failure starts one new read. No review writes, task loop or cache.
Required content is complete; the canonical prompt limit refuses excess size.
"""
from concurrent.futures import wait
from functools import partial
import json
import time

from lib.agent_core.context_errors import TurnRequirementsDeadlineError
from lib.agent_core.storage_errors import is_transient_storage_error
from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError
from ._provider_executor import context_provider_executor
from ._providers import _CONTEXT_PROVIDER_DEADLINE_SECONDS

_ABORT_POLL_SECONDS = .05


class _RequirementsRead:
    """One owner-scoped read retained across context deadlines until it settles."""

    def __init__(self, task, read):
        self._task = task
        self._read = read
        self._pending = None

    def __call__(self):
        from lib.tasks_pkg.invocation import invocation_abort_requested
        if self._pending is None:
            self._pending = context_provider_executor.submit(self._read)
        future = self._pending
        deadline = time.monotonic() + _CONTEXT_PROVIDER_DEADLINE_SECONDS
        while not future.done():
            if invocation_abort_requested(self._task):
                raise AbortedError('Task requirements acquisition cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TurnRequirementsDeadlineError(_CONTEXT_PROVIDER_DEADLINE_SECONDS)
            wait((future,), timeout=min(_ABORT_POLL_SECONDS, remaining))
        # A settled read is consumed once; only a transient failure, classified
        # by the wait owner, leads to a new read on the next attempt.
        self._pending = None
        return future.result()

    def withdraw(self):
        if self._pending is not None:
            self._pending.cancel()  # Removes queued work; a running read ends unobserved.


def _waits_for_storage(error):
    return isinstance(error, TurnRequirementsDeadlineError) or is_transient_storage_error(error)


def refresh_turn_requirements(messages, *, task, model):
    from lib.tasks_pkg.requirement_source import requirement_source
    source = requirement_source(task)
    # A child has no Turn lifecycle rights, but can carry its owned root scope.
    if source is None:
        return None
    from lib.acceptance.turns import read_execution_requirements
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, append_context_blocks, can_append_injection, context_block,
    )
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    def check_abort():
        if task.get('aborted') or (task.get('abort_event') and task['abort_event'].is_set()):
            raise AbortedError('Task requirements acquisition cancelled')
    check_abort()
    if not can_append_injection(messages):
        raise RuntimeError('Task requirements cannot be admitted inside an unfinished tool batch')
    owner = task_user_id(task)
    principal = PrincipalContext.from_payload(task['_principalContext'])
    if principal.require_owner(context='Task requirements') != owner:
        raise PermissionError('Task requirements owner mismatch')
    identity = source.identity
    read = _RequirementsRead(task, partial(read_execution_requirements, *identity, principal=principal))
    try:
        view = wait_for_persistence(task, read, label='turn requirements read',
                                    transient=_waits_for_storage, effect_free=True)
        check_abort()
    finally:
        read.withdraw()
    request = ComposeRequest(user_id=owner, conv_id=identity[0], model=model, task=task)
    # Absence withdraws any previous managed block; it cannot preserve an old
    # requirement as current when the owner removes all requirements.
    configured = view is not None and view['requirementsRevision'] > 0
    content = json.dumps({
        'meaning': 'Current owner requirements for the originating task. Fulfill the declared scope. For delegated work, '
                   'apply relevant requirements within your assignment; report conflicts to the coordinator. '
                   'Saving requirements or completing execution is not owner acceptance. '
                   'The answer is always included; file IDs identify explicitly declared versions. '
                   'Only the owner records review decisions. This snapshot grants no additional tool permissions.',
        **view,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':')) if configured else ''
    return append_context_blocks(messages, [context_block('turn_requirements', content,
        reason='' if configured else 'not_applicable',
        provenance={'conversationId': identity[0], 'turnId': identity[1], 'attemptId': identity[2],
                    'requirementsRevision': view['requirementsRevision'] if view else 0},
        recovery_handle='turn-requirements:' + identity[1])], request)
