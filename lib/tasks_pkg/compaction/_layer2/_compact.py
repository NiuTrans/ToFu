"""Layer 2 — public entrypoints for query-aware context compaction.

  * ``execute_compact_tool``    — generates the summary, mutates messages.
  * ``force_compact_if_needed`` — gates on threshold + injects synthetic pair.
  * ``smart_summary_compact``   — legacy alias delegating to force-compact.
"""

import sys as _sys
import time

from lib.ids import short_id
from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _COMPACT_TOOL_NAME,
    _cooldown_lock,
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
    _SUMMARY_TRIGGER_RATIO,
    _summary_cooldowns,
)
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _should_force_compact,
    _usable_context,
)
from lib.tasks_pkg.compaction._layer2._anchor import (
    _extract_current_query,
    _extract_recently_accessed_files,
    _find_turn_boundary,
    _fold_recent_intra_turn,
    _objective_anchor_index,
)
from lib.tasks_pkg.compaction._layer2._summary import _generate_query_aware_summary

logger = get_logger(__name__)


# The old flat ``_layer2.py`` module exposed ``_archive_transcript`` and
# ``_generate_query_aware_summary`` as module-level globals, and tests (plus
# any hot-reload path) monkeypatch them on the package namespace
# ``lib.tasks_pkg.compaction._layer2``. Resolve those two through the facade
# package at CALL time (not import time) so such patches take effect exactly
# as they did against the flat module.
def _facade():
    return _sys.modules.get('lib.tasks_pkg.compaction._layer2')


def _archive_transcript_dyn(*args, **kwargs):
    fac = _facade()
    fn = getattr(fac, '_archive_transcript', _archive_transcript) if fac else _archive_transcript
    return fn(*args, **kwargs)


def _generate_query_aware_summary_dyn(*args, **kwargs):
    fac = _facade()
    fn = (getattr(fac, '_generate_query_aware_summary', _generate_query_aware_summary)
          if fac else _generate_query_aware_summary)
    return fn(*args, **kwargs)


def _objective_anchor_index_dyn(*args, **kwargs):
    # Same facade-resolution as above: the flat ``_layer2.py`` exposed
    # ``_objective_anchor_index`` as a module global, and both tests and any
    # hot-reload path monkeypatch it on the package namespace
    # ``lib.tasks_pkg.compaction._layer2``. Resolve through the facade at CALL
    # time so those patches take effect (an import-time-bound local reference
    # would ignore them — the exact facade-split drift this restores).
    fac = _facade()
    fn = (getattr(fac, '_objective_anchor_index', _objective_anchor_index)
          if fac else _objective_anchor_index)
    return fn(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: execute_compact_tool — pure LLM summary with selective turn compression
# ═══════════════════════════════════════════════════════════════════════════════

def execute_compact_tool(messages: list, task: dict | None = None, **kwargs) -> str:
    """Execute context compaction — force-injected by the orchestrator only.

    NOT in the model's tool list. The model never calls this voluntarily.
    Triggered when estimated tokens exceed 80% of usable context.

    Pure LLM summary approach with selective turn compression.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    # Optional out-param: caller passes a mutable dict to learn whether
    # messages were actually mutated. Stays False on every early-return
    # failure path; flipped to True only after the message list is
    # replaced.  reactive_compact relies on this so its head-truncate
    # safety net engages when the LLM summary comes back empty.
    _result_meta = kwargs.get('_result_meta') if kwargs else None
    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = False

    tokens_before = _estimate_total_tokens(messages)
    msg_count_before = len(messages)
    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)

    budget_override = kwargs.get('preserve_budget_tokens') if kwargs else None
    if budget_override is not None:
        budget_tokens = max(1, int(budget_override))
    else:
        budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))

    _krp = kwargs.get('keep_recent_pairs') if kwargs else None
    max_turns = _MAX_PRESERVE_TURNS if _krp is None else max(1, int(_krp))

    logger.info('%s [Compact] Starting  conv=%s  tokens=%d  usable=%d  messages=%d  '
                'budget=%d  max_turns=%d',
                pfx, log_id, tokens_before, usable, msg_count_before,
                budget_tokens, max_turns)

    current_query = _extract_current_query(messages)

    boundary = _find_turn_boundary(
        messages, budget_tokens=budget_tokens, max_turns=max_turns,
    )

    if boundary >= len(messages):
        logger.error(
            '%s [Compact] REFUSING — no user message found to anchor preservation. '
            'msg_count=%d  tokens=%d  model=%s',
            pfx, msg_count_before, tokens_before,
            (task.get('config', {}) or {}).get('model', '?') if task else '?',
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — no user message found to '
                'anchor preservation. Messages preserved as-is.')

    system_end = 0
    for i, m in enumerate(messages):
        if m.get('role') == 'system':
            system_end = i + 1
        else:
            break

    if boundary >= len(messages) - 0 and boundary <= system_end:
        logger.error(
            '%s [Compact] REFUSING — boundary=%d would preserve 0 live messages '
            '(system_end=%d, total=%d)',
            pfx, boundary, system_end, msg_count_before,
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — boundary calculation would '
                'preserve no live messages. Bailing out to prevent data loss.')

    with _cooldown_lock:
        _summary_cooldowns[conv_id] = time.time()

    _archive_id: int | None = None
    if not kwargs.get('_compaction_skip_archive'):
        _archive_id = _archive_transcript_dyn(
            conv_id, messages,
            trigger=kwargs.get('_compaction_trigger') or 'force',
            task=task,
            round_num=int((task.get('round_num') if task else 0) or 0),
            tokens_before=int(tokens_before or 0),
            msgs_before=int(msg_count_before or 0),
            reason=kwargs.get('_compaction_reason') or '',
            emit_event=True,
        )

    old_messages = messages[:boundary]
    recent_messages = messages[boundary:]

    # ★ OBJECTIVE ANCHOR — the first real user message is the north-star
    #   objective.  If it falls in the to-be-summarized ``old_messages`` it
    #   would be lossily paraphrased (and re-paraphrased every subsequent
    #   compaction → unbounded drift), so we PULL IT OUT and re-insert it
    #   verbatim exactly once, right after the system messages.  If it is
    #   already in ``recent_messages`` (short conversation) there is nothing to
    #   do — it's preserved as-is.  Because the anchor is a genuine existing
    #   message (not a synthesized prepend), a subsequent compaction finds the
    #   SAME message already at the front of ``recent_messages`` and never
    #   duplicates it — idempotent, byte-identical, cache-prefix-stable.
    anchor_idx = _objective_anchor_index_dyn(messages)
    anchor_msg = None
    if anchor_idx is not None and anchor_idx < boundary:
        anchor_msg = messages[anchor_idx]
        # Summarize everything old EXCEPT the anchor.
        old_messages = [m for k, m in enumerate(messages[:boundary])
                        if k != anchor_idx]
        logger.info('%s [Compact] Preserving objective anchor verbatim '
                    '(msg idx=%d) across summary', pfx, anchor_idx)

    # ── INTRA-TURN FOLD (single-giant-turn overflow) ──
    #   ``_find_turn_boundary`` ALWAYS preserves the current turn whole, so a
    #   single agentic turn (one user request answered with dozens of tool
    #   rounds) that fills the window on its own left ``recent_messages`` huge
    #   and ``old_messages`` tiny — summarizing only the old region barely
    #   shrank anything, and the automatic path could not reduce it at all
    #   (the structural gap the manual /compact 档B fold already fixed). Fold
    #   the COLD tool-call rounds OUT of the preserved region here too: keep
    #   the most-recent hot-tail rounds verbatim, and feed the cold rounds to
    #   the summarizer alongside ``old_messages``. Whole-round removal (shared
    #   ``_split_cold_rounds`` policy) can never orphan a ``tool`` message. A
    #   no-op when the preserved region has <= hot-tail tool-call rounds, so a
    #   normal multi-turn chat near the window is byte-identical to before.
    folded_recent, cold_round_msgs = _fold_recent_intra_turn(recent_messages)
    if cold_round_msgs:
        logger.info('%s [Compact] Intra-turn fold: %d cold round-message(s) '
                    'folded out of the preserved region (%d recent → %d kept)',
                    pfx, len(cold_round_msgs), len(recent_messages),
                    len(folded_recent))
    recent_messages = folded_recent
    summary_input = list(old_messages) + list(cold_round_msgs)

    # Nothing to summarize: no old region with real content AND the preserved
    # turn had too few tool-call rounds to fold (or is one fat non-tool
    # message). A summary_input of only leading ``system`` rows carries no
    # foldable history — summarizing it would waste a cheap-model call and
    # inject a contentless summary, so decline gracefully — mirrors the manual
    # path's "decline rather than risk a cross-message break".
    # _result_meta.compacted stays False so the reactive head-truncate net
    # still engages.
    if not any(m.get('role') != 'system' for m in summary_input):
        logger.info('%s [Compact] Nothing foldable — no old region and preserved '
                    'turn has <= hot-tail tool rounds; skipping', pfx)
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — no foldable history '
                '(preserved turn within the hot-round tail). '
                'Messages preserved as-is.')

    preserved_turns = sum(
        1 for m in recent_messages if m.get('role') == 'user'
    )

    logger.info('%s [Compact] Summarizing %d messages (%d old + %d cold intra-turn '
                'rounds), preserving %d recent (%d turns), query=%.100s',
                pfx, len(summary_input), len(old_messages), len(cold_round_msgs),
                len(recent_messages), preserved_turns, current_query)

    summary_text = _generate_query_aware_summary_dyn(
        summary_input, current_query, pfx, conv_id=conv_id, task=task
    )

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction attempted but summary generation failed. '
                'Messages preserved as-is.')

    recent_files = _extract_recently_accessed_files(messages)
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        summary_text += (
            f'\n\n### Recently Accessed Files\n'
            f'Use read_files to review current state if needed:\n'
            f'{file_list}'
        )

    system_msgs = []
    for msg in old_messages:
        if msg.get('role') == 'system':
            system_msgs.append(msg)
        else:
            break

    # Rebuild: system → [objective anchor, if it was in the summarized region]
    # → recent.  The anchor is placed immediately after the system block so the
    # model always sees the original goal at a stable position, and exactly
    # once (it was removed from ``old_messages`` above, so it isn't also inside
    # the summary text's source, and it is NOT in ``recent_messages`` because
    # anchor_idx < boundary).
    anchor_block = [anchor_msg] if anchor_msg is not None else []
    new_messages = list(system_msgs) + anchor_block + list(recent_messages)
    messages.clear()
    messages.extend(new_messages)

    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = True

    tokens_after = _estimate_total_tokens(messages)
    reduction_pct = (1 - tokens_after / max(1, tokens_before)) * 100

    logger.info('%s [Compact] Complete  conv=%s  '
                'tokens: %d → %d (%.0f%% reduction)  '
                'messages: %d → %d  summarized=%d old messages',
                pfx, log_id,
                tokens_before, tokens_after, reduction_pct,
                msg_count_before, len(messages),
                (boundary - len(system_msgs)) + len(cold_round_msgs))

    # ── Phase-C: record the 'saved' half of this L2 event's cache ROI ──
    # The following round's detect_cache_break completes it with the re-billed
    # cache_write. Best-effort; never let instrumentation break compaction.
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import record_l2_compaction
            record_l2_compaction(
                conv_id, tokens_before=int(tokens_before),
                tokens_after=int(tokens_after),
                msgs_before=int(msg_count_before), msgs_after=int(len(messages)))
        except Exception as _roi_e:
            logger.debug('%s [Compact] record_l2_compaction failed: %s', pfx, _roi_e)

    if _archive_id is not None:
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().update_archive_summary(
                _archive_id, summary_text or '', int(tokens_after), int(len(messages)))
        except Exception as _upd_e:
            logger.debug('[Compact] archive row update failed: %s', _upd_e)

        if task is not None:
            try:
                from lib.agent_core.events import EventType, build_event
                from lib.tasks_pkg.manager import append_event
                append_event(task, build_event(
                    EventType.COMPACTION_DONE,
                    archiveId=int(_archive_id),
                    convId=conv_id,
                    tokensAfter=int(tokens_after),
                    msgsAfter=int(len(messages)),
                    reductionPct=round(reduction_pct, 1),
                ))
            except Exception as _ev_e:
                logger.debug('[Compact] compaction_done emit failed: %s', _ev_e)

    result_parts = [
        '## Context Compacted — Selective Summary\n',
        f'Compressed {(boundary - len(system_msgs)) + len(cold_round_msgs)} '
        f'historical messages '
        f'({tokens_before:,} → {tokens_after:,} tokens, '
        f'{reduction_pct:.0f}% reduction)\n',
        summary_text,
    ]
    compact_result = '\n'.join(result_parts)

    # ── SUCCESS-PATH CONVERGENCE CHECK (post-fold hot-tail overflow) ──
    #   The fold + summary succeeded, but ``_find_turn_boundary`` preserves the
    #   hot-tail rounds WHOLE. If those surviving rounds are themselves so large
    #   that the PROJECTED request — the current ``messages`` PLUS the summary
    #   tool-pair the caller (``force_compact_if_needed``) will append — STILL
    #   exceeds the same trigger ceiling, sending it would only be bounded next
    #   round or by the reactive 413 net. In an OOM-kill-prone deploy the "next
    #   round" may never arrive, so converge NOW with the pairing-safe
    #   head-truncate rather than emit an over-window request. Measured with the
    #   SAME yardstick as ``_should_force_compact`` (authoritative count vs
    #   ``usable × _SUMMARY_TRIGGER_RATIO``) so the check and the trigger agree.
    _summary_pair = [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': '_conv_proj', 'type': 'function',
                         'function': {'name': _COMPACT_TOOL_NAME,
                                      'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': '_conv_proj',
         'name': _COMPACT_TOOL_NAME, 'content': compact_result},
    ]

    def _project_tokens() -> tuple[int, str]:
        try:
            return _count_tokens_authoritative(list(messages) + _summary_pair, task)
        except Exception as _pe:
            logger.debug('%s [Compact] convergence projection count failed '
                         '(%s) — using heuristic', pfx, _pe)
            return _estimate_total_tokens(messages), 'heuristic'

    ceiling = int(usable * _SUMMARY_TRIGGER_RATIO)
    proj_tokens, proj_method = _project_tokens()
    if proj_tokens > ceiling:
        logger.warning(
            '%s [Compact] Post-fold STILL over ceiling: projected=%d (via %s) '
            '> ceiling=%d (usable=%d × %.2f) — preserved hot-tail rounds are '
            'oversized; converging with pairing-safe head-truncate',
            pfx, proj_tokens, proj_method, ceiling, usable, _SUMMARY_TRIGGER_RATIO)
        from lib.tasks_pkg.compaction._reactive import _head_truncate
        _tok_pre = _estimate_total_tokens(messages)
        _dropped = _head_truncate(
            messages, task, reported_token_count=proj_tokens,
            event_name='post_compact_converge')
        _tok_post = _estimate_total_tokens(messages)
        logger.warning(
            '%s [Compact] Post-fold head-truncate dropped %d message(s): '
            'preserved-region tokens %d → %d (conv=%s round=%s)',
            pfx, _dropped, _tok_pre, _tok_post, log_id,
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
                pfx, proj_after, ceiling, _dropped, log_id,
                (task.get('round_num') if task else '?'))

    return compact_result


# ═══════════════════════════════════════════════════════════════════════════════
#  Force compact: inject context_compact tool call when over threshold
# ═══════════════════════════════════════════════════════════════════════════════

def force_compact_if_needed(messages: list, task: dict | None = None,
                            keep_recent_pairs: int | None = None,
                            preserve_budget_tokens: int | None = None,
                            *, force: bool = False,
                            **kwargs) -> bool:
    """Check token usage and force-inject a context_compact tool round if needed.

    Args:
        keep_recent_pairs: Legacy knob mapped to ``max_turns`` (turn-count cap).
        preserve_budget_tokens: Token budget for verbatim preservation.
        force: Skip the ``_should_force_compact`` threshold gate.

    Returns True if compaction was performed, False otherwise.
    """
    if not force and not _should_force_compact(messages, task):
        return False

    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.info('%s [ForceCompact] Injecting context_compact for conv=%s',
                pfx, conv_id[:8] if conv_id else '?')

    # ★ Surface the L2 summary as a live phase. Without this, the front-end
    #   spinner stays frozen on "Analyzing results…" for the several seconds
    #   the cheap-model summary call takes — the user can't tell the harness
    #   is busy compressing context rather than hung.
    if task is not None:
        try:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager import append_event
            append_event(task, build_event(
                EventType.PHASE, phase='compacting',
                detail='Compressing earlier context to fit the window…',
                detailKey='stream.phase.compactingWindow'))
        except Exception as _ph_e:
            logger.debug('%s [ForceCompact] phase emit failed: %s', pfx, _ph_e)

    _trigger = (kwargs.get('_compaction_trigger')
                if isinstance(kwargs, dict) else None) or 'force'
    _reason = (kwargs.get('_compaction_reason')
               if isinstance(kwargs, dict) else None) or ''
    _skip_archive = bool(kwargs.get('_compaction_skip_archive')
                         if isinstance(kwargs, dict) else False)
    _meta: dict = {}
    compact_result = execute_compact_tool(
        messages, task=task,
        keep_recent_pairs=keep_recent_pairs,
        preserve_budget_tokens=preserve_budget_tokens,
        _compaction_trigger=_trigger,
        _compaction_reason=_reason,
        _compaction_skip_archive=_skip_archive,
        _result_meta=_meta,
    )

    # If the summary LLM returned empty / compaction refused, the message
    # list was NOT mutated. Injecting a synthetic context_compact
    # tool-pair here would only grow the context and — worse — make the
    # caller (reactive_compact) believe compaction succeeded, skipping its
    # head-truncate safety net and looping the same oversized prompt back
    # to the API. Report failure so the caller can fall through.
    if not _meta.get('compacted'):
        # ★ Deterministic proactive safety net (fix for the OOM fatal loop).
        #   The summary LLM is the ONLY mechanism the proactive path had; on
        #   a vanilla/exported deploy the cheap-model dispatch can fail
        #   outright (no model tagged 'cheap', saturated single model,
        #   summary input itself too big). Historically force-compact then
        #   returned False and did nothing, so the context stayed pinned near
        #   the window every round — and the reactive head-truncate net never
        #   fired because the max_tokens clamp keeps the request just under
        #   the hard ceiling (no API rejection). Nothing bounded the context
        #   → unbounded re-send → OOM (SIGKILL).
        #
        #   So when the proactive pipeline opts in (_allow_head_truncate_fallback)
        #   AND we are genuinely over the usable window, fall through to the
        #   same last-resort _head_truncate the reactive path already trusts,
        #   right here. This is bounded, logged (audit_log
        #   'proactive_head_truncate') context loss — strictly better than a
        #   process death. The empty-summary→False contract is preserved for
        #   the NON-critical case (still headroom before the window): we only
        #   head-truncate when estimated input >= usable window.
        _allow_ht = bool(kwargs.get('_allow_head_truncate_fallback')
                         if isinstance(kwargs, dict) else False)
        if _allow_ht:
            try:
                from lib.tasks_pkg.compaction._tokens import (
                    _count_tokens_authoritative)
                _est_tokens, _tok_method = _count_tokens_authoritative(
                    messages, task)
            except Exception as _ce:
                logger.debug('%s [ForceCompact] authoritative count failed, '
                             'using heuristic: %s', pfx, _ce)
                _est_tokens = _estimate_total_tokens(messages)
                _tok_method = 'heuristic'
            _usable = _usable_context(_get_context_limit(task))
            if _est_tokens >= _usable:
                logger.warning(
                    '%s [ForceCompact] Summary failed AND context critically '
                    'over budget (est=%d via %s >= usable=%d) — falling back '
                    'to deterministic head-truncate so the context is bounded '
                    'without depending on the summary LLM',
                    pfx, _est_tokens, _tok_method, _usable)
                from lib.tasks_pkg.compaction._reactive import _head_truncate
                _dropped = _head_truncate(
                    messages, task,
                    reported_token_count=_est_tokens,
                    event_name='proactive_head_truncate')
                if _dropped:
                    # Context was bounded — surface as a real compaction so
                    # the pipeline notifies the cache tracker (prefix changed)
                    # and the round proceeds with a smaller prompt.
                    return True
                logger.warning(
                    '%s [ForceCompact] Head-truncate dropped 0 messages '
                    '(too few to shed) — reporting failure', pfx)
        logger.warning('%s [ForceCompact] Compaction did not mutate messages '
                       '(summary empty or refused) — reporting failure so the '
                       'caller can fall back', pfx)
        return False

    compact_call_id = short_id('compact_', 12)

    messages.append({
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': compact_call_id,
            'type': 'function',
            'function': {
                'name': _COMPACT_TOOL_NAME,
                'arguments': '{}',
            },
        }],
    })

    messages.append({
        'role': 'tool',
        'tool_call_id': compact_call_id,
        'name': _COMPACT_TOOL_NAME,
        'content': compact_result,
    })

    return True


def smart_summary_compact(messages: list, task: dict | None = None):
    """Legacy entry point — now delegates to force_compact_if_needed."""
    force_compact_if_needed(messages, task=task)
