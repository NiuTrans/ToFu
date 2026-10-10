"""Select an auxiliary summary route without granting new routing authority.

Entry point: select_summary_route(task, input_tokens, output_tokens). The default
'aa-cheapest' policy selects the highest exact AA Intelligence Index among the
minimum-cost eligible routes in the SAME execution/provider access scope.
'qualified-cheapest' is an alias; explicit 'main' disables substitution. AA is a
quality proxy, not proof of summary fidelity or speed. Profiles describe supply,
initialized dispatch slots prove access.
No slots, credentials, registrations, network probes or task state are created.

The caller must use strict_model=True and the returned execution provider pin.
Completion verification/adoption belongs to _layer2._summary, not this policy.
"""
from __future__ import annotations

import math
from collections import defaultdict

from lib.log import get_logger

logger = get_logger(__name__)

_MAIN = 'main'
_AA_CHEAPEST = 'aa-cheapest'
_CHEAPEST_ALIAS = 'qualified-cheapest'
_NON_CHAT = {'embedding', 'image_gen', 'transcription', 'tts'}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bucket_aa(bucket):
    """Lowest exact Intelligence Index across every reachable offering.

    configured_model_profiles uses cached_routing_scores, which owns freshness
    and exact canonical-model matching without inline network I/O. Ordinal role
    profiles remain other consumers' policy, not an AA-to-summary threshold.
    """
    evidence = []
    for _, profile in bucket['members']:
        scores = _mapping(profile.get('aaScores'))
        score = _number(scores.get('intelligence'))
        if (scores.get('source') != 'Artificial Analysis'
                or scores.get('match') != 'exact' or score is None):
            return None
        evidence.append((score, scores))
    return min(evidence, key=lambda row: (
        row[0], _number(row[1].get('fetched_at')) or 0,
        str(row[1].get('aa_slug') or ''))) if evidence else None


def _project_cost(profile, input_tokens, output_tokens):
    """Cold-cache upper estimate, using one tier for every token component.

    Do not compare blended prices or assume prefix reuse across models. When a
    declared cache write is dearer than uncached input, reserve the entire
    prompt at that write rate (not input PLUS write). Cache reads are zero.
    Missing/invalid prices are unknown, never free or a global-model estimate.
    """
    from lib.pricing import DEFAULT_USD_CNY_RATE
    from lib.pricing._provider import normalize_pricing, _choose_context_tier

    raw = _mapping(profile.get('declaredPricing'))
    if (not raw or raw.get('unit', 'per_million_tokens') != 'per_million_tokens'
            or any(key in raw for key in ('peak', 'peakMul', 'peakSchedule'))):
        return None
    tiers = raw.get('contextTiers') or []
    if not isinstance(tiers, list) or any(not isinstance(tier, dict) for tier in tiers):
        return None
    if tiers:
        limits = [_tokens(tier.get('maxPromptTokens')) for tier in tiers]
        if (not all(limits) or len(set(limits)) != len(limits)
                or input_tokens > max(limits)):
            return None
    # Validate before the shared resolver can coerce missing/NaN rates to zero.
    for row in tiers or [raw]:
        if (row.get('unit', raw.get('unit', 'per_million_tokens')) != 'per_million_tokens'
                or any(key in row for key in ('peak', 'peakMul', 'peakSchedule'))):
            return None
        if any(_number(row.get(key)) is None for key in ('input', 'output')):
            return None
        if str(row.get('currency', raw.get('currency', ''))).upper() not in {'USD', 'CNY'}:
            return None
        if tiers and (not _tokens(row.get('maxPromptTokens'))):
            return None
        for key in ('cache_read', 'cache_write', 'cacheReadMul', 'cacheWriteMul'):
            if key in row and _number(row[key]) is None:
                return None
    for key in ('cache_read', 'cache_write', 'cacheReadMul', 'cacheWriteMul'):
        if key in raw and _number(raw[key]) is None:
            return None
    # Empty model prevents unrelated legacy name-derived pricing tiers from
    # overriding an owner offering's explicit flat rate card.
    resolved = normalize_pricing('', raw)
    selected = _choose_context_tier(resolved.get('contextTiers'), input_tokens)
    if selected:
        resolved.update(selected)
    row = raw
    if tiers:
        ordered = sorted(tiers, key=lambda tier: tier['maxPromptTokens'])
        row = next((tier for tier in ordered if input_tokens <= tier['maxPromptTokens']), ordered[-1])
    input_rate = float(resolved['input'])
    output_rate = float(resolved['output'])

    def cache_rate(absolute, multiplier):
        if absolute in row:
            return float(row[absolute])
        if multiplier in row:
            return input_rate * float(row[multiplier])
        if absolute in raw:
            return float(raw[absolute])
        if multiplier in raw:
            return input_rate * float(raw[multiplier])
        return None

    write_rate = cache_rate('cache_write', 'cacheWriteMul')
    read_rate = cache_rate('cache_read', 'cacheReadMul')
    if any(_number(rate) is None for rate in
           (input_rate, output_rate, read_rate, write_rate) if rate is not None):
        return None
    currency = str(resolved.get('currency') or raw.get('currency')).upper()
    if currency == 'CNY' and not _number(DEFAULT_USD_CNY_RATE):
        return None
    conversion = 1.0 / DEFAULT_USD_CNY_RATE if currency == 'CNY' else 1.0
    use_write = write_rate is not None and write_rate > input_rate
    prompt_rate = write_rate if use_write else input_rate
    projected = (input_tokens * prompt_rate + output_tokens * output_rate) * conversion / 1e6
    if not math.isfinite(projected):
        return None
    return projected, {
        'source': 'declared_offering_pricing', 'estimated': True,
        'providerId': profile['providerId'], 'currency': currency,
        'unit': 'per_million_tokens',
        'usdToCny': DEFAULT_USD_CNY_RATE if currency == 'CNY' else None,
        'currencyRateSource': 'pricing_default' if currency == 'CNY' else 'identity',
        'tierId': selected.get('id') if selected else None,
        'promptTokens': input_tokens, 'outputTokens': output_tokens,
        'uncachedInputTokens': 0 if use_write else input_tokens,
        'cacheReadTokens': 0, 'cacheWriteTokens': input_tokens if use_write else 0,
        'cacheAssumption': 'cold_no_reuse_upper_bound',
        'rates': {'input': input_rate, 'output': output_rate,
                  'cacheRead': read_rate, 'cacheWrite': write_rate},
    }


def _authorized_buckets(dispatcher, profiles, *, owner, tenant, pin, provider):
    """Every slot reachable by the returned model+pin must pass the join.

    The picker cannot pin one offering inside an execution group. Reject an
    ambiguous group instead of qualifying just its cheapest/roomiest member.
    """
    index = defaultdict(list)
    for profile in profiles:
        provenance = _mapping(profile.get('provenance'))
        if (profile.get('enabled') is True
                and provenance.get('source') == 'model_routing_v2'
                and provenance.get('identityState') == 'confirmed'
                and provenance.get('ownerUserId') == owner
                and provenance.get('tenantId') == tenant):
            index[(profile.get('providerId'), provenance.get('offeringId'),
                   profile.get('modelId'))].append(profile)
    slots = list(dispatcher.slots)
    route_keys = set()
    for slot in slots:
        execution_pin = str(getattr(slot, 'provider_id', '') or '')
        if pin and execution_pin != pin:
            continue
        model = str(getattr(slot, 'logical_model', '') or getattr(slot, 'model', '') or '')
        route_keys.add((execution_pin, model))

    result = {}
    for key in sorted(route_keys):
        # The strict picker may resolve aliases or match exact wire names.
        # Qualifying only a hand-rolled logical-id grouping would miss a
        # different logical slot reachable through that wire-id spelling.
        matcher = dispatcher._prefer_matcher(key[1])
        if not callable(matcher):
            continue
        members = [slot for slot in slots if slot.provider_id == key[0] and matcher(slot)]
        if not members:
            continue
        joined = []
        scopes = set()
        for slot in members:
            caps = set(getattr(slot, 'capabilities', ()) or ())
            actual_provider = str(getattr(slot, 'routing_provider_id', '') or slot.provider_id)
            matches = index.get((actual_provider, getattr(slot, 'route_offering_id', ''), key[1]), [])
            if (not key[0] or not key[1]
                    or (provider and provider not in (key[0], actual_provider))
                    or getattr(slot, 'routing_owner_user_id', 0) != owner
                    or (getattr(slot, 'request_scoped', False) and not pin)
                    or 'text' not in caps or caps & _NON_CHAT or len(matches) != 1):
                break
            profile = matches[0]
            access = profile['provenance'].get('providerAccessId')
            if not access:
                break
            # Never move between subscription and paid API, even when both
            # were minted into one fallback group by ordinary task routing.
            billing = str(getattr(slot, 'oauth', '') or 'token_api')
            scopes.add((actual_provider, access, billing))
            joined.append((slot, profile))
        else:
            if len(scopes) == 1:
                result[key] = {'members': joined, 'scope': next(iter(scopes))}
    return result


def _fits(bucket, input_tokens, output_tokens):
    from lib.llm_dispatch.context_policy import slot_context_window

    for slot, profile in bucket['members']:
        window = _tokens(profile.get('contextWindow'))
        declared_slot_window = _tokens(getattr(slot, 'context_window', None))
        slot_window = _tokens(slot_context_window(slot)) if declared_slot_window else None
        output_limit = _tokens(getattr(slot, 'max_output_tokens', None))
        if (not window or not slot_window or not output_limit
                or output_tokens > output_limit
                or input_tokens + output_tokens > min(window, slot_window)):
            return False
    return True


def _bucket_prices(bucket, input_tokens, output_tokens):
    prices = [_project_cost(profile, input_tokens, output_tokens)
              for _, profile in bucket['members']]
    return prices if prices and all(price is not None for price in prices) else []


def select_summary_route(task, input_tokens, output_tokens, *,
                         capacity_output_tokens=None):
    """Return route, projected cost and optional AA selection evidence.

    providerId is the enforceable execution pin, not the billing-provider ID.
    Unknown prices stay None. Disabled/invalid policy or incomplete evidence
    retains the main route; this function never supplies a foreign fallback.
    output_tokens is the priced projection; capacity_output_tokens is the
    transport allowance, which must fit without inflating projected spend.
    """
    task = _mapping(task)
    config = _mapping(task.get('config'))

    capacity_output_tokens = (output_tokens if capacity_output_tokens is None
                              else capacity_output_tokens)
    compaction = _mapping(config.get('compaction'))
    requested = compaction.get('summaryPolicy', _AA_CHEAPEST)
    policy = (_AA_CHEAPEST if requested in (_AA_CHEAPEST, _CHEAPEST_ALIAS)
              else _MAIN)
    disabled = bool(config.get('disableModelFallback') or task.get('disableModelFallback'))
    if disabled:
        policy = _MAIN
    model = task.get('model') or config.get('model') or config.get('preset') or ''
    model = model if isinstance(model, str) else ''
    task_pin = str(task.get('_pinned_provider_id') or config.get('_pinned_provider_id') or '')
    provider = str(task.get('provider_id') or config.get('providerId') or config.get('provider_id') or '')
    result = {
        'modelId': model, 'providerId': task_pin or provider, 'policy': policy,
        'reason': ('main_explicit' if requested == _MAIN
                   else 'no_eligible_cheaper_aa_route' if policy == _AA_CHEAPEST
                   else 'invalid_summary_policy'),
        'projectedCostUsd': None, 'costBasis': {'source': 'unknown'},
        'selectionBasis': {},
    }
    try:
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pin = get_pinned_provider() or task_pin
        result['providerId'] = pin or provider
        config_pin = str(config.get('_pinned_provider_id') or '')
        if task_pin and (pin != task_pin or config_pin not in ('', task_pin)):
            result['providerId'] = task_pin
            result['reason'] = 'conflicting_provider_pins'
            return result
        if not model:
            result['reason'] = 'missing_main_model'
            return result
        if (_tokens(input_tokens) is None or not _tokens(output_tokens)
                or not _tokens(capacity_output_tokens)
                or capacity_output_tokens < output_tokens):
            result['reason'] = 'invalid_token_budget'
            return result
        from lib.tasks_pkg.manager import task_user_id
        # task_user_id may stamp a compatibility principal: never mutate the
        # parent's task during speculative compaction preflight.
        owner = task_user_id(dict(task))
        principal = _mapping(task.get('_principalContext'))
        tenant = principal.get('tenant_id', task.get('_tenant_id'))
        if task.get('_tenant_id') is not None and task['_tenant_id'] != tenant:
            result['reason'] = 'conflicting_tenant_identity'
            return result
        from lib.llm_dispatch import get_dispatcher
        from lib.model_profiles import configured_model_profiles
        profiles = configured_model_profiles(owner_user_id=owner, tenant_id=tenant)
        buckets = _authorized_buckets(
            get_dispatcher(), profiles, owner=owner, tenant=tenant,
            pin=pin, provider=provider)
        main_keys = sorted(key for key in buckets if key[1] == model)
        if len(main_keys) != 1:
            result['reason'] = 'main_route_unresolved'
            return result
        main_key = main_keys[0]
        main = buckets[main_key]
        result['providerId'] = main_key[0]

        restrictions = (task.get('provider_id'), config.get('providerId'),
                        config.get('provider_id'))
        if any(value and value not in (main_key[0], main['scope'][0])
               for value in restrictions):
            result['reason'] = 'conflicting_provider_pins'
            return result
        if main['scope'][2] != 'token_api':
            # A rate card does not establish marginal subscription cost.
            result.update(reason='subscription_route_preserved',
                          costBasis={'source': 'subscription_unpriced'})
            return result
        if not _fits(main, input_tokens, capacity_output_tokens):
            result['reason'] = 'main_capacity_unqualified'
            return result
        main_prices = _bucket_prices(main, input_tokens, output_tokens)
        if main_prices:
            cost, basis = max(main_prices, key=lambda price: price[0])
            result.update(projectedCostUsd=cost, costBasis=basis)
        if disabled:
            result.update(policy=_MAIN, reason='disable_model_fallback')
            return result
        if policy == _MAIN:
            return result
        if not main_prices:
            result['reason'] = 'main_price_unknown'
            return result
        candidates = []
        for key, bucket in sorted(buckets.items()):
            if (key == main_key or key[0] != main_key[0]
                    or bucket['scope'] != main['scope']):
                continue
            aa = _bucket_aa(bucket)
            if aa is None:
                continue
            if not _fits(bucket, input_tokens, capacity_output_tokens):
                continue
            prices = _bucket_prices(bucket, input_tokens, output_tokens)
            if not prices:
                continue
            # Compare worst reachable candidate cost against best reachable
            # main cost, since a provider pin cannot select one credential.
            cost, basis = max(prices, key=lambda price: price[0])
            if cost < min(price[0] for price in main_prices):
                candidates.append((cost, aa[0], key, basis, aa[1]))
        if not candidates:
            result['reason'] = 'no_eligible_cheaper_aa_route'
            return result
        # Cheapest means the exact minimum token-weighted projected cost, not
        # a hidden price band. AA maximizes the accepted proxy only within
        # that cost tier; route identity breaks remaining ties deterministically.
        cost, score, key, basis, scores = min(
            candidates, key=lambda item: (item[0], -item[1], item[2]))
        result.update(modelId=key[1], providerId=key[0],
                      reason='lowest_projected_cost_highest_aa',
                      projectedCostUsd=cost, costBasis=basis,
                      selectionBasis={
                          'source': 'Artificial Analysis', 'metric': 'intelligence',
                          'score': score, 'match': 'exact',
                          'ranking': 'cost_asc_aa_desc',
                          'eligibleRoutes': len(candidates),
                          'fetchedAt': _number(scores.get('fetched_at')),
                          'aaSlug': str(scores.get('aa_slug') or '')[:200],
                      })
        return result
    except Exception as exc:
        # Supply/identity/pricing failures never turn into permission to use
        # ambient models. Leave main admission to the strict dispatch caller.
        logger.debug('[SummaryRoute] main route retained: %s', exc)
        result['reason'] = 'route_evidence_unavailable'
        return result


__all__ = ['select_summary_route']
