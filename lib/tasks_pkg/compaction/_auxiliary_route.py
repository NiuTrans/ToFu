"""Owner-wide auxiliary summary selection, independent of the parent family.

The shared compiler owns eligibility. AA/declarations/reference cost rank usable
routes, never authorize them. Discovery is secret-free; dispatch binds one witness.
"""
from lib.tasks_pkg.compaction._summary_routing import _number, _tokens, _project_cost
from lib.model_routing.dispatch_adapter import candidate_route_identity as route_identity

PAID_SUMMARY_CEILING_USD = 0.05
_RESOURCE_KINDS = {'oauth', 'subscription', 'local_identity'}
_RANKING = 'authorized_resource_cost_then_aa'


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


def select_auxiliary_route(result, profiles, *, owner, tenant, input_tokens,
                           output_tokens, capacity_output_tokens,
                           paid_ceiling=PAID_SUMMARY_CEILING_USD, subscription_main=True,
                           main_cost=None, parent_offering_ids=(), parent_route_identities=()):
    from lib.model_routing import (ModelRoutingRepository, OwnerBoundary,
                                   NativeModelSelection, RoutePolicy)
    from lib.model_routing.routing import RouteCandidateCompiler
    from lib.model_routing.domain import ProviderOfferingRef
    from lib.tasks_pkg.compaction._summary_routing import _bucket_aa

    authority = ModelRoutingRepository().get(OwnerBoundary.create(owner, tenant))
    compiler = RouteCandidateCompiler(authority.document)

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
            actual_capacity = candidate_capacity(candidate, input_tokens, capacity_output_tokens)
            if actual_capacity < output_tokens:
                continue
            resource = candidate.credential.get('kind') in _RESOURCE_KINDS
            # Credential.kind=subscription can carry an API key envelope;
            # neither key authentication nor a provider name proves paid billing.
            # A plain api_key still requires known Offering prices, never MSRP.
            price = _project_cost({'providerId': candidate.provider_id,
                                   'declaredPricing': candidate.offering.get('actual_pricing')},
                                  input_tokens, output_tokens)
            maximum_price = _project_cost({'providerId': candidate.provider_id,
                                           'declaredPricing': candidate.offering.get('actual_pricing')},
                                          input_tokens, actual_capacity)
            quality_rank = {'light': 0, 'standard': 1, 'heavy': 2, 'frontier': 3}.get(
                profile.get('quality') if declared else None, 4)
            if resource:
                tier = 0
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
                if (price is None or maximum_price is None or paid_ceiling is None
                        or maximum_price[0] > paid_ceiling
                        or (not subscription_main and (
                            main_cost is None or price[0] >= main_cost))):
                    continue
                cost, basis = price
                tier, economic_rank = 1, cost
            evidence = {
                'source': 'Artificial Analysis' if aa is not None else
                          'maintained_declared_rule' if declared else 'routing_admission',
                'metric': 'intelligence' if aa is not None else
                          'declared_capability' if declared else 'text_capability',
                'score': aa if aa is not None else profile.get('qualityScore') if declared else None,
                'match': 'exact' if aa is not None else 'declared' if declared else 'confirmed',
                'ranking': _RANKING, 'qualityTier': profile.get('quality', 'unknown'),
                'resourceTier': tier, 'paidCeilingUsd': paid_ceiling,
            }
            identity = route_identity(candidate, authority.revision)
            # Already-authorized subscription/local resources first. Known
            # reference proxy or paid cost first within each resource class;
            # exact AA next, declared lightness only as a fallback/tie breaker.
            rank = (tier, economic_rank, aa is None, -(aa or 0), quality_rank,
                    candidate.provider_id, candidate.offering['offering_id'],
                    candidate.deployment['deployment_id'], candidate.credential['credential_id'])
            candidates.append((rank, candidate, cost, basis, evidence, identity,
                               maximum_price[0] if cost is not None else None, actual_capacity))
    if not candidates:
        result['reason'] = 'no_qualified_authorized_auxiliary_route'
        return result
    _, candidate, cost, basis, evidence, identity, maximum_cost, actual_capacity = min(candidates, key=lambda row: row[0])
    evidence['eligibleRoutes'] = len(candidates)
    result.update(modelId=candidate.model['model_id'], providerId=candidate.provider_id,
                  requestOwned=True, routeIdentity=identity,
                  reason='authorized_auxiliary_resource_policy',
                  projectedCostUsd=cost, costBasis=basis, selectionBasis=evidence,
                  declaredPricing=candidate.offering.get('actual_pricing') if cost is not None else None,
                  maximumCostUsd=maximum_cost, inputTokens=input_tokens,
                  capacityOutputTokens=actual_capacity)
    return result


def mint_auxiliary_route(route, *, owner, tenant, lifecycle):
    """Bind the discovery witness before resolving any credential plaintext."""
    from lib.model_routing import NativeModelSelection, RoutePolicy
    from lib.model_routing.domain import ProviderOfferingRef
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
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
    lifecycle.callback(dispose_routed_slot_group, group)
    if candidate_capacity(group.candidates[0], route['inputTokens'], route['capacityOutputTokens']) < route['capacityOutputTokens']:
        raise ValueError('planned route capacity changed')
    lifecycle.enter_context(provider_pin(group.pin_id))
    return group
