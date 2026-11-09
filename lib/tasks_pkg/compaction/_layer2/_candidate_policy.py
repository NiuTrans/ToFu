"""L2 candidate eligibility and cache economics before/after summary I/O.

Entry points: defers_for_unverified_pricing, admit_compaction_sources and
admit_compaction_candidate. Shared pricing, estimates and retry policy arrive
through explicit entrypoint ports.
These decisions update decline diagnostics/retry witnesses, never source history,
archives, provider calls or accepted continuation state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _AUTO_COMPACT_MIN_REDUCTION_RATIO, _SUMMARY_MAX_TOKENS,
    _SUMMARY_DISPATCH_MAX_TOKENS,
)
from lib.tasks_pkg.compaction._layer2._source_plan import CompactionSources
from lib.tasks_pkg.compaction._layer2._projection import (
    _retained_projection, _with_restoration_reserve,
)

# Preserve the entrypoint diagnostic stream across phase extraction.
logger = get_logger('lib.tasks_pkg.compaction._layer2._compact')


@dataclass(frozen=True, slots=True)
class EconomicPolicyPorts:
    """Bind current estimate, accounting and retry policies once per attempt."""
    estimate_tokens: Callable
    projected_summary_usage: Callable
    payback_policy: Callable
    defer_retry: Callable
    cache_economics: Callable
    summary_usage_tokens: Callable


@dataclass(frozen=True, slots=True)
class EconomicPreflight:
    active: bool
    payback_limit: float
    payback_policy: dict
    projected_summary_cost: int
    summary_route: dict | None


@dataclass(frozen=True, slots=True)
class CandidateEconomics:
    summary_cost_tokens: int
    accounting: dict | None
    adoption: dict | None


def proactive_economic_attempt(options: dict, *, tokens_before: int, usable: int) -> bool:
    """Safety is mandatory even when this entry came from the economic pipeline."""
    return bool(options.get('_proactive_economic')) and tokens_before < usable


def defers_for_unverified_pricing(
    task: dict | None, options: dict, *, tokens_before: int, usable: int,
    pricing_verified: Callable,
) -> bool:
    """Whether an optional attempt must defer whatever source or route it plans.

    Without verified main-route rates for the current prompt, every candidate's
    payback is infinite, so turn diffs, summary estimates and summary-route
    selection cannot change the deferral. Mandatory attempts never defer here.
    """
    return (proactive_economic_attempt(options, tokens_before=tokens_before, usable=usable)
            and not pricing_verified(task, prompt_tokens=tokens_before))


def admit_compaction_sources(
    messages: list, task: dict | None, sources: CompactionSources, *,
    options: dict, tokens_before: int, usable: int, current_query: str,
    anchor_text: str, result_meta: dict, log_prefix: str, ports: EconomicPolicyPorts,
    unverified_pricing_deferral: bool = False,
) -> EconomicPreflight | str:
    """Decline empty/uneconomic source before buying a summary.

    ``unverified_pricing_deferral`` is the attempt's single
    ``defers_for_unverified_pricing`` verdict; it skips route selection.
    """
    _proactive = proactive_economic_attempt(
        options, tokens_before=tokens_before, usable=usable)
    _payback_limit, _payback_policy = ports.payback_policy(
        task,
        current_round=options.get('_compaction_round'),
        remaining_api_rounds=options.get('_compaction_remaining_api_rounds'),
    )

    # Nothing to summarize: no old region with real content AND the preserved
    # turn had too few tool-call rounds to fold (or is one fat non-tool
    # message). A summary_input of only leading ``system`` rows carries no
    # foldable history — summarizing it would waste a cheap-model call and
    # inject a contentless summary, so decline gracefully — mirrors the manual
    # path's "decline rather than risk a cross-message break".
    # _result_meta.compacted stays False so the reactive head-truncate net
    # still engages.
    if not any(m.get('role') != 'system' for m in sources.summary_input):
        # The dispatch-guard admission pass MUST leave the prompt under its
        # ceiling.  When the whole conversation is one giant in-progress
        # turn (every message sits inside the preserved hot tail), a summary
        # has nothing to fold — but refusing here surfaces a fatal
        # ContextCompactionError for a turn the user is still watching.
        # Fall through so force_compact_if_needed's over-window deterministic
        # head-truncate can bound the context instead.
        from lib.tasks_pkg.compaction._summary_budget import (
            bounded_summary_task as _bst_gate)
        if (options.get('_compaction_trigger') == 'dispatch_guard'
                and options.get('_allow_head_truncate_fallback')
                and not _bst_gate(task)):
            logger.info('%s [Compact] dispatch_guard: nothing foldable — '
                        'deferring to the over-window deterministic head '
                        'truncate (context must be bounded)', log_prefix)
            return ''
        logger.info('%s [Compact] Nothing foldable — no old region and preserved '
                    'turn has <= hot-tail tool rounds; skipping', log_prefix)
        if isinstance(result_meta, dict):
            result_meta.update({
                'compacted': False,
                'reason': 'nothing_foldable',
            })
        if _proactive:
            ports.defer_retry(
                task, tokens_before, reason='nothing_foldable')
        return ('Context compaction skipped — no foldable history '
                '(preserved turn within the hot-round tail). '
                'Messages preserved as-is.')

    if not _proactive:
        # Manual/window safety does not consume a foldable-region ROI estimate.
        return EconomicPreflight(False, _payback_limit, _payback_policy, 0, None)

    # Proactive L2 must earn the cache-prefix rewrite. Project the foldable
    # region vanishing while charging the quoted summary call; the quote
    # prices the request dispatch sends, which revalidation re-quotes.
    # This keeps an uneconomic attempt from buying a summary and rejecting it
    # only afterwards. Forced manual/reactive paths bypass this because their
    # goal is user intent or window correctness.
    _foldable_tokens = ports.estimate_tokens(sources.summary_input)
    _projected_summary_cost = ports.projected_summary_usage(
        sources.summary_input, current_query, task,
        anchor_text=anchor_text,
        source_messages=messages,
        **({'continuity_fold': sources.continuity_fold} if sources.continuity_fold else {}))
    # Managed snapshots are restored later. Do not sell their removal as
    # permanent savings, even during optimistic preflight.
    retained = _retained_projection(
        sources.old_messages, sources.recent_messages, sources.anchor_msg,
        sources.retained_entries, task=task)
    if sources.continuity_handoff is not None:
        from lib.tasks_pkg.compaction._handoff import insert_compaction_handoff
        insert_compaction_handoff(retained, sources.continuity_handoff)
    _best_candidate_tokens = ports.estimate_tokens(
        _with_restoration_reserve(retained, messages, task=task))
    _restored_context_reserve = max(0, _best_candidate_tokens - ports.estimate_tokens(retained))
    if unverified_pricing_deferral:
        # No route price can repay a rewrite whose current input is unpriced.
        _projected_route = {'reason': 'main_route_pricing_unverified'}
    else:
        from lib.tasks_pkg.compaction._summary_routing import select_summary_route
        _projected_route = select_summary_route(
            task or {}, input_tokens=max(
                0, _projected_summary_cost - _SUMMARY_MAX_TOKENS),
            output_tokens=_SUMMARY_MAX_TOKENS,
            capacity_output_tokens=(sources.summary_output_budget or _SUMMARY_DISPATCH_MAX_TOKENS))
    _best_econ = ports.cache_economics(
        task, tokens_before=tokens_before,
        candidate_tokens=_best_candidate_tokens,
        summary_usage_tokens=_projected_summary_cost,
        summary_cost={'costUsd': _projected_route.get('projectedCostUsd')})
    _best_econ.update({
        'payback_limit_rounds': _payback_limit,
        'restored_context_reserve_tokens': _restored_context_reserve,
        'candidate_measurement_scope': 'compaction_result_plus_restore_reserve',
        'payback_policy': _payback_policy,
    })
    _best_reduction = (_best_econ['dropped_tokens']
                       / max(1, tokens_before))
    if _best_reduction < _AUTO_COMPACT_MIN_REDUCTION_RATIO:
        logger.info(
            '%s [Compact] Proactive low-yield decline before summary: '
            'foldable=%d total=%d projected_reduction=%.1f%% < %.1f%%',
            log_prefix, _foldable_tokens, tokens_before,
            _best_reduction * 100,
            _AUTO_COMPACT_MIN_REDUCTION_RATIO * 100)
        if isinstance(result_meta, dict):
            result_meta.update({
                'compacted': False,
                'reason': 'low_yield',
                'foldable_tokens': _foldable_tokens,
                'projected_reduction_ratio': _best_reduction,
            })
        ports.defer_retry(
            task, tokens_before, reason='low_yield',
            economics=_best_econ)
        return ('Context compaction skipped — foldable history is below '
                'the automatic minimum reduction. Messages preserved '
                'as-is.')
    if unverified_pricing_deferral or _best_econ['payback_rounds'] > _payback_limit:
        logger.info(
            '%s [Compact] Proactive uneconomic decline before summary: '
            'foldable=%d total=%d cache_read=%d projected_rewrite=%d '
            'summary_estimate=%d payback=%.2f rounds > %.2f '
            'pricing_known=%s summary_cost_known=%s route_reason=%s '
            '— deferring optional summary',
            log_prefix, _foldable_tokens, tokens_before,
            _best_econ['cache_read_tokens'],
            _best_econ['cache_rewrite_tokens'],
            _projected_summary_cost,
            _best_econ['payback_rounds'],
            _payback_limit, _best_econ.get('pricing_known'),
            _best_econ.get('summary_cost_known'),
            _projected_route.get('reason', 'unknown'))
        if isinstance(result_meta, dict):
            result_meta.update({
                'compacted': False,
                'reason': 'cache_negative',
                'foldable_tokens': _foldable_tokens,
                'projected_summary_cost_tokens': _projected_summary_cost,
                'economics': _best_econ,
            })
        ports.defer_retry(
            task, tokens_before, reason='cache_negative',
            economics=_best_econ)
        if (_best_econ.get('pricing_known') is False
                or _best_econ.get('summary_cost_known') is False):
            return ('Context compaction deferred — verified pricing evidence '
                    'is incomplete for the input or summary route. '
                    'Messages preserved as-is.')
        return ('Context compaction skipped — foldable history cannot '
                'repay the warm-cache rewrite. Messages preserved as-is.')
    return EconomicPreflight(
        active=True, payback_limit=_payback_limit,
        payback_policy=_payback_policy,
        projected_summary_cost=_projected_summary_cost,
        summary_route=_projected_route,
    )


def admit_compaction_candidate(
    messages: list, new_messages: list, task: dict | None,
    policy: EconomicPreflight, *, candidate_tokens: int, tokens_before: int,
    summary_usage: dict, result_meta: dict, log_prefix: str, ports: EconomicPolicyPorts,
) -> CandidateEconomics | str:
    """Charge paid summary work separately from the future adoption decision."""
    if not policy.active:
        # Keep paid usage accounting; restoration estimates here serve ROI only.
        return CandidateEconomics(ports.summary_usage_tokens(summary_usage), None, None)

    # Share the actual restoration projection with capacity admission. Count
    # current managed sections once, including surviving mixed-carrier siblings;
    # inactive project scopes no longer return. No provider reads or task writes.
    _restored_context_reserve = max(0, ports.estimate_tokens(
        _with_restoration_reserve(new_messages, messages, task=task)) - candidate_tokens)
    _economic_candidate_tokens = candidate_tokens + _restored_context_reserve

    _summary_cost_tokens = ports.summary_usage_tokens(summary_usage)
    _accounting_econ = ports.cache_economics(
        task, tokens_before=tokens_before,
        candidate_tokens=_economic_candidate_tokens,
        summary_usage_tokens=_summary_cost_tokens,
        summary_usage=summary_usage)
    # Once generated, summary tokens are sunk. Adoption must compare only
    # future cache rewrite cost with future savings; otherwise a paid,
    # faithful candidate can be discarded and regenerated on a later turn.
    _adoption_econ = ports.cache_economics(
        task, tokens_before=tokens_before,
        candidate_tokens=_economic_candidate_tokens,
        summary_usage_tokens=0)
    for _economics in (_accounting_econ, _adoption_econ):
        _economics['restored_context_reserve_tokens'] = _restored_context_reserve
        _economics['candidate_measurement_scope'] = 'compaction_result_plus_restore_reserve'
        _economics.update({
            'payback_limit_rounds': policy.payback_limit,
            'payback_policy': policy.payback_policy,
        })
    _realized_reduction = (_accounting_econ['dropped_tokens']
                           / max(1, tokens_before))
    if _realized_reduction < _AUTO_COMPACT_MIN_REDUCTION_RATIO:
        logger.info(
            '%s [Compact] Proactive candidate rejected: projected=%d '
            'before=%d realized_reduction=%.1f%% < %.1f%% — preserving '
            'messages and cache prefix',
            log_prefix, candidate_tokens, tokens_before,
            _realized_reduction * 100,
            _AUTO_COMPACT_MIN_REDUCTION_RATIO * 100)
        if isinstance(result_meta, dict):
            result_meta.update({
                'compacted': False,
                'reason': 'low_yield',
                'candidate_tokens': int(candidate_tokens),
                'realized_reduction_ratio': _realized_reduction,
                'economics': _accounting_econ,
                'adoptionEconomics': _adoption_econ,
            })
        ports.defer_retry(
            task, tokens_before, reason='low_yield',
            economics=_accounting_econ)
        return ('Context compaction skipped — generated summary is below '
                'the automatic minimum reduction. Messages preserved '
                'as-is.')
    if (_adoption_econ['warm_prefix_tokens'] > 0
            and _adoption_econ['payback_rounds']
            > policy.payback_limit):
        logger.info(
            '%s [Compact] Proactive candidate rejected: projected=%d '
            'before=%d dropped=%d cache_read=%d rewrite=%d '
            'summary_cost_sunk=%d future_payback=%.2f rounds > %.2f — '
            'preserving cache prefix',
            log_prefix, candidate_tokens, tokens_before,
            _adoption_econ['dropped_tokens'],
            _adoption_econ['cache_read_tokens'],
            _adoption_econ['cache_rewrite_tokens'],
            _summary_cost_tokens, _adoption_econ['payback_rounds'],
            policy.payback_limit)
        if isinstance(result_meta, dict):
            result_meta.update({
                'compacted': False,
                'reason': 'cache_negative',
                'candidate_tokens': int(candidate_tokens),
                'economics': _accounting_econ,
                'adoptionEconomics': _adoption_econ,
            })
        ports.defer_retry(
            task, tokens_before, reason='cache_negative',
            economics=_accounting_econ)
        return ('Context compaction skipped — generated summary would not '
                'repay the warm-cache rewrite. Messages preserved as-is.')
    return CandidateEconomics(
        summary_cost_tokens=_summary_cost_tokens,
        accounting=_accounting_econ, adoption=_adoption_econ,
    )
