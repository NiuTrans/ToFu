"""Layer 1 — micro_compact: per-round compaction of cold tool results
+ thinking blocks + cold images + (optional) paired interstitials +
(optional) cold assistant content.

Runs every round (cheap, no LLM call).  Cache-aware: messages in the
prompt-cache prefix are left byte-identical to avoid invalidation.

Architecture (2026-06 step refactor):
    ``micro_compact`` is the *orchestration shell* — it builds the
    ``_round_index`` (DB + in-flight task), computes the cache-prefix
    boundary, owns the ``_stamp_l1`` durable-placeholder closure, and
    persists narrow turn-projection updates. The actual
    *transforms* (the former Phase A–D) live as registered steps in
    ``_builtin_steps.py`` and run through ``_steps.run_steps`` against a
    :class:`CompactionContext`.  This makes the compression methods
    orderable / ablatable / replaceable purely by configuration without
    touching the shell.

Critical L1 invariant (memory: ``l1-compaction-fix-durable-placeholders``):
when a tool result is compacted, the placeholder text is also written
back to the source-of-truth ``toolContent`` field on the matching turn
projection. Without this, the placeholder lives only in the api-form messages
list (discarded after the LLM call) and the next turn re-reads the original
33k-char content.
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
    function owns the durable-placeholder bookkeeping and persistence.

    Cache-aware: if prompt cache is active (tracked by cache_tracking.py),
    messages in the cache prefix are left byte-identical to avoid
    invalidating the cache.  Inspired by Claude Code's microcompact which
    only edits messages OUTSIDE the cache prefix window.

    Args:
        messages: The live messages list.  Mutated in place.
        conv_id:  Conversation ID for logging.
        task:     Live task dict.  When provided, micro_compact stamps
                  per-call compaction metadata (compactionLayer='L1',
                  compactedFromChars / compactedToChars / toolTokens)
                  on the matching ``task['toolRounds']`` entry and emits
                  a ``tool_compacted`` SSE event so the frontend can flag
                  individual tool calls as compacted in real time.

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

    # ── Lookup index: tool_call_id → round_entry ──
    #
    # The index spans TWO data sources:
    #   1. ``task['toolRounds']`` — the in-flight turn's rounds. These
    #      are the rounds the assistant is producing right now; their
    #      tool_call_ids match the bottom of the api-form ``messages``.
    #   2. ``conv.messages[i]['toolRounds']`` for every prior assistant
    #      message persisted in the DB. By definition L1 compacts COLD
    #      rounds — i.e. rounds that have aged out of the in-flight
    #      turn — so without these the index would only catch the
    #      current turn and silently no-op on every cold compaction.
    #
    # We also stash the loaded conv messages so ``_stamp_l1`` can mutate
    # the source-of-truth ``toolContent`` field (durable placeholders
    # that survive the next ``build_api_messages_from_db`` rebuild) and
    # so the closing block can persist any mutations back to disk.
    _round_index: dict[str, dict] = {}
    # Track which ids came from the persisted conv (vs the in-flight task)
    # so we only DB-write when conv-form rounds were mutated. Task-form
    # rounds are folded by the turn-event bridge when the turn ends.
    _conv_owned_ids: set[str] = set()
    # A normalized turn is the transcript authority for v2 conversations.
    # Keep the owning projected message for every historical tool call so a
    # compacted placeholder can be CAS-patched onto that ONE settled turn;
    # whole-conversation replacement is forbidden for turn-native snapshots.
    _conv_owner_by_tcid: dict[str, dict] = {}
    _dirty_turn_messages: dict[str, dict] = {}
    _conv_messages: list | None = None
    _conv_dirty = False
    _conv_index_loaded = False

    if task is not None:
        for _r in task.get('toolRounds') or []:
            _tcid = _r.get('toolCallId')
            if _tcid:
                _round_index[_tcid] = _r

    owner_id = None
    if task is not None:
        from lib.tasks_pkg.manager import task_user_id
        owner_id = task_user_id(task)

    def _load_conversation_round_index() -> None:
        """Load settled rounds only when a real L1 mutation needs a stamp.

        Most per-round passes are no-ops: either every cold result already is a
        placeholder or the result has not aged beyond the hot tail.  Loading a
        conversation-sized transcript before discovering that fact made the
        nominally cheap L1 pass perform one authority read on every model
        round.  The in-flight task index is sufficient until ``stamp`` sees an
        historical tool-call id; that is the first point at which persistence
        ownership is actually required.
        """
        nonlocal _conv_messages, _conv_index_loaded
        if _conv_index_loaded:
            return
        _conv_index_loaded = True
        if not conv_id or owner_id is None:
            return
        try:
            from lib.agent_core.store import get_conversation_store
            _loaded = get_conversation_store().load_transcript(
                conv_id, user_id=owner_id)
            if _loaded is not None:
                _conv_messages, _unused_updated_at, _unused_rev = _loaded
                if isinstance(_conv_messages, list):
                    if any(not isinstance(_m, dict) or not _m.get('_turnId')
                           for _m in _conv_messages):
                        raise ValueError(
                            'Transcript authority returned an identity-free row')
                    for _m in _conv_messages:
                        if not isinstance(_m, dict):
                            continue
                        for _r in (_m.get('toolRounds') or []):
                            _tcid = _r.get('toolCallId')
                            if _tcid and _tcid not in _round_index:
                                _round_index[_tcid] = _r
                                _conv_owned_ids.add(_tcid)
                                _conv_owner_by_tcid[_tcid] = _m
        except Exception as _e:
            logger.debug('[L1] conv-side _round_index load failed conv=%s: %s',
                         conv_id[:8] if conv_id else '?', _e)
            _conv_messages = None

    def _stamp_l1(msg: dict, before_chars: int, after_chars: int) -> None:
        """Stamp L1 compaction state on the matching round_entry and
        emit a tool_compacted SSE event so the frontend can flag the
        per-tool chip as compacted. Idempotent — safe to call twice.

        Also mutates ``round_entry['toolContent']`` to the placeholder
        text so the next ``build_api_messages_from_db`` rebuild produces
        the compacted content (otherwise the L1 mutation of
        ``messages[idx]['content']`` is thrown away on the next turn)."""
        nonlocal _conv_dirty
        tc_id = msg.get('tool_call_id', '')
        if not tc_id:
            return
        round_entry = _round_index.get(tc_id)
        if round_entry is None:
            _load_conversation_round_index()
            round_entry = _round_index.get(tc_id)
        if round_entry is None:
            return
        if round_entry.get('compactionLayer') == 'L1':
            return  # already stamped
        round_entry['compactionLayer'] = 'L1'
        round_entry['compactedFromChars'] = before_chars
        round_entry['compactedToChars'] = after_chars
        # Make the placeholder durable: rebuild reads toolContent, not
        # the api-form messages list. Only flag the conversation dirty
        # when the round we mutated actually lives in the conv-side
        # messages snapshot (cold rounds from prior assistant turns).
        # Task-form rounds (current turn) get persisted via the
        # terminal turn-event projection path.
        _new_content = msg.get('content', '')
        if isinstance(_new_content, str) and _new_content:
            round_entry['toolContent'] = _new_content
            if tc_id in _conv_owned_ids:
                _conv_dirty = True
                _owner = _conv_owner_by_tcid.get(tc_id)
                _turn_id = str((_owner or {}).get('_turnId') or '')
                if _turn_id:
                    _dirty_turn_messages[_turn_id] = _owner
        # Cheap re-count: micro_compact's placeholders are short, so the
        # token estimate from len/4 is fine here. We still try the real
        # counter but it's optional.
        try:
            from lib.token_counter import count_text
            round_entry['toolTokens'] = count_text(
                msg.get('content', '') if isinstance(msg.get('content'), str) else str(msg.get('content', '')),
                model=task.get('model', '') if task else '',
            )
        except Exception as _e:
            logger.debug('[L1] count_text failed for tc_id=%s: %s', tc_id[:8], _e)
            round_entry['toolTokens'] = max(1, after_chars // 4)
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
                toolTokens=round_entry.get('toolTokens', 0),
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

    # ── Persist conv-form mutations ──
    # When _stamp_l1 mutated the source-of-truth toolContent on a round
    # belonging to a prior assistant message, write the conversation back
    # to the DB so the next build_api_messages_from_db rebuild produces
    # placeholders. Without this, the placeholder lives only in the api-
    # form messages list which is discarded after this LLM call, and
    # next turn re-reads the original 33k-char content.
    #
    # CAS guard via rev: if a concurrent writer changed the row, skip the write
    # — they have a fresher view; our mutation will be re-applied next
    # round when this conversation is rebuilt and L1 fires again.
    # ``rev`` and not ``updated_at``: the latter is a value the writer itself
    # supplies, so a clock that does not advance between two writes yields a
    # predicate that passes while the data has already changed. ``rev`` is
    # issued by a DB trigger in the same statement and cannot be forged.
    if _conv_dirty and _conv_messages is not None and conv_id:
        _landed = 0
        # Persist each dirty settled owner through the same turn-projection CAS
        # used by PATCH /turns. A whole-transcript fallback does not exist.
        from lib.turn_lifecycle import (
            LifecycleConflict, LifecycleNotFound,
            update_turn_projection,
        )
        for _turn_id, _owner in _dirty_turn_messages.items():
            _projection_rev = _owner.get('_projectionRevision')
            if not isinstance(_projection_rev, int):
                logger.warning('[L1-persist] conv=%s turn=%s lacks a '
                               'projection revision; placeholder will be '
                               're-applied next round', conv_id[:8],
                               _turn_id[:8])
                continue
            try:
                _updated = update_turn_projection(
                    conv_id, _turn_id, projection=_owner,
                    expected_projection_revision=_projection_rev,
                    user_id=owner_id)
                _new_turn = (_updated or {}).get('turn') or {}
                if isinstance(_new_turn.get('projectionRevision'), int):
                    _owner['_projectionRevision'] = _new_turn[
                        'projectionRevision']
                _landed += 1
            except LifecycleConflict:
                logger.info('[L1-persist] conv=%s turn=%s CAS skipped — '
                            'turn changed; placeholder will be re-applied '
                            'next round', conv_id[:8], _turn_id[:8])
            except LifecycleNotFound:
                logger.warning('[L1-persist] conv=%s turn=%s vanished '
                               'before placeholder persist', conv_id[:8],
                               _turn_id[:8])
            except Exception as _e:
                logger.warning('[L1-persist] conv=%s turn=%s persist failed: %s',
                               conv_id[:8], _turn_id[:8], _e, exc_info=True)
        if _landed:
            logger.info('[L1-persist] conv=%s patched %d turn projection(s) '
                        'with durable placeholders', conv_id[:8], _landed)

        # Event-driven cross-device sync: either persistence path bumped the
        # authoritative conversation revision. Push once per L1 pass, only
        # after at least one CAS landed.
        if _landed:
            try:
                from lib.agent_core.store import get_conversation_store
                get_conversation_store().notify_conversation_changed(
                    conv_id, user_id=owner_id)
            except Exception as _ne:
                logger.debug('[L1-persist] conv=%s conv-changed notify skipped: %s',
                             conv_id[:8] if conv_id else '?', _ne)

    return tokens_saved
