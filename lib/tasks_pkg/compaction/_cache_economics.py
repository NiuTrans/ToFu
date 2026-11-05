"""Shared cache rewrite/payback policy for optional L1 and L2 candidates.

One route-aware cost equation and bounded payback horizon, plus the exact
next-request admission for L1 edits of already-sent bytes; no message mutation,
provider calls or retained state. Candidate generation and adoption stay with
individual stages. Pricing and current-task cache evidence are existing owners.
"""
import math
from collections.abc import Mapping

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _AUTO_COMPACT_MIN_PAYBACK_ROUNDS
from lib.tasks_pkg.compaction._tokens import _fixed_compaction_cadence_payback_horizon

logger = get_logger(__name__)


_UNSET = object()


def _main_route(task: Mapping) -> tuple[str, str | None]:
    config = task.get('config') or {}
    config = config if isinstance(config, Mapping) else {}
    model = task.get('model') or config.get('model', '')
    provider_id = (task.get('provider_id') or config.get('provider_id')
                   or config.get('providerId') or None)
    return model, provider_id


def _main_route_rates(model, provider_id, *, prompt_tokens: int, exchange_rate):
    """Return ``(pricing, verified rates)`` or raise for unverified evidence."""
    from lib.tasks_pkg.compaction._summary_cost import (
        _lookup_verified_pricing, _verified_rates)
    pricing = _lookup_verified_pricing(model, provider_id, prompt_tokens=prompt_tokens)
    return pricing, _verified_rates(pricing, usd_to_cny=exchange_rate)


def main_route_pricing_verified(task: dict | None, *, prompt_tokens: int) -> bool:
    """Whether the current prompt has verified main-route rates.

    ``_proactive_cache_economics`` prices a candidate only after these exact
    current-prompt rates resolve. False therefore means ``pricing_known=False``
    and infinite payback for every candidate, summary estimate and route.
    """
    from lib.pricing import get_pricing_data

    model, provider_id = _main_route(task if isinstance(task, Mapping) else {})
    try:
        _main_route_rates(model, provider_id, prompt_tokens=max(0, int(prompt_tokens)),
                          exchange_rate=get_pricing_data().get('usdToCny'))
    except Exception as exc:
        logger.debug('[Compact] main-route pricing unverified: %s', exc)
        return False
    return True


def sent_step_rewrite_economics(task: dict | None, *, tokens_before: int,
                                tokens_after: int, prompt_tokens: int) -> dict:
    """Price an edit of the previous request's last tool step for the next request.

    Editing any byte the previous request sent makes the next request re-send
    that request's last tool step (the request before it cached everything
    earlier). Keeping the step reads ``tokens_before`` back at the cache-read
    rate; editing writes ``tokens_after`` at the cache-write rate. New content
    after the sent prefix is written either way and cancels, and both sides
    share one input rate, so token-equivalents compare exactly. Admit only a
    strictly cheaper next request on verified main-route rates.
    """
    from lib.pricing import get_pricing_data

    result = {'admitted': False, 'pricing_known': False,
              'tokens_before': max(0, int(tokens_before)),
              'tokens_after': max(0, int(tokens_after)),
              'cache_read_mul': None, 'cache_write_mul': None,
              'keep_cost_tokens': None, 'rewrite_cost_tokens': None,
              'reason': 'pricing_unverified'}
    model, provider_id = _main_route(task if isinstance(task, Mapping) else {})
    try:
        _, rates = _main_route_rates(
            model, provider_id, prompt_tokens=max(0, int(prompt_tokens)),
            exchange_rate=get_pricing_data().get('usdToCny'))
    except Exception as exc:
        logger.debug('[Compact] sent-step rewrite pricing unverified: %s', exc)
        return result
    keep = result['tokens_before'] * rates['cacheReadMul']
    rewrite = result['tokens_after'] * rates['cacheWriteMul']
    result.update({
        'admitted': rewrite < keep, 'pricing_known': True,
        'cache_read_mul': rates['cacheReadMul'],
        'cache_write_mul': rates['cacheWriteMul'],
        'keep_cost_tokens': keep, 'rewrite_cost_tokens': rewrite,
        'reason': 'cheaper_next_request' if rewrite < keep else 'rewrite_not_cheaper',
    })
    return result


def _proactive_cache_economics(task: dict | None, *, tokens_before: int,
                                candidate_tokens: int,
                                summary_usage_tokens: int = 0,
                                summary_usage=_UNSET,
                                summary_cost=_UNSET) -> dict:
    """Project all-in API-equivalent break-even, not token-percent savings.

    Resolve current/candidate tiers independently and compare only the observed
    warm prefix. Retain the existing full rewrite-cost equation. Summary usage
    wins over an explicit projected USD price; tokens alone never prove price.
    Omitting both summary arguments with zero tokens is a no-summary operation
    (L1 or adoption after generation). Explicit missing usage remains unknown.
    """
    from lib.pricing import get_pricing_data
    from lib.tasks_pkg.compaction._summary_cost import (
        _nonnegative_finite, summary_cost_details)
    from lib.tasks_pkg.compaction._cache_evidence import (
        current_cache_read, current_route_cache_cold,
        current_warm_prefix_tokens)

    task = task if isinstance(task, Mapping) else {}
    model, provider_id = _main_route(task)
    if summary_usage is not _UNSET:
        # The main route cannot fill holes in the summarizer's route evidence.
        actual_summary_cost = summary_cost_details(summary_usage)
    elif summary_cost is not _UNSET:
        actual_summary_cost = summary_cost
    elif summary_usage_tokens:
        actual_summary_cost = {'costUsd': None, 'reason': 'usage_unavailable'}
    else:
        actual_summary_cost = {'costUsd': 0.0, 'known': True}
    actual_summary_cost = (actual_summary_cost
                           if isinstance(actual_summary_cost, Mapping) else {})
    summary_usd = _nonnegative_finite(actual_summary_cost.get('costUsd'))
    if (actual_summary_cost.get('known') is False
            or actual_summary_cost.get('costBasis', 'metered_api_equivalent')
            != 'metered_api_equivalent'):
        summary_usd = None

    cache_read = current_cache_read(task)
    # Price what the next request can read back, including a fresh write;
    # the observed read alone calls a just-written prefix cold.
    warm_prefix = current_warm_prefix_tokens(task)
    total_before = max(0, int(tokens_before))
    total_after = max(0, int(candidate_tokens))
    dropped = max(0, total_before - total_after)
    cache_replay_before = min(total_before, warm_prefix)
    cache_replay_after = min(total_after, warm_prefix)
    # A route with no evidenced cache bills plain input too: it need not first
    # create a warm prefix and fail to read it back to earn uncached savings.
    # A healthy warm route can cache its new tail next round, so that tail
    # still cannot claim recurring plain-input savings.
    route_cold = current_route_cache_cold(task)
    uncached_dropped = (max(0, (total_before - cache_replay_before)
                            - (total_after - cache_replay_after))
                        if route_cold or warm_prefix == 0 else 0)
    result = {
        'cache_read_tokens': cache_read,
        'warm_prefix_tokens': warm_prefix,
        'route_cache_cold': route_cold,
        'uncached_dropped_tokens': uncached_dropped,
        'cache_replay_tokens_before': cache_replay_before,
        'cache_replay_tokens_after': cache_replay_after,
        'cache_rewrite_tokens': cache_replay_after,
        'dropped_tokens': dropped,
        'cache_write_mul': 1.0,
        'cache_read_mul': 1.0,
        'marginal_savings_per_dropped_token': 1.0,
        # These legacy token-equivalent fields remain for retry-floor callers.
        # Without route prices there is no meaningful money-to-token conversion.
        'rewrite_cost_tokens': cache_replay_after,
        'summary_cost_tokens': None,
        'savings_per_round_tokens': (max(0, cache_replay_before - cache_replay_after)
                                     + uncached_dropped),
        'rewrite_cost_usd': None,
        'summary_cost_usd': summary_usd,
        'summary_cost_known': summary_usd is not None,
        'summary_cost_basis': 'metered_api_equivalent',
        'savings_per_round_usd': None,
        'total_cost_usd': None,
        'payback_rounds': float('inf'),
        'pricing_source': 'conservative_default',
        'pricing_known': False,
    }
    try:
        exchange_rate = get_pricing_data().get('usdToCny')
        pricing_before, before = _main_route_rates(
            model, provider_id, prompt_tokens=total_before,
            exchange_rate=exchange_rate)
        pricing_after, after = _main_route_rates(
            model, provider_id, prompt_tokens=total_after,
            exchange_rate=exchange_rate)
        before_input_usd = (before['input'] / before['usdToCny']
                            if before['currency'] == 'CNY' else before['input'])
        after_input_usd = (after['input'] / after['usdToCny']
                           if after['currency'] == 'CNY' else after['input'])
        replay_before_usd = (
            cache_replay_before * before_input_usd * before['cacheReadMul']
            / 1_000_000)
        replay_after_usd = (
            cache_replay_after * after_input_usd * after['cacheReadMul']
            / 1_000_000)
        savings_usd = (max(0.0, replay_before_usd - replay_after_usd)
                       + uncached_dropped * after_input_usd / 1_000_000)
        rewrite_usd = (
            cache_replay_after * after_input_usd * after['cacheWriteMul']
            / 1_000_000)
        total_usd = rewrite_usd + summary_usd if summary_usd is not None else None
        if any(not math.isfinite(value) for value in (
                before_input_usd, after_input_usd, replay_before_usd,
                replay_after_usd, rewrite_usd, savings_usd)):
            raise ValueError('nonfinite price projection')
        if total_usd is not None and not math.isfinite(total_usd):
            raise ValueError('nonfinite total cost')
        reference_input_usd = after_input_usd or before_input_usd
        token_scale = 1_000_000 / reference_input_usd if reference_input_usd > 0 else None
        before_tier = pricing_before.get('selectedTier') or {}
        after_tier = pricing_after.get('selectedTier') or {}
        result.update({
            'cache_write_mul': after['cacheWriteMul'],
            'cache_read_mul': before['cacheReadMul'],
            'marginal_savings_per_dropped_token': (
                before_input_usd * before['cacheReadMul'] / reference_input_usd
                if reference_input_usd > 0 else 0.0),
            'rewrite_cost_tokens': rewrite_usd * token_scale if token_scale else 0.0,
            'summary_cost_tokens': (summary_usd * token_scale
                                    if summary_usd is not None and token_scale else None),
            'savings_per_round_tokens': savings_usd * token_scale if token_scale else 0.0,
            'rewrite_cost_usd': rewrite_usd,
            'savings_per_round_usd': savings_usd,
            'total_cost_usd': total_usd,
            'payback_rounds': (total_usd / savings_usd
                               if savings_usd > 0 and total_usd is not None
                               else float('inf')),
            'pricing_source': before['source'],
            'pricing_known': True,
            'pricing_before': {
                'model': model, 'provider_id': provider_id,
                'pricing_source': before['source'], 'currency': before['currency'],
                'usd_to_cny': before['usdToCny'],
                'tier_id': before_tier.get('id'),
                'input_rate_usd': before_input_usd,
                'cache_read_mul': before['cacheReadMul'],
            },
            'pricing_after': {
                'model': model, 'provider_id': provider_id,
                'pricing_source': after['source'], 'currency': after['currency'],
                'usd_to_cny': after['usdToCny'],
                'tier_id': after_tier.get('id'),
                'input_rate_usd': after_input_usd,
                'cache_read_mul': after['cacheReadMul'],
                'cache_write_mul': after['cacheWriteMul'],
            },
            'crosses_pricing_tier': before_tier.get('id') != after_tier.get('id'),
        })
    except Exception as exc:
        # Unknown money must not become free summarization or fictitious ROI.
        logger.debug('[Compact] cache pricing lookup failed: %s', exc)
    return result


def _proactive_payback_policy(
    task: dict | None,
    *,
    current_round: object = None,
    remaining_api_rounds: object = None,
) -> tuple[float, str]:
    """Return the exact-ROI horizon selected by the request strategy.

    ``adaptive`` already makes an observable expected-value decision before
    entering L2.  Reapplying the fixed one-round horizon inside L2 used to
    veto candidates that the adaptive decision had explicitly admitted.  Use
    that same bounded remaining-round horizon for both exact candidate checks.
    The fixed strategy starts with the shipped one-round rule, then earns a
    bounded wider horizon from observed prefix-rewrite cadence. The task's
    remaining hard API-round budget caps that horizon.
    """
    fixed = max(0.0, float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
    cfg = ((task or {}).get('config') or {}) if isinstance(task, dict) else {}
    comp_cfg = cfg.get('compaction') if isinstance(cfg, dict) else None
    if not (isinstance(comp_cfg, dict)
            and str(comp_cfg.get('strategy') or '').lower() == 'adaptive'):
        horizon = _fixed_compaction_cadence_payback_horizon(
            task,
            current_round,
            remaining_api_rounds=remaining_api_rounds,
        )
        if horizon > fixed:
            return horizon, 'fixed_compaction_cadence'
        return fixed, 'fixed_one_round'

    decision = (task or {}).get('_adaptiveCompactionDecision')
    if not (isinstance(decision, dict) and decision.get('shouldTrigger')):
        return fixed, 'fixed_one_round'
    try:
        horizon = float(decision.get('remainingRoundsMedian'))
    except (TypeError, ValueError, OverflowError):
        return fixed, 'fixed_one_round'
    if not math.isfinite(horizon) or horizon < fixed:
        return fixed, 'fixed_one_round'
    # Mirrors _adaptive_compaction_economics' established request bound.  The
    # private decision is still validated here rather than trusted blindly.
    return min(200.0, horizon), 'adaptive_expected_horizon'


