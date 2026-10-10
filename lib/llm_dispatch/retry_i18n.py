"""lib/llm_dispatch/retry_i18n.py — Single source of truth for dispatch-retry
HUD i18n fields.

The dispatcher (``lib/llm_dispatch/api.py``) reports retry cycles via
``on_retry(attempt, reason=…, status_code=…)`` where ``reason`` is a short
ENGLISH log token ('Endpoint unreachable', …). Two independent emitters
surface those cycles as transient ``retrying`` PHASE events:

  • ``lib/tasks_pkg/manager/_stream.py::_on_retry``          (main chat bubble)
  • ``lib/swarm/agent.py::_on_dispatch_retry``               (swarm/flow
    worker bubble, via step_phase → FlowEventAdapter)

Both MUST ship the same structured ``detailKey`` / ``detailArgs`` (plus a
typed ``reasonKey`` for known tokens) so the frontend HUD localizes instead
of leaking raw English jargon mid-generation. This module owns the mapping
and the branch selection so the two emitters can never drift apart.

Wire contract (unchanged): each emitter still computes its OWN legacy
``detail`` string and keeps it byte-identical — ``detailKey``/``detailArgs``
are additive; headless / non-i18n clients keep reading ``detail``.

This module also owns the retry-phase emission budget shared by those two
emitters.  Dispatch retries may intentionally wait forever, but their durable
status history must not grow forever with them.  Liveness callbacks continue
to run on every cycle; only duplicate UI/durable phase frames are sampled.
"""

from __future__ import annotations

GATEWAY_PREFIXES = ('aws.', 'vertex.', 'gcp.', 'azure.', 'bedrock.')


class RetryPhaseEventBudget:
    """Bound duplicate retry/wait phase frames without bounding the retry.

    One instance belongs to one LLM round.  For each coarse status signature,
    emit occurrence 1, 2, 4, ... 32768 and suppress the rest.  At most eight
    signatures are tracked, so one round can persist no more than 128 sampled
    retry/wait frames even if an upstream remains unavailable indefinitely.

    Callers must perform their cheap liveness update *before* asking this
    budget: a suppressed UI frame is still proof that the dispatch loop is
    alive and must remain user-abortable.
    """

    MAX_SIGNATURES = 8
    MAX_EVENTS_PER_SIGNATURE = 16

    def __init__(self) -> None:
        self._occurrences: dict[tuple, int] = {}

    @property
    def maximum_events(self) -> int:
        """Hard upper bound for one budget instance (measurement contract)."""
        return self.MAX_SIGNATURES * self.MAX_EVENTS_PER_SIGNATURE

    def should_emit(self, signature: tuple) -> bool:
        """Return whether this occurrence should become a phase event.

        ``signature`` must contain only coarse bounded fields (for example
        phase kind, typed reason and status), never request text or exception
        bodies.  Unknown signatures beyond the fixed cardinality budget are
        ignored rather than growing this bookkeeping map.
        """
        if signature not in self._occurrences:
            if len(self._occurrences) >= self.MAX_SIGNATURES:
                return False
            self._occurrences[signature] = 0
        occurrence = self._occurrences[signature] + 1
        self._occurrences[signature] = occurrence
        if occurrence > (1 << (self.MAX_EVENTS_PER_SIGNATURE - 1)):
            return False
        return occurrence & (occurrence - 1) == 0


def display_model_name(model: str) -> str:
    """Strip internal gateway/provider prefixes for a user-facing label."""
    name = model or 'the model'
    for prefix in GATEWAY_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


# Dispatcher retry-reason tokens → typed i18n keys (static/js/i18n.js
# `stream.retryReason.*`). Unknown tokens fall back to the raw reason on the
# frontend (same ruling as an unknown detailKey).
# The dispatcher's reason token for a gateway/upstream-outage retry cycle
# (raised ONLY with RateLimitError.is_gateway=True — see lib/llm_errors.py
# _GATEWAY_THROTTLE_STATUS and the vendor-transient 4xx escalations).
GATEWAY_RETRY_TOKEN = 'Upstream error'
RETRY_REASON_KEYS = {
    'Endpoint unreachable': 'stream.retryReason.endpointUnreachable',
    'Request timed out': 'stream.retryReason.requestTimedOut',
    'Waiting for model (rate-limited)': 'stream.retryReason.waitingForModel',
    'Key balance exhausted': 'stream.retryReason.keyBalanceExhausted',
    'Upstream error': 'stream.retryReason.upstreamError',
    'Waiting for model (retry backoff)': 'stream.retryReason.waitingBackoff',
    'Waiting for model (shared project limit)': 'stream.retryReason.waitingSharedProject',
    'Rate limited (429)': 'stream.retryReason.rateLimited',
}

# These tokens describe the dispatcher's 300 ms all-slots-cooling poll, not
# completed upstream request attempts.  The callback's ``attempt`` value is
# still retained on the phase event for diagnostics/coalescing, but must not
# be interpolated into user-facing copy as "attempt N".
COOLDOWN_WAIT_REASON_TOKENS = frozenset({
    'Waiting for model (retry backoff)',
    'Waiting for model (shared project limit)',
})


def cooldown_wait_label(causes: set) -> tuple:
    """(reason token, status_code) for an all-slots-cooling wait — labelled by
    the ACTUAL cooldown cause, never a hardcoded 限流.

    Precedence: shared-project contention > per-key rate-limit > generic
    backoff. Contention wins because it is the most actionable truth (the
    saturation is EXTERNAL — other tenants filled the shared pipe, not
    anything this key did). The contention token rides status_code 0 so
    retry_phase_fields takes the reason branch — a 429 status would swallow
    it into the generic rate-limited detailKey.
    """
    if causes and 'contention' in causes:
        return 'Waiting for model (shared project limit)', 0
    if causes and 'rate_limit' not in causes:
        return 'Waiting for model (retry backoff)', 0
    return 'Waiting for model (rate-limited)', 429


def retry_phase_fields(*, model: str, attempt: int, reason: str = '',
                       status_code: int = 0,
                       legacy_detail: str = '') -> dict:
    """Compute the structured i18n fields for a ``retrying`` PHASE event.

    Args:
        model: Raw model id (gateway prefixes are stripped for the
            user-facing ``detailArgs['model']`` label).
        attempt: Dispatcher retry/cooldown cycle count.
        reason: Dispatcher reason token (may be '').
        status_code: HTTP status that triggered the cycle (0 = transport).
        legacy_detail: The emitter's pre-existing English/zh detail string,
            returned unchanged as ``detail`` (wire parity).

    Returns:
        ``{'detail', 'detailKey', 'detailArgs'}`` — detailArgs carries
        ``reasonKey`` only when the token is known.
    """
    label = display_model_name(model)
    if status_code == 429:
        detail_key = 'stream.phase.retryRateLimited'
        detail_args = {'model': label, 'attempt': attempt}
    elif reason == GATEWAY_RETRY_TOKEN:
        # Gateway/outage class — the dispatcher passes this token ONLY for
        # RateLimitError(is_gateway=True) (502/503/504 + vendor-transient
        # wrapped 4xx). Strict mode WAITS IT OUT on the pinned model and
        # never switches (owner directive 2026-08-18), so the HUD gets a
        # dedicated key that says exactly that instead of the generic
        # "retrying…{reason}" shape. ``status`` rides along for clients
        # that want to name the HTTP code (5xx only — a wrapped 4xx would
        # read as an auth/permission error, which it is not).
        detail_key = 'stream.phase.retryGateway'
        detail_args = {'model': label, 'attempt': attempt}
        if status_code:
            detail_args['status'] = status_code
    elif reason in COOLDOWN_WAIT_REASON_TOKENS:
        detail_key = 'stream.phase.retryCooldownWait'
        detail_args = {'reason': reason, 'model': label}
        detail_args['reasonKey'] = RETRY_REASON_KEYS[reason]
    elif reason:
        detail_key = 'stream.phase.retryReason'
        detail_args = {'reason': reason, 'model': label, 'attempt': attempt}
        reason_key = RETRY_REASON_KEYS.get(reason)
        if reason_key:
            detail_args['reasonKey'] = reason_key
    else:
        detail_key = 'stream.phase.retryGeneric'
        detail_args = {'model': label, 'attempt': attempt}
    return {'detail': legacy_detail, 'detailKey': detail_key,
            'detailArgs': detail_args}


__all__ = ['GATEWAY_PREFIXES', 'RetryPhaseEventBudget',
           'display_model_name', 'GATEWAY_RETRY_TOKEN', 'RETRY_REASON_KEYS',
           'COOLDOWN_WAIT_REASON_TOKENS', 'cooldown_wait_label',
           'retry_phase_fields']
