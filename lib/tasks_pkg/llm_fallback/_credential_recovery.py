"""Bounded same-round recovery for proven pre-ingress credential failures.

Entry: stream_with_credential_recovery, with an injected normal stream call.
Dependencies: typed LLM errors and the existing task phase/event authority.
This module never builds requests, executes tools, rotates routes or settles.
"""
import time

from lib.agent_core.events import Phase, build_phase
from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.tasks_pkg.manager._events import append_event

logger = get_logger(__name__)

_CREDENTIAL_PREPARE_MAX_ATTEMPTS = 3
_CREDENTIAL_PREPARE_RECOVERY_SECONDS = 30.0


def stream_with_credential_recovery(stream, task, body, **kwargs):
    """Retry only proven pre-ingress transient credential failures.

    Keeps the same round/body/model and never repeats a delivered tool call.
    Deterministic body bugs, policy/account changes and stream failures escape.
    The normal round commit and settlement paths remain the only authorities.
    """
    from lib.llm_errors import LocalRequestPreparationError, SubscriptionCredentialUnavailableError
    deadline = time.monotonic() + _CREDENTIAL_PREPARE_RECOVERY_SECONDS
    tool_delivered = False
    callback = kwargs.get('on_tool_call_ready')
    if callback is not None:
        def deliver_tool(*args, **callback_kwargs):
            nonlocal tool_delivered
            tool_delivered = True
            return callback(*args, **callback_kwargs)
        kwargs = {**kwargs, 'on_tool_call_ready': deliver_tool}
    for attempt in range(1, _CREDENTIAL_PREPARE_MAX_ATTEMPTS + 1):
        if task.get('aborted'):
            raise AbortedError('Task aborted during credential recovery')
        try:
            return stream(task, body, **kwargs)
        except LocalRequestPreparationError as error:
            cause = error.__cause__
            if (not isinstance(cause, SubscriptionCredentialUnavailableError)
                    or not cause.transient or error.provider_reached or tool_delivered
                    or attempt >= _CREDENTIAL_PREPARE_MAX_ATTEMPTS
                    or time.monotonic() >= deadline):
                raise
            logger.warning('[%s] Shared credential preparation retry attempt=%d source=%s issue=%s',
                           task['id'][:8], attempt, cause.credential_source, cause.reason)
            append_event(task, build_phase(
                Phase.RETRYING,
                detail='正在恢复共享登录凭据 / Recovering shared sign-in credentials',
                attempt=attempt, model=body.get('model', '')))
            wait_until = min(deadline, time.monotonic() + attempt)
            while time.monotonic() < wait_until:
                if task.get('aborted'):
                    raise AbortedError('Task aborted during credential recovery')
                time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
            if task.get('aborted'):
                raise AbortedError('Task aborted during credential recovery')
            if time.monotonic() >= deadline:
                raise
