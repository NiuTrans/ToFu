"""Shared cache rewrite/payback policy for optional L1 and L2 candidates.

One route-aware cost equation and bounded payback horizon; no message mutation,
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
        _lookup_verified_pricing, _nonnegative_finite, _verified_rates,
        summary_cost_details)
    from lib.tasks_pkg.compaction._cache_evidence import current_cache_read

    task = task if isinstance(task, Mapping) else {}
    config = task.get('config') or {}
    config = config if isinstance(config, Mapping) else {}
    model = task.get('model') or config.get('model', '')
    provider_id = (task.get('provider_id') or config.get('provider_id')
                   or config.get('providerId') or None)
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
    total_before = max(0, int(tokens_before))
    total_after = max(0, int(candidate_tokens))
    dropped = max(0, total_before - total_after)
    cache_replay_before = min(total_before, max(0, cache_read))
    cache_replay_after = min(total_after, max(0, cache_read))
    result = {
        'cache_read_tokens': cache_read,
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
        'savings_per_round_tokens': max(0, cache_replay_before - cache_replay_after),
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
        pricing_before = _lookup_verified_pricing(
            model, provider_id, prompt_tokens=total_before)
        pricing_after = _lookup_verified_pricing(
            model, provider_id, prompt_tokens=total_after)
        exchange_rate = get_pricing_data().get('usdToCny')
        before = _verified_rates(pricing_before, usd_to_cny=exchange_rate)
        after = _verified_rates(pricing_after, usd_to_cny=exchange_rate)
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
        savings_usd = max(0.0, replay_before_usd - replay_after_usd)
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


