"""Construct and converge the detached L2 continuation projection.

Entry points: retained projection/restoration reserve shared by planning and
adoption, and converge_compaction_candidate. Reuses history/context owners and
the existing pairing-safe reactive truncator; never archives or adopts source
history. The caller retains cancellation and durable commit authority.
"""
from __future__ import annotations

from typing import Callable

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _SUMMARY_TRIGGER_RATIO
from lib.tasks_pkg.compaction._history import (
    coalesce_history_carriers, is_human_message, make_history_carrier,
)

# Preserve the entrypoint diagnostic stream across phase extraction.
logger = get_logger('lib.tasks_pkg.compaction._layer2._compact')


def _retained_projection(old_messages, recent_messages, anchor_msg, retained_entries, *, task=None):
    """Build the same attributed policy/user/execution shell for planning and adoption."""
    result = [m for m in old_messages if m.get('role') in ('system', 'developer')]
    wrapper = make_history_carrier(retained_entries)
    if wrapper:
        result.append(wrapper)
    current = next((m for m in reversed(recent_messages) if is_human_message(m)), None)
    for message in recent_messages:
        if is_human_message(message) and message is not current:
            kind = 'opening_request' if message is anchor_msg else 'earlier_request'
            result.append(make_history_carrier([
                {'kind': kind, 'content': message.get('content', '')}]))
        else:
            result.append(message)
    from lib.tasks_pkg.context_composer import compact_context_snapshots
    from lib.tasks_pkg.project_guidance import compact_project_guidance
    return compact_project_guidance(
        compact_context_snapshots(coalesce_history_carriers(result)), task)


def _with_restoration_reserve(candidate, source, *, task=None):
    """Account existing managed carriers that recomposition would restore."""
    from lib.tasks_pkg.context_composer import compact_context_snapshots
    from lib.tasks_pkg.project_guidance import compact_project_guidance
    kept = {id(m) for m in candidate}
    # Recomposition restores current snapshots, never every historical version.
    # Partial mixed carriers are copies; retire duplicate sections again after
    # joining so those siblings consume their budget exactly once. A freshly
    # carried checklist in the candidate supersedes the source's older state.
    return compact_project_guidance(compact_context_snapshots([
        m for m in compact_context_snapshots(source)
        if m.get('_contextComposer') and id(m) not in kept] + candidate), task)


def converge_compaction_candidate(
    messages: list, task: dict | None, *, usable: int, proactive: bool,
    options: dict, log_prefix: str, log_id: str, estimate_tokens: Callable,
    trigger_threshold: Callable, is_bounded: Callable,
) -> None:
    """Remeasure the complete candidate and apply existing window policy."""
    # ── SUCCESS-PATH CONVERGENCE CHECK (post-fold hot-tail overflow) ──
    #   The fold + summary succeeded, but ``_find_turn_boundary`` preserves the
    #   hot-tail rounds WHOLE. If those surviving rounds are themselves so large
    #   that the complete request — including its already-installed handoff — STILL
    #   exceeds the same trigger ceiling, sending it would only be bounded next
    #   round or by the reactive 413 net. In an OOM-kill-prone deploy the "next
    #   round" may never arrive, so converge NOW with the pairing-safe
    #   head-truncate rather than emit an over-window request. Measured with the
    #   SAME yardstick as ``_should_force_compact`` (authoritative count vs
    #   ``usable × _SUMMARY_TRIGGER_RATIO``) so the check and the trigger agree.
    def _project_tokens() -> tuple[int, str]:
        try:
            # Local import (same pattern as the _allow_ht block below): the
            # bare name was never bound in this module, so EVERY projection
            # raised NameError into the except and silently ran 'heuristic'.
            from lib.tasks_pkg.compaction._tokens import (
                _count_tokens_authoritative)
            return _count_tokens_authoritative(messages, task)
        except Exception as _pe:
            logger.debug('%s [Compact] convergence projection count failed '
                         '(%s) — using heuristic', log_prefix, _pe)
            return estimate_tokens(messages), 'heuristic'

    window_ceiling = int(usable * _SUMMARY_TRIGGER_RATIO)
    effective_trigger, _window_trigger, _working_set_trigger = (
        trigger_threshold(task))
    ceiling = (min(window_ceiling, int(effective_trigger))
               if proactive else window_ceiling)

    if options.get('_compaction_trigger') in {'working_set', 'window', 'dispatch_guard'}:
        from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
        _, admission_target = resolve_admission_budget(
            task, round_num=int(options.get('_compaction_round') or 0))
        ceiling = min(ceiling, admission_target)
    ceiling_kind = 'effective automatic target' if proactive else 'window safety'
    proj_tokens, proj_method = _project_tokens()
    if proj_tokens > ceiling and is_bounded(task):
        # A complete summary is not permission to delete the preserved tail.
        # Final rendered-prompt admission refuses an overfull request safely.
        logger.warning('%s [Compact] Complete candidate exceeds safety target; '
                       'retaining hot tail for final admission', log_prefix)
    elif proj_tokens > ceiling:
        logger.warning(
            '%s [Compact] Post-fold STILL over ceiling: projected=%d (via %s) '
            '> ceiling=%d (%s; usable=%d) — preserved hot-tail rounds are '
            'oversized; converging with pairing-safe head-truncate',
            log_prefix, proj_tokens, proj_method, ceiling, ceiling_kind, usable)
        from lib.tasks_pkg.compaction._reactive import _head_truncate
        _tok_pre = estimate_tokens(messages)
        _dropped = _head_truncate(
            messages, task,
            token_target=max(1, ceiling - max(0, proj_tokens - _tok_pre)),
            event_name='post_compact_converge')
        _tok_post = estimate_tokens(messages)
        logger.warning(
            '%s [Compact] Post-fold head-truncate dropped %d message(s): '
            'preserved-region tokens %d → %d (conv=%s round=%s)',
            log_prefix, _dropped, _tok_pre, _tok_post, log_id,
            (task.get('round_num') if task else '?'))
        # Re-measure. If STILL over (pathological: a single hot round bigger
        # than the window — head-truncate is floored at system_end+4 messages),
        # do NOT raise and abort the round. Log an ERROR so this boundary is
        # visible in error.log, and pass through: the reactive 413 net is the
        # last resort this round.
        proj_after, _ = _project_tokens()
        if proj_after > ceiling:
            logger.error(
                '%s [Compact] Post-fold convergence INCOMPLETE — projected still '
                '%d > ceiling=%d after head-truncate (dropped=%d; only the system '
                'prefix + an oversized hot round remain). Passing through; the '
                'reactive 413 net must bound this request. conv=%s round=%s',
                log_prefix, proj_after, ceiling, _dropped, log_id,
                (task.get('round_num') if task else '?'))
