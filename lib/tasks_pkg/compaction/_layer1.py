"""Layer 1 — micro_compact: per-round compaction of cold tool results
+ thinking blocks + cold images + (optional) paired interstitials +
(optional) cold assistant content.

Runs every round (cheap, no LLM call).  Cache-aware: the previous request's
messages are the provider-cached prefix and stay byte-identical, except that
the last tool step inside them may be edited when that makes the very next
request cheaper (``_sent_step_boundary``).

Architecture (2026-06 step refactor):
    ``micro_compact`` is the *orchestration shell* — it computes the
    cache-prefix boundary and emits request-local compaction evidence. The actual
    *transforms* (the former Phase A–D) live as registered steps in
    ``_builtin_steps.py`` and run through ``_steps.run_steps`` against a
    :class:`CompactionContext`.  This makes the compression methods
    orderable / ablatable / replaceable purely by configuration without
    touching the shell.

Critical L1 invariant: settled conversation turns are immutable. L1 transforms
only the request-local API projection; it never loads or updates transcript
authority and never stamps compaction fields onto durable tool rounds.
"""

import copy
import time

from lib.log import get_logger
import lib.tasks_pkg.compaction._constants as compaction_constants

logger = get_logger(__name__)


def _sent_step_boundary(messages: list, sent_count: int, step_names: list, *,
                        conv_id: str, task: dict | None,
                        constants) -> tuple[int, int | None]:
    """Return ``(cache_prefix_count, rewrite_from)`` inside the previous request.

    messages[0:sent_count] were sent and cached by the previous request.
    Editing any of them makes the next request re-send that request's last
    tool step, messages[step_start:sent_count] with step_start the last
    assistant message in the sent prefix; the request before it cached
    everything earlier. A detached dry run of the same steps with only that
    step unprotected shows what L1 would change there. The edit is admitted
    only when writing the edited step costs less than reading the original
    back (``sent_step_rewrite_economics``); otherwise everything sent stays
    byte-identical. ``rewrite_from`` is set only for an admitted edit.
    """
    step_start = next((index for index in range(sent_count - 1, -1, -1)
                       if messages[index].get('role') == 'assistant'), None)
    if step_start is None:
        return sent_count, None
    from lib.tasks_pkg.compaction._steps import CompactionContext, run_steps
    # The trial must leave the live list untouched. Below step_start a shallow
    # copy suffices: protected steps never edit there, and a step that ignores
    # the prefix (compact_cold_assistant) only reassigns top-level fields.
    candidate = ([dict(message) for message in messages[:step_start]]
                 + copy.deepcopy(messages[step_start:]))
    run_steps(step_names, CompactionContext(
        messages=candidate, conv_id=conv_id, task=task, constants=constants,
        cache_prefix_count=step_start, dry_run=True))
    if candidate[step_start:sent_count] == messages[step_start:sent_count]:
        return sent_count, None

    from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id
    from lib.tasks_pkg.compaction._cache_economics import (
        _main_route, sent_step_rewrite_economics)
    from lib.tasks_pkg.compaction._tokens import (
        _estimate_total_tokens, estimate_msg_tokens_for_model)
    model = _main_route(task or {})[0]
    before = sum(estimate_msg_tokens_for_model(message, model)
                 for message in messages[step_start:sent_count])
    after = sum(estimate_msg_tokens_for_model(message, model)
                for message in candidate[step_start:sent_count])
    verdict = sent_step_rewrite_economics(
        task, tokens_before=before, tokens_after=after,
        prompt_tokens=_estimate_total_tokens(messages))
    if verdict['pricing_known']:
        priced = 'rewrite %.0f (write x%.2f) %s keep %.0f (read x%.2f)' % (
            verdict['rewrite_cost_tokens'], verdict['cache_write_mul'],
            '<' if verdict['admitted'] else '>=',
            verdict['keep_cost_tokens'], verdict['cache_read_mul'])
    else:
        priced = 'pricing unverified'
    logger.info('[L1] conv=%s sent-step edit %s: msgs[%d:%d] ~%d -> ~%d tokens; %s',
                _log_id(conv_id), 'admitted' if verdict['admitted'] else 'refused',
                step_start, sent_count, before, after, priced)
    if verdict['admitted']:
        return step_start, step_start
    return sent_count, None


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 1 — Micro-compaction (orchestration shell)
# ═══════════════════════════════════════════════════════════════════════════════

def micro_compact(messages: list, conv_id: str = '', task: dict | None = None,
                  **kwargs) -> int:
    """Compress cold tool results AND strip old thinking blocks.

    The transforms run through the compaction step registry; this
    function owns only request-local bookkeeping and observability.

    Cache-aware: the messages the previous request sent (tracked by
    cache_tracking) are the provider-cached prefix and are left
    byte-identical. The one exception is that request's last tool step, which
    is edited only when the dry-run edit makes the next request cheaper on
    verified main-route prices; such an edit is advertised to the cache-break
    detector as a bounded rewrite. ``ignore_cache_prefix`` arms skip both.

    Args:
        messages: The live messages list.  Mutated in place.
        conv_id:  Conversation ID for logging.
        task:     Live task dict. When provided, micro_compact emits a
                  request-local ``tool_compacted`` event; no transcript or
                  tool-round projection is changed.

    Keyword Args:
        steps: Explicit ordered list of compaction step names to run.
            When omitted, the default ordering is built from the gates
            below — reproducing the historical phase order:
            ``strip_thinking → compact_tool_results
            → [fold_paired_interstitial] → strip_cold_images
            → [compact_cold_assistant]``.
        enable_assistant_compact: If True, append ``compact_cold_assistant``
            (Phase D).  Disabled by default because A/B testing proved it
            invalidates prompt cache — only enable when cache rebuild is
            already expected (e.g. during force_compact / reactive).
        enable_paired_assistant_compact: If True, insert
            ``fold_paired_interstitial`` (Phase B2) right after
            ``compact_tool_results``.  A/B-verified -1.4% cache writes
            vs B-only (2026-04-27).
        keep_refetched_results: If True, ``compact_tool_results`` prefers
            re-fetched results inside the same hot budget (optional
            pressure only; see ``CompactionContext``).

    Returns:
        Estimated number of tokens saved.
    """
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task, conv_id)

    # Constants have one concrete owner. Per-request experiment changes use
    # ``constant_overrides`` below instead of mutating a package facade.
    _c = compaction_constants

    tokens_saved = 0

    owner_id = None
    if task is not None:
        from lib.tasks_pkg.manager import task_user_id
        owner_id = task_user_id(task)

    # Lazy and pass-local: a steady no-op pass must not inspect all rounds.
    # Keep the first match, exactly like the former per-result ``next`` scan.
    round_by_call_id = None
    emitted_count = 0
    failed_count = 0
    event_elapsed = 0.0
    started_at = time.monotonic()

    def _stamp_l1(msg: dict, before_chars: int, after_chars: int) -> None:
        """Emit evidence for a request-local projection change.

        Never look up or mutate the owning durable round. Historical metadata
        is descriptive user state and must remain byte-identical.
        """
        nonlocal round_by_call_id, emitted_count, failed_count, event_elapsed
        if kwargs.get('_dry_run', False):
            return
        tc_id = msg.get('tool_call_id', '')
        if not tc_id or task is None:
            return
        if round_by_call_id is None:
            round_by_call_id = {}
            for row in task.get('toolRounds') or ():
                if isinstance(row, dict):
                    round_by_call_id.setdefault(str(row.get('toolCallId') or ''), row)
        round_entry = round_by_call_id.get(str(tc_id), {})
        try:
            from lib.token_counter import count_text
            tool_tokens = count_text(
                msg.get('content', '') if isinstance(msg.get('content'), str) else str(msg.get('content', '')),
                model=task.get('model', '') if task else '',
            )
        except Exception as _e:
            logger.debug('[L1] count_text failed for tc_id=%s: %s', tc_id[:8], _e)
            tool_tokens = max(1, after_chars // 4)
        event_started_at = time.monotonic()
        try:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager import append_event
            # Carry the placeholder content on the SSE event itself so the
            # frontend debug panel can patch its cached api-form snapshot
            # immediately, instead of waiting for the next ``messages_snapshot``.
            # Without this, the panel keeps showing the pre-compaction 100KB
            # blob until the next round, even though the chip already flipped
            # to COMPACTED — see "debug panel alignment" trail (2026-05-13).
            _placeholder = msg.get('content', '') if isinstance(msg.get('content'), str) else ''
            append_event(task, build_event(
                EventType.TOOL_COMPACTED,
                roundNum=round_entry.get('roundNum'),
                toolCallId=tc_id,
                toolName=round_entry.get('toolName', ''),
                compactionLayer='L1',
                compactedFromChars=before_chars,
                compactedToChars=after_chars,
                toolTokens=tool_tokens,
                compactedContent=_placeholder,
            ))
            emitted_count += 1
            # Exact events remain durable; routine logs need only one pass
            # aggregate. Enable DEBUG for an individual diagnostic emit.
            logger.debug(
                '[L1] tool_compacted emitted: tc_id=%s tool=%s round=%s '
                '%dch→%dch (-%.0f%%)',
                tc_id[:12] if tc_id else '?',
                round_entry.get('toolName', '?'),
                round_entry.get('roundNum'),
                before_chars, after_chars,
                (1 - after_chars / before_chars) * 100 if before_chars else 0,
            )
        except Exception as _ev_err:
            failed_count += 1
            logger.warning('[L1] tool_compacted SSE emit failed: '
                           'tc_id=%s tool=%s round=%s err=%s',
                           tc_id[:12] if tc_id else '?',
                           round_entry.get('toolName', '?'),
                           round_entry.get('roundNum'), _ev_err)
        finally:
            event_elapsed += max(0.0, time.monotonic() - event_started_at)

    # ── Cache-aware: the previous request's messages are the cached prefix ──
    ignore_cache_prefix = bool(kwargs.get('ignore_cache_prefix', False))
    dry_run = bool(kwargs.get('_dry_run', False))
    sent_count = 0
    if conv_id and not ignore_cache_prefix:
        try:
            from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
            # Pass the LIVE message count so the (monotonic) boundary is
            # clamped to the messages that actually exist this round — a
            # history shrink (L2/L3 macro-compact, edit-and-resend) must let
            # the boundary fall, or micro_compact would be permanently
            # disabled for this conv → unbounded context growth.
            sent_count = get_cache_prefix_count(
                conv_id,
                user_id=owner_id,
                current_msg_count=len(messages),
            )
        except Exception as e:
            logger.debug('[Compaction] cache_tracking not available: %s', e)
    sent_count = min(sent_count, len(messages))

    # ── Run the L1 transform steps ─────────────────────────────────────
    # The Phase A–D bodies live as registered steps in _builtin_steps.py.
    # Build the default ordering, inserting the two gated steps in their
    # historical positions when the corresponding kwarg is set, so output
    # is byte-identical to the pre-refactor monolith.  An explicit
    # ``steps=[...]`` kwarg overrides (used by experiments / config arms).
    from lib.tasks_pkg.compaction._steps import (
        CompactionContext, make_constants, run_steps)
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401 (registers steps)

    step_names = kwargs.get('steps')
    if step_names is None:
        step_names = ['strip_thinking', 'compact_tool_results']
        if kwargs.get('enable_paired_assistant_compact', False):
            step_names.append('fold_paired_interstitial')
        step_names.append('strip_cold_images')
        if kwargs.get('enable_assistant_compact', False):
            step_names.append('compact_cold_assistant')

    constants = make_constants(_c, kwargs.get('constant_overrides'))
    cache_prefix_count, rewrite_from = sent_count, None
    if sent_count and not dry_run:
        cache_prefix_count, rewrite_from = _sent_step_boundary(
            messages, sent_count, step_names,
            conv_id=conv_id, task=task, constants=constants)
    sent_step = (copy.deepcopy(messages[rewrite_from:sent_count])
                 if rewrite_from is not None else None)

    ctx = CompactionContext(
        messages=messages,
        conv_id=conv_id,
        task=task,
        constants=constants,
        cache_prefix_count=cache_prefix_count,
        ignore_cache_prefix=ignore_cache_prefix,
        keep_refetched_results=bool(kwargs.get('keep_refetched_results', False)),
        dry_run=dry_run,
        stamp_fn=_stamp_l1,
    )
    tokens_saved += run_steps(step_names, ctx)

    if sent_step is not None and messages[rewrite_from:sent_count] != sent_step:
        # Admitted edit of cached bytes: the next read drop from this index on
        # is expected, while detectors stay live for anything earlier.
        try:
            from lib.tasks_pkg.cache_tracking._roi import notify_compaction
            notify_compaction(conv_id, user_id=owner_id,
                              intended_rewrite_index=rewrite_from)
        except Exception as exc:
            logger.debug('[L1] cache notification failed: %s', exc)

    if not (task or {}).get('aborted'):
        from lib.tasks_pkg.context_composer.checklist import carry_checklist_through_compaction
        carry_checklist_through_compaction(messages, task)
    if emitted_count or failed_count:
        logger.info(
            '[L1] evidence task=%s emitted=%d failed=%d event_ms=%.1f total_ms=%.1f',
            str(task.get('id') or '?')[:8] if task else '?',
            emitted_count, failed_count, event_elapsed * 1000,
            max(0.0, time.monotonic() - started_at) * 1000,
        )

    return tokens_saved
