"""Public dispatch error and wait-status contract types."""

from lib.llm_errors import RateLimitError, is_subscription_quota_error
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatchWaitStatus:
    """Typed status for a pool-capacity wait before an attempt is reserved."""

    kind: str
    request_elapsed_s: float
    transport_idle_s: float = 0.0
    semantic_idle_s: float = 0.0
    response_headers_seen: bool = False
    transport_byte_count: int = 0
    sse_event_count: int = 0
    reasoning_chars: int = 0
    content_chars: int = 0
    tool_call_count: int = 0


class DispatchNoAdmissibleSlot(RuntimeError):
    """Strict optional-work policy rejected every slot before transport.

    No upstream call was made, so cooldown polling or an outer retry cannot
    discover capacity unless external billing/key state changes first.
    """

    terminal_for_optional_work = True


class DispatchSharedContentionDeferred(RuntimeError):
    """Optional work yielded before transport to an active family gate.

    This is not an upstream 429: the selected provider/model family was
    already known to be contended and no request was dispatched. Durable or
    user-facing work keeps the default wait-and-probe policy; reconstructible
    enrichment may end its current refresh and try again through its ordinary
    lifecycle.
    """

    terminal_for_optional_work = True
    request_not_dispatched = True
    reason = 'shared_contention_deferred'
    is_shared_contention = True

    def __init__(self, *, retry_after_s: float):
        self.retry_after_s = max(0.0, float(retry_after_s))
        super().__init__(
            'Optional dispatch deferred by an active shared-contention gate'
        )


class DispatchRateLimitBudgetExceeded(RateLimitError):
    """A caller-owned ceiling on actual upstream rate-limit responses.

    The dispatcher remains indefinitely waitable by default. Optional
    background work may set ``max_429_attempts`` so a provider-wide outage
    cannot consume API requests until the much longer wall-clock deadline.
    Capacity-poll cycles do not count: ``attempts`` records only requests that
    reached an upstream and returned a rate-limit-class response.
    """

    terminal_for_optional_work = True

    def __init__(self, last_error: Exception, *, attempts: int, limit: int):
        super().__init__(
            f'Upstream rate-limit attempt budget exhausted '
            f'({attempts}/{limit})',
            is_gateway=bool(getattr(last_error, 'is_gateway', False)),
            reason='rate_limit_attempt_budget_exhausted',
            status_code=int(getattr(last_error, 'status_code', 0) or 429),
            is_shared_contention=bool(
                getattr(last_error, 'is_shared_contention', False)),
            is_subscription_quota=bool(
                getattr(last_error, 'is_subscription_quota', False)
                or is_subscription_quota_error(str(last_error))),
            retry_after_s=getattr(last_error, 'retry_after_s', None),
        )
        self.attempts = int(attempts)
        self.limit = int(limit)
