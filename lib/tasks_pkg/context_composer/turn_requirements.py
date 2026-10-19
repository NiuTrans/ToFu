"""Current owner requirements at the common final model-admission boundary.

Read through the acceptance facade with an immutable execution identity, using
the existing provider executor/deadline. No review writes, task loop or cache.
Required content is complete; the canonical prompt limit refuses excess size.
"""
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
import json
import time

from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError
from ._provider_executor import context_provider_executor
from ._providers import _CONTEXT_PROVIDER_DEADLINE_SECONDS


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
    future = context_provider_executor.submit(partial(read_execution_requirements, *identity, principal=principal))
    deadline = time.monotonic() + _CONTEXT_PROVIDER_DEADLINE_SECONDS
    try:
        while True:
            check_abort()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Current task requirements could not be read before the context deadline')
            try:
                view = future.result(timeout=min(.05, remaining))
                break
            except FutureTimeout:
                if future.done():
                    raise
        check_abort()
    finally:
        if not future.done():
            future.cancel()
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
