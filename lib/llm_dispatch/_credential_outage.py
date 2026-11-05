"""Credential-source outage routing shared by the dispatch retry loops.

Responsibility: ``LocalRequestPreparationError(provider_reached=False)`` caused
by ``SubscriptionCredentialUnavailableError`` (unreadable source) or
``SubscriptionLoginRequiredError`` (missing/expired sign-in) proves that one
credential source could not issue a bearer before provider ingress. Unlike a
deterministic body-construction failure, an eligible slot that does not share
that source can still serve the unchanged request under the same model/pin
constraints. Only unreadable transient sources earn same-source retries, which
live in the task layer's credential recovery, not here.

Entry point: ``CredentialOutageRouting``, one instance per dispatch call. It
owns the request-local exclusion of every slot sharing the failed source (same
key, owner-scoped credential id, or outbound OAuth resolver) and the learned,
owner-scoped cooldown in ``lib.model_routing.health.owner_health_registry``.
Only a request by a slot's own positive routing owner learns, consults or
clears that slot's fact; an ownerless request speaks for no owner.
A learned cooldown only demotes: the cooled credential is still tried when no
other eligible slot remains, so its typed error (and the shared credential
recovery that depends on it) is never replaced by a no-slot error.

Dependencies: the dispatcher slot pool and ``lib.model_routing.health``.
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def credential_source_outage(error):
    """Return the typed cause when ``error`` is a proven-unsent credential-source outage."""
    from lib.llm.llm_errors import (
        LocalRequestPreparationError, SubscriptionCredentialUnavailableError,
        SubscriptionLoginRequiredError)
    if not isinstance(error, LocalRequestPreparationError) or error.provider_reached:
        return None
    cause = error.__cause__
    return cause if isinstance(
        cause, (SubscriptionCredentialUnavailableError, SubscriptionLoginRequiredError)) else None


def _shares_credential_source(slot, failed) -> bool:
    if slot.key_name == failed.key_name:
        return True
    if (failed.route_credential_id
            and slot.route_credential_id == failed.route_credential_id
            and slot.routing_owner_user_id == failed.routing_owner_user_id
            and slot.routing_tenant_id == failed.routing_tenant_id):
        return True
    # Within one request every slot of a subscription kind resolves its bearer
    # through the same outbound login resolver for the requesting owner.
    return bool(failed.oauth) and slot.oauth == failed.oauth


def _requesting_owner(owner_user_id):
    """Positive requesting owner id, or None for an ownerless request."""
    from lib.identity import require_user_id
    try:
        return require_user_id(owner_user_id, context='credential outage routing')
    except ValueError:
        return None


def _learned_credential(slot, owner):
    """Owner-scoped credential identity, or None when health is unattributable.

    Subscription slots of every owner share one outbound resolver, so an
    ownerless failure attributed to each slot's owner would cool every owner's
    credential at once. A request never teaches another owner's table either.
    """
    if (owner is None or slot.routing_owner_user_id != owner
            or not slot.route_credential_id):
        return None
    return owner, slot.routing_tenant_id, slot.route_credential_id


class CredentialOutageRouting:
    """Request-local credential-source exclusions plus learned cooldowns."""

    def __init__(self, dispatcher=None, owner_user_id=None):
        self._dispatcher = dispatcher
        self._owner = _requesting_owner(owner_user_id)
        self.failed_keys: set[str] = set()
        self.cooled_keys: set[str] = self._learned_cooled_keys()

    @property
    def keys(self) -> set[str]:
        return self.failed_keys | self.cooled_keys

    def _slots(self):
        return list(self._dispatcher.slots) if self._dispatcher is not None else []

    def _learned_cooled_keys(self) -> set[str]:
        from lib.model_routing.health import (
            HealthTarget, existing_owner_health_registry, owner_health_learned)
        if self._owner is None or not owner_health_learned():
            return set()
        cooled = set()
        for slot in self._slots():
            learned = _learned_credential(slot, self._owner)
            if learned is None:
                continue
            owner, tenant, credential_id = learned
            registry = existing_owner_health_registry(owner, tenant)
            if registry is not None and registry.target_unavailable(
                    HealthTarget('credential', credential_id)):
                cooled.add(slot.key_name)
        return cooled

    def retry_cooled(self) -> bool:
        """Re-admit learned-cooled slots once no other eligible slot remains."""
        if not self.cooled_keys:
            return False
        self.cooled_keys.clear()
        return True

    def absorb(self, error, slot, *, log_prefix='') -> bool:
        """Exclude and learn a proven-unsent credential-source outage.

        Returns False for every other failure; the caller then surfaces the
        error unchanged.
        """
        cause = credential_source_outage(error)
        if cause is None:
            return False
        from lib.llm.llm_errors import SubscriptionLoginRequiredError
        from lib.model_routing.health import (
            CREDENTIAL_SOURCE_UNAVAILABLE, HealthTarget, classify_failure,
            owner_health_registry)
        if isinstance(cause, SubscriptionLoginRequiredError):
            reason, source = 'sign_in_required', slot.oauth or 'subscription'
        else:
            reason, source = cause.reason, cause.credential_source
        sharing = [candidate for candidate in self._slots()
                   if _shares_credential_source(candidate, slot)]
        keys = {candidate.key_name for candidate in sharing} | {slot.key_name}
        self.failed_keys |= keys
        self.cooled_keys -= keys
        scope = classify_failure(kind=CREDENTIAL_SOURCE_UNAVAILABLE)
        learned = {_learned_credential(candidate, self._owner)
                   for candidate in (slot, *sharing)} - {None}
        for owner, tenant, credential_id in learned:
            owner_health_registry(owner, tenant).record_failure(
                HealthTarget(scope, credential_id),
                reason=f'{CREDENTIAL_SOURCE_UNAVAILABLE}:{reason}')
        logger.warning(
            '%s credential source unavailable (%s, source=%s) — excluding %d '
            'slot(s) sharing it and trying other eligible slots',
            log_prefix, reason, source, len(keys))
        return True

    def note_success(self, slot) -> None:
        """A verified provider success proves the slot's credential source healthy."""
        from lib.model_routing.health import (
            HealthTarget, existing_owner_health_registry, owner_health_learned)
        if not owner_health_learned():
            return
        learned = _learned_credential(slot, self._owner)
        if learned is None:
            return
        owner, tenant, credential_id = learned
        registry = existing_owner_health_registry(owner, tenant)
        if registry is not None:
            registry.clear_target(HealthTarget('credential', credential_id))
