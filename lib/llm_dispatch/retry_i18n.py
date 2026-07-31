"""lib/llm_dispatch/retry_i18n.py — Single source of truth for dispatch-retry
HUD i18n fields.

The dispatcher (``lib/llm_dispatch/api.py``) reports retry cycles via
``on_retry(attempt, reason=…, status_code=…)`` where ``reason`` is a short
ENGLISH log token ('Endpoint unreachable', …). Two independent emitters
surface those cycles as transient ``retrying`` PHASE events:

  • ``lib/tasks_pkg/manager/_stream.py::_on_retry``          (main chat bubble)
  • ``lib/swarm/agent.py::_on_dispatch_retry``               (swarm/endpoint
    worker bubble, via step_phase → EndpointEventAdapter)

Both MUST ship the same structured ``detailKey`` / ``detailArgs`` (plus a
typed ``reasonKey`` for known tokens) so the frontend HUD localizes instead
of leaking raw English jargon mid-generation. This module owns the mapping
and the branch selection so the two emitters can never drift apart.

Wire contract (unchanged): each emitter still computes its OWN legacy
``detail`` string and keeps it byte-identical — ``detailKey``/``detailArgs``
are additive; headless / non-i18n clients keep reading ``detail``.
"""

from __future__ import annotations

GATEWAY_PREFIXES = ('aws.', 'vertex.', 'gcp.', 'azure.', 'bedrock.')


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


def cooldown_wait_label(causes: set) -> tuple:
    """(reason token, status_code) for an all-slots-cooling wait — labelled by
    the ACTUAL cooldown cause, never a hardcoded 限流.

    Precedence: shared-project contention > per-key rate-limit > generic
    backoff. Contention wins because it is the most actionable truth (the
    whole (provider, model) family is parked by EXTERNAL saturation, not by
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


__all__ = ['GATEWAY_PREFIXES', 'display_model_name', 'RETRY_REASON_KEYS',
           'cooldown_wait_label', 'retry_phase_fields']
