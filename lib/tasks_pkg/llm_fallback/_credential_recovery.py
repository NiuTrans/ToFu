"""Bounded same-round recovery for proven pre-ingress credential failures.

Entries: call_with_credential_recovery and stream_with_credential_recovery.
Dependencies: typed LLM errors and the existing task phase/event authority.
This module never builds requests, executes tools, rotates routes or settles.
"""
import time

from lib.agent_core.events import Phase, build_phase
from lib.llm.llm_errors import AbortedError
from lib.log import get_logger
from lib.tasks_pkg.manager._events import append_event

logger = get_logger(__name__)

_CREDENTIAL_PREPARE_MAX_ATTEMPTS = 3
_CREDENTIAL_PREPARE_RECOVERY_SECONDS = 30.0


def call_with_credential_recovery(call, *, abort_check=None, on_retry=None,
                                  retry_allowed=None):
    """Retry a proven transient pre-ingress failure on the same bound request.

    Callers retain request construction, route authority, usage and phase
    ownership. No returned provider work or untyped failure is retried here.
    """
    from lib.llm.llm_errors import LocalRequestPreparationError, SubscriptionCredentialUnavailableError
    deadline = time.monotonic() + _CREDENTIAL_PREPARE_RECOVERY_SECONDS
    for attempt in range(1, _CREDENTIAL_PREPARE_MAX_ATTEMPTS + 1):
        if abort_check is not None and abort_check():
            raise AbortedError('Task aborted during credential recovery')
        try:
            return call()
        except LocalRequestPreparationError as error:
            cause = error.__cause__
            if (not isinstance(cause, SubscriptionCredentialUnavailableError)
                    or not cause.transient or error.provider_reached
                    or (retry_allowed is not None and not retry_allowed())
                    or attempt >= _CREDENTIAL_PREPARE_MAX_ATTEMPTS
                    or time.monotonic() >= deadline):
                raise
            if on_retry is not None:
                on_retry(attempt, cause)
            wait_until = min(deadline, time.monotonic() + attempt)
            while time.monotonic() < wait_until:
                if abort_check is not None and abort_check():
                    raise AbortedError('Task aborted during credential recovery')
                time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
            if abort_check is not None and abort_check():
                raise AbortedError('Task aborted during credential recovery')
            if time.monotonic() >= deadline:
                raise


def stream_with_credential_recovery(stream, task, body, **kwargs):
    """Retry only proven pre-ingress transient credential failures.

    Keeps the same round/body/model and never repeats a delivered tool call.
    Deterministic body bugs, policy/account changes and stream failures escape.
    The normal round commit and settlement paths remain the only authorities.
    """
    tool_delivered = False
    callback = kwargs.get('on_tool_call_ready')
    if callback is not None:
        def deliver_tool(*args, **callback_kwargs):
            nonlocal tool_delivered
            tool_delivered = True
            return callback(*args, **callback_kwargs)
        kwargs = {**kwargs, 'on_tool_call_ready': deliver_tool}

    def report_retry(attempt, cause):
        logger.warning('[%s] Shared credential preparation retry attempt=%d source=%s issue=%s',
                       task['id'][:8], attempt, cause.credential_source, cause.reason)
        append_event(task, build_phase(
            Phase.RETRYING,
            detail='正在恢复共享登录凭据 / Recovering shared sign-in credentials',
            attempt=attempt, model=body.get('model', '')))

    return call_with_credential_recovery(
        lambda: stream(task, body, **kwargs),
        abort_check=lambda: bool(task.get('aborted')),
        retry_allowed=lambda: not tool_delivered,
        on_retry=report_retry)
