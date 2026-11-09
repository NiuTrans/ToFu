"""Select whole L2 source units and reserve a complete summary projection.

Entry point: plan_compaction_sources. Dependencies: existing anchor/preservation
ports plus shared history, skill and admission owners. Planning never mutates
source messages or performs summary/archive I/O; refusal updates diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _SUMMARY_DISPATCH_MAX_TOKENS, _SUMMARY_MAX_TOKENS,
)
from lib.tasks_pkg.compaction._handoff import (
    insert_compaction_handoff, is_compaction_handoff, make_compaction_handoff,
)
from lib.tasks_pkg.compaction._history import collect_history_entries, history_entries

# Preserve the entrypoint diagnostic stream across phase extraction.
logger = get_logger('lib.tasks_pkg.compaction._layer2._compact')


@dataclass(frozen=True, slots=True)
class SourcePlanningPorts:
    """Bind the entrypoint's current policy/fault-injection dependencies."""
    plan_tail: Callable
    anchor_index: Callable
    retained_projection: Callable
    restoration_reserve: Callable


@dataclass(frozen=True, slots=True)
class CompactionSources:
    """Candidate source references; the caller retains adoption authority."""
    old_messages: list
    recent_messages: list
    cold_round_msgs: list
    anchor_msg: dict | None
    retained_entries: list
    summary_input: list
    dispatch_measure: Callable | None
    dispatch_target: int | None
    summary_output_budget: int | None
    continuity_handoff: dict | None = None
    # Oldest observation keys the narrative carries instead of the packet.
    continuity_fold: frozenset = frozenset()


def plan_compaction_sources(
    messages: list, task: dict | None, *, budget_tokens: int, max_turns: int,
    handoff_phase: str, recent_files_block: str, turn_diff_block: str,
    options: dict, result_meta: dict, tokens_before: int, log_prefix: str,
    ports: SourcePlanningPorts, planning_messages: list | None = None,
) -> CompactionSources | str:
    """Return planned sources or the unchanged-input refusal message."""
    source_messages = messages
    if planning_messages is not None:
        messages = planning_messages
    originals = {id(projected): original
                 for projected, original in zip(messages, source_messages)}
    msg_count_before = len(messages)
    preserve_allowance = budget_tokens
    boundary, recent_messages, cold_round_msgs = ports.plan_tail(
        messages, budget_tokens=budget_tokens, max_turns=max_turns)

    from lib.tasks_pkg.compaction._skills import active_skill_message_ids
    protected_skills = active_skill_message_ids(messages, task)
    def protect_skill_tail(recent, cold):
        recent_ids = {id(message) for message in recent} | protected_skills
        return ([message for message in messages if id(message) in recent_ids],
                [message for message in cold if id(message) not in protected_skills])
    recent_messages, cold_round_msgs = protect_skill_tail(recent_messages, cold_round_msgs)

    # Old handoffs feed the summarizer through source_messages, but do not
    # accumulate as execution shells in the next accepted projection.
    folded_handoffs = [m for m in recent_messages if is_compaction_handoff(m)]
    recent_messages = [m for m in recent_messages if not is_compaction_handoff(m)]


    if boundary >= len(messages):
        logger.error(
            '%s [Compact] REFUSING — no user message found to anchor preservation. '
            'msg_count=%d  tokens=%d  model=%s',
            log_prefix, msg_count_before, tokens_before,
            (task.get('config', {}) or {}).get('model', '?') if task else '?',
        )
        if isinstance(result_meta, dict):
            result_meta['compacted'] = False
        return ('Context compaction skipped — no user message found to '
                'anchor preservation. Messages preserved as-is.')

    old_messages = [message for message in messages[:boundary]
                    if id(message) not in protected_skills]
    anchor_idx = ports.anchor_index(messages)
    anchor_msg = messages[anchor_idx] if anchor_idx is not None else None
    retained_entries = collect_history_entries(
        old_messages, opening_message=anchor_msg)
    dispatch_measure = options.get('_dispatch_prompt_measure')
    dispatch_target = options.get('_dispatch_prompt_target')
    if (not callable(dispatch_measure)
            and options.get('_compaction_trigger') in {'working_set', 'window', 'dispatch_guard'}):
        from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
        from lib.tasks_pkg.compaction._tokens import _count_tokens_authoritative
        _, dispatch_target = resolve_admission_budget(
            task, round_num=int(options.get('_compaction_round') or 0))
        dispatch_measure = lambda candidate: _count_tokens_authoritative(candidate, task)[0]
    def selected_sources():
        # Original observations corresponding to folded L1 shells, in source
        # chronology. Hot execution remains verbatim and needs no second copy.
        selected = [message for message in old_messages
                    if message.get('role') not in ('system', 'developer')
                    and (message is not anchor_msg or history_entries(message))]
        folded_ids = {id(m) for m in selected + cold_round_msgs + folded_handoffs}
        return [originals[id(m)] for m in messages if id(m) in folded_ids]

    from lib.tasks_pkg.compaction._continuity import (
        build_continuity, fold_continuity, render_continuity)
    folded_observations = frozenset()
    def continuity_reserve():
        return render_continuity(fold_continuity(
            build_continuity(selected_sources(), task), folded_observations))

    try:
        continuity = continuity_reserve()
    except (ValueError, TypeError):
        result_meta.update(compacted=False, summaryFailureReason='continuity_invalid')
        return 'Compaction continuity could not be verified; original context preserved.'
    def continuation_reserve():
        return make_compaction_handoff(
            '## Context Compacted — Selective Summary\n\n'
            + recent_files_block + ('\n\n' + turn_diff_block if turn_diff_block else '')
            + '\n\n' + continuity, phase=handoff_phase)

    summary_output_budget = None
    if callable(dispatch_measure) and isinstance(dispatch_target, int):
        summary_output_budget = min(_SUMMARY_DISPATCH_MAX_TOKENS,
                                    max(_SUMMARY_MAX_TOKENS, dispatch_target // 4))
        # Plan BEFORE the one model call. Reserve its actual output ceiling,
        # deterministic decorations, tools (inside measure), history wrappers
        # and managed context. Smaller suffix trials only select source units;
        # they neither summarize nor mutate the original projection.
        from lib.tasks_pkg.context_composer.checklist import carry_checklist_through_compaction
        while True:
            preview = ports.retained_projection(
                old_messages, recent_messages, anchor_msg, retained_entries)
            insert_compaction_handoff(preview, continuation_reserve())
            carry_checklist_through_compaction(preview, task)
            retained_tokens = dispatch_measure(
                ports.restoration_reserve(preview, messages))
            reserved_tokens = retained_tokens + summary_output_budget
            if reserved_tokens <= dispatch_target:
                break
            next_budget = max(1, budget_tokens // 2)
            if budget_tokens <= 1:
                # Preferred transport allowance is not required state. At the
                # smallest whole-unit tail, use the remaining room when the
                # nominal summary target fits. Complete-output validation and
                # final candidate admission still reject truncated/overfull
                # summaries without changing the source.
                available_summary_tokens = dispatch_target - retained_tokens
                if available_summary_tokens >= _SUMMARY_MAX_TOKENS:
                    summary_output_budget = min(
                        summary_output_budget, available_summary_tokens)
                    reserved_tokens = retained_tokens + summary_output_budget
                    break
                # Folded state only grows; refusing here would end the
                # conversation for good. The oldest observations move to the
                # narrative so the newest fit the preservation allowance the
                # verbatim tail uses, and the tail is planned again under it.
                fold = frozenset() if folded_observations else _fold_unfit_continuity(
                    build_continuity(selected_sources(), task), preserve_allowance,
                    result_meta=result_meta, log_prefix=log_prefix)
                if not fold:
                    if isinstance(result_meta, dict):
                        result_meta.update(compacted=False,
                            summaryFailureReason='irreducible_prompt_floor',
                            plannedPromptTokens=reserved_tokens,
                            targetPromptTokens=dispatch_target)
                    return ('Context compaction refused before summary: required '
                            'execution state and summary output cannot fit. Messages preserved as-is.')
                folded_observations, next_budget = fold, preserve_allowance
            budget_tokens = next_budget
            boundary, recent_messages, cold_round_msgs = ports.plan_tail(
                messages, budget_tokens=budget_tokens, max_turns=max_turns)
            recent_messages, cold_round_msgs = protect_skill_tail(recent_messages, cold_round_msgs)
            folded_handoffs = [m for m in recent_messages if is_compaction_handoff(m)]
            recent_messages = [m for m in recent_messages if not is_compaction_handoff(m)]
            old_messages = [message for message in messages[:boundary]
                    if id(message) not in protected_skills]
            retained_entries = collect_history_entries(old_messages, opening_message=anchor_msg)
            continuity = continuity_reserve()
        if isinstance(result_meta, dict):
            result_meta.update(plannedPromptTokens=reserved_tokens,
                               targetPromptTokens=dispatch_target)
    summary_input = selected_sources()
    return CompactionSources(
        old_messages=old_messages,
        recent_messages=recent_messages,
        cold_round_msgs=cold_round_msgs,
        anchor_msg=anchor_msg,
        retained_entries=retained_entries,
        summary_input=summary_input,
        dispatch_measure=dispatch_measure,
        dispatch_target=dispatch_target,
        summary_output_budget=summary_output_budget,
        # Economic preflight is an optimistic floor: the packet is required;
        # recent-file/turn-diff decorations remain optional.
        continuity_handoff=make_compaction_handoff(continuity, phase=handoff_phase),
        continuity_fold=folded_observations,
    )


def _fold_unfit_continuity(state: dict, allowance: int, *, result_meta, log_prefix: str) -> frozenset:
    """Oldest observation keys to fold so the packet fits ``allowance`` tokens."""
    from lib.tasks_pkg.compaction._continuity import observations_to_fold, render_continuity
    from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens
    fold = observations_to_fold(state, lambda candidate: _estimate_msg_tokens(
        {'role': 'user', 'content': render_continuity(candidate)}) <= allowance)
    if fold:
        if isinstance(result_meta, dict):
            result_meta['continuityFoldedObservations'] = len(fold)
        logger.warning(
            '%s [Compact] Continuity packet of %d observations cannot fit beside the '
            'smallest execution tail; folding the oldest %d into the narrative so the '
            'newest fit the %d-token preservation allowance',
            log_prefix, len(state['observations']), len(fold), allowance)
    return fold
