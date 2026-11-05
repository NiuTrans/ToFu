"""Owner-wide auxiliary summary selection, independent of the parent family.

The shared compiler owns eligibility. AA/declarations/reference cost rank usable
routes, never authorize them. Billing class orders them first: organization-billed
paid APIs, then the owner's own subscription/OAuth/local resources. Discovery is
secret-free; dispatch binds one witness.
"""
from lib.tasks_pkg.compaction._summary_routing import (
    _number, _tokens, _project_cost, _project_route_cost)
from lib.model_routing.dispatch_adapter import candidate_route_identity as route_identity

_RESOURCE_KINDS = {'oauth', 'subscription', 'local_identity'}
_RANKING = 'paid_api_then_resource_cost_aa'


def candidate_capacity(candidate, input_tokens, capacity_output_tokens):
    from lib.context_limits import resolve_learned_context_limit
    from lib.model_info import _clamp_route_max_tokens, _route_output_limit_key
    from lib.model_routing.inference import candidate_inference_metadata
    window = _tokens(candidate_inference_metadata(candidate).get('context_window'))
    ceiling = _clamp_route_max_tokens(
        candidate.model['model_id'], capacity_output_tokens,
        route_key=_route_output_limit_key(
            provider_id=candidate.provider_id,
            offering_id=candidate.offering['offering_id'],
            deployment_id=candidate.deployment['deployment_id'],
            protocol=candidate.connection['protocol'], model=candidate.model['model_id']),
        declared_limit=int(candidate.deployment.get('max_output_tokens') or 0))
    if not window:
        return 0
    window = resolve_learned_context_limit(candidate.provider_id, candidate.model['model_id'], window)
    return max(0, min(capacity_output_tokens, ceiling, window - input_tokens))


def credential_route_exclusion(group, *, owner, tenant):
    """Private request-local dependency witness; never receipt or health data.

    Equal encrypted references share the same envelope, even across model IDs.
    The materialized Codex binding selects one existing outbound login resolver;
    provider names, credential kinds and diagnostic source labels do not.
    """
    return {
        'owner': owner, 'tenant': str(tenant or ''),
        'credentialIds': frozenset(candidate.credential['credential_id']
                                   for candidate in group.candidates),
        'secretReferences': frozenset(candidate.credential['secret_reference']
            for candidate in group.candidates if candidate.credential['secret_reference']),
        'resolver': 'codex' if group.primary.slot.oauth == 'codex' else '',
    }


def candidate_credential_excluded(candidate, excluded_routes, *, owner, tenant):
    credential = candidate.credential
    return any(exclusion['owner'] == owner
        and exclusion['tenant'] == str(tenant or '')
        and (credential['credential_id'] in exclusion['credentialIds']
             or (credential['secret_reference']
                 and credential['secret_reference'] in exclusion['secretReferences']))
        for exclusion in excluded_routes)


def group_credential_excluded(group, excluded_routes, *, owner, tenant):
    """Check aliases whose actual resolver is knowable only after minting."""
    return any(exclusion['owner'] == owner
        and exclusion['tenant'] == str(tenant or '')
        and exclusion['resolver'] == 'codex'
        and group.primary.slot.oauth == 'codex'
        for exclusion in excluded_routes)


def record_auxiliary_route_success(group, *, owner, tenant):
    """A completed summary proves the group's exact route healthy again."""
    from lib.model_routing.health import existing_owner_health_registry
    health = existing_owner_health_registry(owner, tenant)
    if health is not None:
        for candidate in group.candidates:
            health.record_success(candidate)


def select_auxiliary_route(result, profiles, *, owner, tenant, input_tokens,
                           output_tokens, capacity_output_tokens, subscription_main=True,
                           main_cost=None, parent_offering_ids=(), parent_route_identities=(),
                           excluded_routes=(), main_serves_summary=False):
    from lib.model_routing import (ModelRoutingRepository, OwnerBoundary,
                                   NativeModelSelection, RoutePolicy)
    from lib.model_routing.routing import RouteCandidateCompiler
    from lib.model_routing.domain import ProviderOfferingRef
    from lib.tasks_pkg.compaction._summary_routing import _bucket_aa

    from lib.model_routing.health import existing_owner_health_registry

    authority = ModelRoutingRepository().get(OwnerBoundary.create(owner, tenant))
    compiler = RouteCandidateCompiler(authority.document)
    # Learned credential outages are owner facts that remove cooled candidates
    # after ranking; parent witnesses stay health-blind (identity, not availability).
    health = existing_owner_health_registry(owner, tenant)

    if parent_route_identities:
        resource_parents = []
        for identity in parent_route_identities:
            selection = NativeModelSelection(model=None, provider_offering=ProviderOfferingRef(
                identity['providerId'], identity['offeringId']))
            witnessed = [candidate for candidate in compiler.compile(selection, policy=RoutePolicy(
                required_capabilities=frozenset({'text'})))
                if route_identity(candidate, authority.revision) == identity]
            if len(witnessed) != 1:
                result['reason'] = 'parent_route_changed'
                return result
            resource_parents.append(witnessed[0].credential.get('kind') in _RESOURCE_KINDS)
        subscription_main = all(resource_parents)
    candidates = []
    for profile in profiles:
        provenance = profile.get('provenance') or {}
        if (profile.get('enabled') is not True or provenance.get('identityState') != 'confirmed'
                or provenance.get('source') != 'model_routing_v2'
                or provenance.get('ownerUserId') != owner
                or str(provenance.get('tenantId') or '') != str(tenant or '')
                or provenance.get('revision') != authority.revision
                or provenance.get('offeringId') in parent_offering_ids):
            continue
        aa_evidence = _bucket_aa({'members': [(None, profile)]})
        aa = aa_evidence[0] if aa_evidence else None
        declared = (profile.get('evidence') in ('provider_catalog', 'vendor_declaration')
                    and profile.get('autoSelectable') is True
                    and (_number(profile.get('confidence')) or 0) >= 0.8
                    and profile.get('quality') in ('light', 'standard', 'heavy', 'frontier'))
        # No name/family/quality/benchmark allowlist: registered, authorized
        # text routes are eligible even before optional profile coverage catches up.
        selection = NativeModelSelection(model=None, provider_offering=ProviderOfferingRef(
            profile['providerId'], provenance['offeringId']))
        compiled = compiler.compile(selection, policy=RoutePolicy(
            required_capabilities=frozenset({'text'}),
            required_context=input_tokens + output_tokens))
        for candidate in compiled:
            if candidate_credential_excluded(candidate, excluded_routes, owner=owner, tenant=tenant):
                continue
            actual_capacity = candidate_capacity(candidate, input_tokens, capacity_output_tokens)
            if actual_capacity < output_tokens:
                continue
            resource = candidate.credential.get('kind') in _RESOURCE_KINDS
            if resource and not subscription_main and main_serves_summary:
                # Owner decision 2026-11-02: subscriptions are the owner's own
                # quota; a paid main that fits is itself the paid fallback.
                continue
            quality_rank = {'light': 0, 'standard': 1, 'heavy': 2, 'frontier': 3}.get(
                profile.get('quality') if declared else None, 4)
            if resource:
                tier, maximum_cost = 1, None
                cost, basis = None, {'source': 'authorized_resource_policy',
                                    'tier': profile.get('quality', 'unknown'),
                                    'marginalCostKnown': False}
                from lib.pricing._tables import pricing_for_model
                reference = pricing_for_model(candidate.model['model_id'])
                proxy = _project_cost({'providerId': candidate.provider_id,
                                       'declaredPricing': {'currency': 'USD',
                                           'unit': 'per_million_tokens', **reference} if reference else None},
                                      input_tokens, output_tokens)
                economic_rank = proxy[0] if proxy else float('inf')
                if proxy:
                    basis['referenceProxyUsd'] = proxy[0]
                    basis['referenceSource'] = 'maintained_model_price_table'
            else:
                # Credential.kind=subscription can carry an API key envelope;
                # neither key authentication nor a provider name proves paid
                # billing. A plain api_key needs a known price: its Offering rate
                # card, else its model's maintained price; unknown never qualifies.
                declared_price = candidate.offering.get('actual_pricing')
                price = _project_route_cost(
                    declared_price, provider_id=candidate.provider_id,
                    model_id=candidate.model['model_id'],
                    input_tokens=input_tokens, output_tokens=output_tokens)
                maximum_price = _project_route_cost(
                    declared_price, provider_id=candidate.provider_id,
                    model_id=candidate.model['model_id'],
                    input_tokens=input_tokens, output_tokens=actual_capacity)
                # Owner decision 2026-11-02: no cost ceiling, which would refuse
                # long contexts. A paid main that can serve the summary is the
                # paid baseline, so only a strictly cheaper known price replaces
                # it; otherwise the cheapest known-price paid route leads.
                if (price is None or maximum_price is None
                        or (not subscription_main and main_serves_summary
                            and (main_cost is None or price[0] >= main_cost))):
                    continue
                cost, basis = price
                tier, economic_rank, maximum_cost = 0, cost, maximum_price[0]
            evidence = {
                'source': 'Artificial Analysis' if aa is not None else
                          'maintained_declared_rule' if declared else 'routing_admission',
                'metric': 'intelligence' if aa is not None else
                          'declared_capability' if declared else 'text_capability',
                'score': aa if aa is not None else profile.get('qualityScore') if declared else None,
                'match': 'exact' if aa is not None else 'declared' if declared else 'confirmed',
                'ranking': _RANKING, 'qualityTier': profile.get('quality', 'unknown'),
                'resourceTier': tier,
            }
            identity = route_identity(candidate, authority.revision)
            # Organization-billed paid APIs first, owner resources second. Known
            # paid cost or reference proxy first within each class; exact AA
            # next, declared lightness only as a fallback/tie breaker.
            rank = (tier, economic_rank, aa is None, -(aa or 0), quality_rank,
                    candidate.provider_id, candidate.offering['offering_id'],
                    candidate.deployment['deployment_id'], candidate.credential['credential_id'])
            candidates.append((rank, candidate, cost, basis, evidence, identity,
                               maximum_cost, actual_capacity))
    if not candidates:
        result['reason'] = 'no_qualified_authorized_auxiliary_route'
        return result
    leader = min(candidates, key=lambda row: row[0])
    usable = [row for row in candidates
              if health is None or not health.candidate_state(row[1])[0]]
    if leader not in usable:
        # Learned cooldown changes the plan, never the request identity: an
        # unchanged failed request must keep its SummaryBudget key and bound.
        _, lead, lead_cost, _, _, lead_identity, _, lead_capacity = leader
        result['_requestRoute'] = {
            'requestOwned': True, 'modelId': lead.model['model_id'],
            'providerId': lead.provider_id, 'routeIdentity': lead_identity,
            'declaredPricing': lead.offering.get('actual_pricing') if lead_cost is not None else None,
            'capacityOutputTokens': lead_capacity}
    if not usable:
        result['reason'] = 'no_qualified_authorized_auxiliary_route'
        return result
    _, candidate, cost, basis, evidence, identity, maximum_cost, actual_capacity = min(usable, key=lambda row: row[0])
    evidence['eligibleRoutes'] = len(usable)
    result.update(modelId=candidate.model['model_id'], providerId=candidate.provider_id,
                  requestOwned=True, routeIdentity=identity,
                  reason='authorized_auxiliary_resource_policy',
                  projectedCostUsd=cost, costBasis=basis, selectionBasis=evidence,
                  declaredPricing=candidate.offering.get('actual_pricing') if cost is not None else None,
                  maximumCostUsd=maximum_cost, inputTokens=input_tokens,
                  capacityOutputTokens=actual_capacity,
                  _parentRouteIdentities=parent_route_identities)
    return result


def _dispose_owned_auxiliary_group(group):
    """Join an incomplete release once, without hiding its first failure.

    ExitStack consumes even failed callbacks. The existing disposer explicitly
    leaves a failed group retryable; retain this exact group until its bounded
    cleanup completes. Observer-only failures after disposal never replay it.
    Cleanup is not model recovery and the original failure still prevents
    summary adoption or route requalification.
    """
    from lib.model_routing.dispatch_adapter import dispose_routed_slot_group

    # This group is already leaving its request lifecycle. A failed physical
    # detach cannot leave its OAuth resolver callable by an unrelated picker.
    # Only these owned slot objects are fenced; catalog/key health is untouched.
    for handle in group.handles:
        with handle.slot._lock:
            handle.slot.is_available = False
    try:
        dispose_routed_slot_group(group)
    except BaseException as first_error:
        if not group.disposed:
            try:
                dispose_routed_slot_group(group)
            except BaseException as cleanup_error:
                raise first_error from cleanup_error
        raise


def mint_auxiliary_route(route, *, owner, tenant, lifecycle):
    """Bind the discovery witness before resolving any credential plaintext.

    Selection alone consults learned owner health. A credential cooled by a
    concurrent request after selection is still minted and tried (dispatch
    demotes, never strands it); a second health filter here turned that race
    into a route-changed failure instead of the plan's own typed outcome.
    """
    from lib.model_routing import NativeModelSelection, RoutePolicy
    from lib.model_routing.domain import ProviderOfferingRef
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group
    from lib.llm_dispatch.provider_pin import provider_pin
    identity = route['routeIdentity']
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    group = mint_routed_slot_group(
        ModelRoutingRepository(), OwnerBoundary.create(owner, tenant),
        selection=NativeModelSelection(model=None, provider_offering=ProviderOfferingRef(
            identity['providerId'], identity['offeringId'])),
        policy=RoutePolicy(required_capabilities=frozenset({'text'}),
                           required_context=route['inputTokens'] + route['capacityOutputTokens']),
        max_candidates=1, expected_route_identity=identity)
    lifecycle.callback(_dispose_owned_auxiliary_group, group)
    if candidate_capacity(group.candidates[0], route['inputTokens'], route['capacityOutputTokens']) < route['capacityOutputTokens']:
        raise ValueError('planned route capacity changed')
    lifecycle.enter_context(provider_pin(group.pin_id))
    return group
