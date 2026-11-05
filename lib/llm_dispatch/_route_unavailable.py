"""Route-unavailable dispatch policy shared by the stream and non-stream loops.

Responsibility: ``ModelRouteUnavailableError`` (a gateway 5xx carrying
New-API's distributor ``model_not_found`` / no-channel verdict) proves that one
provider/wire model has no enabled upstream channel right now. It is route
evidence, not key health, not a whole-gateway outage and not per-key
contention, and it can heal when the gateway re-enables a channel. One
``RouteUnavailableRouting`` per dispatch call:

* ``absorb`` holds every slot that sends the failed wire model to the same
  provider endpoint until its next probe time, so non-strict and multi-route dispatch fail over to
  another eligible route or model without re-probing it; a hold lapses at
  that time, so a long call still notices the route coming back;
* when nothing but held routes can serve (strict or pinned single route), the
  loop sleeps ``probe_delay_s()`` abortably and probes again: the owner's
  waitable policy never fails the turn, and the fixed
  ``ROUTE_UNAVAILABLE_BACKOFF_S`` schedule replaces 0.5 s gateway cycling;
* the fact is learned at Deployment scope (``classify_failure``) in the slot
  routing owner's ``owner_health_registry`` and a provider success on that
  Deployment clears it. As for credential outages, only a request whose
  positive owner equals the slot's routing owner learns or clears it.

Entry points: ``RouteUnavailableRouting``, ``route_unavailable_reason``,
``log_route_unavailable``. Dependencies: the dispatcher slot pool,
``lib.model_routing.health`` and the retry-notice token owner
``lib.llm_dispatch.retry_i18n``.
"""
from __future__ import annotations

import time

from lib.log import get_logger
from lib.llm_dispatch._credential_outage import _requesting_owner
from lib.llm_dispatch.retry_i18n import ROUTE_UNAVAILABLE_REASON_PREFIX

logger = get_logger('lib.llm_dispatch.api')

# Seconds before held routes are probed again when no other route can serve,
# indexed by the call's verdict count; the last step repeats. A fixed policy
# constant, independent of hardware and never a launch setting.
ROUTE_UNAVAILABLE_BACKOFF_S = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
_HEALTH_KIND = 'model_not_found'


def route_unavailable_reason(error) -> str:
    """Retry-notice reason carrying the gateway's bounded message."""
    message = getattr(error, 'gateway_message', '') or str(error)[:160]
    return f'{ROUTE_UNAVAILABLE_REASON_PREFIX}{message}'


def _route_identity(slot):
    return (str(slot.routing_provider_id or slot.provider_id or ''),
            str(slot.base_url or ''), str(slot.model or ''))


def _learned_deployment(slot, owner):
    """Owner-scoped Deployment identity, or None when health is unattributable."""
    if (owner is None or slot.routing_owner_user_id != owner
            or not slot.route_deployment_id):
        return None
    return owner, slot.routing_tenant_id, slot.route_deployment_id


class RouteUnavailableRouting:
    """Request-local holds on no-channel routes and their probe schedule."""

    def __init__(self, dispatcher=None, owner_user_id=None):
        self._dispatcher = dispatcher
        self._owner = _requesting_owner(owner_user_id)
        self._held: set[tuple[str, str]] = set()
        self.verdicts = 0
        self._probe_at = 0.0

    @property
    def pairs(self) -> set[tuple[str, str]]:
        """Held ``(key_name, model)`` pairs; holds lapse at the probe time."""
        if self._held and time.monotonic() >= self._probe_at:
            self._held.clear()
        return set(self._held)

    def absorb(self, error, slot) -> float:
        """Hold ``slot``'s provider/wire model; return seconds to its probe."""
        delay = ROUTE_UNAVAILABLE_BACKOFF_S[
            min(self.verdicts, len(ROUTE_UNAVAILABLE_BACKOFF_S) - 1)]
        self.verdicts += 1
        self._probe_at = time.monotonic() + delay
        route = _route_identity(slot)
        pool = list(getattr(self._dispatcher, 'slots', None) or ())
        self._held |= {(candidate.key_name, candidate.model)
                       for candidate in pool
                       if _route_identity(candidate) == route}
        self._held.add((slot.key_name, slot.model))
        learned = _learned_deployment(slot, self._owner)
        if learned is not None:
            from lib.model_routing.health import (
                HealthTarget, classify_failure, owner_health_registry)
            owner, tenant, deployment_id = learned
            owner_health_registry(owner, tenant).record_failure(
                HealthTarget(classify_failure(kind=_HEALTH_KIND), deployment_id),
                reason='route_unavailable:%s' % (
                    getattr(error, 'error_code', '') or _HEALTH_KIND))
        return delay

    def probe_delay_s(self) -> float:
        """Seconds until held routes are due for their next probe."""
        return max(0.0, self._probe_at - time.monotonic())

    def note_success(self, slot) -> None:
        """A provider success proves the slot's Deployment has a channel."""
        from lib.model_routing.health import (
            HealthTarget, classify_failure, existing_owner_health_registry,
            owner_health_learned)
        if not owner_health_learned():
            return
        learned = _learned_deployment(slot, self._owner)
        if learned is None:
            return
        owner, tenant, deployment_id = learned
        registry = existing_owner_health_registry(owner, tenant)
        if registry is not None:
            registry.clear_target(HealthTarget(
                classify_failure(kind=_HEALTH_KIND), deployment_id))


def log_route_unavailable(log_prefix, slot, error, *, verdicts, probe_in_s):
    """Record one verdict; the first of a call warns, later ones are info."""
    log = logger.warning if verdicts == 1 else logger.info
    log('%s Model route unavailable on %s:%s (HTTP %s, %s, verdict #%d): %s '
        '— provider/wire model held %.0fs while other eligible routes serve',
        log_prefix, slot.key_name, slot.model,
        getattr(error, 'status_code', 0) or '?',
        getattr(error, 'error_code', '') or '?', verdicts,
        getattr(error, 'gateway_message', '') or str(error)[:160],
        probe_in_s)
