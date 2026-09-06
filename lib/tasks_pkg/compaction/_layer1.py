"""Layer 1 — micro_compact: per-round compaction of cold tool results
+ thinking blocks + cold images + (optional) paired interstitials +
(optional) cold assistant content.

Runs every round (cheap, no LLM call).  Cache-aware: messages in the
prompt-cache prefix are left byte-identical to avoid invalidation.

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

from lib.log import get_logger
import lib.tasks_pkg.compaction._constants as compaction_constants

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 1 — Micro-compaction (orchestration shell)
# ═══════════════════════════════════════════════════════════════════════════════

def micro_compact(messages: list, conv_id: str = '', task: dict | None = None,
                  **kwargs) -> int:
    """Compress cold tool results AND strip old thinking blocks.

    The transforms run through the compaction step registry; this
    function owns only request-local bookkeeping and observability.

    Cache-aware: if prompt cache is active (tracked by cache_tracking.py),
    messages in the cache prefix are left byte-identical to avoid
    invalidating the cache.  Inspired by Claude Code's microcompact which
    only edits messages OUTSIDE the cache prefix window.

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

    Returns:
        Estimated number of tokens saved.
    """
    # Constants have one concrete owner. Per-request experiment changes use
    # ``constant_overrides`` below instead of mutating a package facade.
    _c = compaction_constants

    tokens_saved = 0

    owner_id = None
    if task is not None:
        from lib.tasks_pkg.manager import task_user_id
        owner_id = task_user_id(task)

    def _stamp_l1(msg: dict, before_chars: int, after_chars: int) -> None:
        """Emit evidence for a request-local projection change.

        Never look up or mutate the owning durable round. Historical metadata
        is descriptive user state and must remain byte-identical.
        """
        tc_id = msg.get('tool_call_id', '')
        if not tc_id or task is None:
            return
        round_entry = next((
            row for row in (task.get('toolRounds') or [])
            if str(row.get('toolCallId') or '') == str(tc_id)
        ), {})
        try:
            from lib.token_counter import count_text
            tool_tokens = count_text(
                msg.get('content', '') if isinstance(msg.get('content'), str) else str(msg.get('content', '')),
                model=task.get('model', '') if task else '',
            )
        except Exception as _e:
            logger.debug('[L1] count_text failed for tc_id=%s: %s', tc_id[:8], _e)
            tool_tokens = max(1, after_chars // 4)
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
            # ── Diagnostic emit log ──
            # The tool_compacted SSE handler had a cross-message bug
            # (fixed 2026-05-12) where stamps for cold rounds were
            # silently dropped client-side.  Logging every emit at
            # info level lets us tell at a glance whether the SERVER
            # is producing events vs the FRONTEND failing to apply
            # them — without that distinction, "no pill" is a
            # two-place bug hunt every time. Cheap: O(compacted) per
            # micro_compact pass, and L1 already only fires once per
            # cold round per call.
            logger.info(
                '[L1] tool_compacted emitted: tc_id=%s tool=%s round=%s '
                '%dch→%dch (-%.0f%%)',
                tc_id[:12] if tc_id else '?',
                round_entry.get('toolName', '?'),
                round_entry.get('roundNum'),
                before_chars, after_chars,
                (1 - after_chars / before_chars) * 100 if before_chars else 0,
            )
        except Exception as _ev_err:
            logger.warning('[L1] tool_compacted SSE emit failed: '
                           'tc_id=%s tool=%s round=%s err=%s',
                           tc_id[:12] if tc_id else '?',
                           round_entry.get('toolName', '?'),
                           round_entry.get('roundNum'), _ev_err)

    # ── Cache-aware: determine which messages are in the cache prefix ──
    # Messages in the cache prefix are skipped to maintain byte-identical
    # content for prompt cache stability.
    _cache_prefix_count = 0
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking._prefix import get_cache_prefix_count
            # Pass the LIVE message count so the (monotonic) boundary is
            # clamped to the messages that actually exist this round — a
            # history shrink (L2/L3 macro-compact, edit-and-resend) must let
            # the boundary fall, or micro_compact would be permanently
            # disabled for this conv → unbounded context growth.
            _cache_prefix_count = get_cache_prefix_count(
                conv_id,
                user_id=owner_id,
                current_msg_count=len(messages),
            )
        except Exception as e:
            logger.debug('[Compaction] cache_tracking not available: %s', e)

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

    ctx = CompactionContext(
        messages=messages,
        conv_id=conv_id,
        task=task,
        constants=make_constants(_c, kwargs.get('constant_overrides')),
        cache_prefix_count=_cache_prefix_count,
        ignore_cache_prefix=bool(kwargs.get('ignore_cache_prefix', False)),
        stamp_fn=_stamp_l1,
    )
    tokens_saved += run_steps(step_names, ctx)

    return tokens_saved
