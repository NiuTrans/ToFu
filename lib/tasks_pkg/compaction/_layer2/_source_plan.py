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
            insert_compaction_handoff(preview, make_compaction_handoff(
                '## Context Compacted — Selective Summary\n\n'
                + recent_files_block + ('\n\n' + turn_diff_block if turn_diff_block else ''),
                phase=handoff_phase))
            carry_checklist_through_compaction(preview, task)
            retained_tokens = dispatch_measure(
                ports.restoration_reserve(preview, messages))
            reserved_tokens = retained_tokens + summary_output_budget
            if reserved_tokens <= dispatch_target:
                break
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
                if isinstance(result_meta, dict):
                    result_meta.update(compacted=False,
                        summaryFailureReason='irreducible_prompt_floor',
                        plannedPromptTokens=reserved_tokens,
                        targetPromptTokens=dispatch_target)
                return ('Context compaction refused before summary: required '
                        'execution state and summary output cannot fit. Messages preserved as-is.')
            budget_tokens = max(1, budget_tokens // 2)
            boundary, recent_messages, cold_round_msgs = ports.plan_tail(
                messages, budget_tokens=budget_tokens, max_turns=max_turns)
            recent_messages, cold_round_msgs = protect_skill_tail(recent_messages, cold_round_msgs)
            folded_handoffs = [m for m in recent_messages if is_compaction_handoff(m)]
            recent_messages = [m for m in recent_messages if not is_compaction_handoff(m)]
            old_messages = [message for message in messages[:boundary]
                    if id(message) not in protected_skills]
            retained_entries = collect_history_entries(old_messages, opening_message=anchor_msg)
        if isinstance(result_meta, dict):
            result_meta.update(plannedPromptTokens=reserved_tokens,
                               targetPromptTokens=dispatch_target)
    # Keep source chronology for summary evidence. The opener is supplied
    # separately; previously retained entries stay available as attributed
    # history, never as a new human turn.
    summary_input = [message for message in old_messages
                     if message.get('role') not in ('system', 'developer')
                     and (message is not anchor_msg or history_entries(message))]
    folded_ids = {id(m) for m in summary_input + cold_round_msgs + folded_handoffs}
    # L1 may have reduced a consumed result to a recovery preview. Summarize
    # the original selected observations so errors, mutations and identifiers
    # still pass through the existing bounded evidence formatter exactly once.
    summary_input = [originals[id(m)] for m in messages if id(m) in folded_ids]
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
    )
