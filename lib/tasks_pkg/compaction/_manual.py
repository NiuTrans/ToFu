"""Layer 2 (manual) — user-triggered active context compaction (`/compact`).

Unlike the automatic L2 path (``_layer2.execute_compact_tool``), which is
*ephemeral* and rewrites only the in-flight request, this module implements the
**persistent** manual compaction the user asks for with a button. It rewrites
the conversation's actual transcript authority so every subsequent turn loads
a small context: a legacy conversation uses a message-aggregate revision CAS;
a turn-native conversation compiles the before/after projections into the
receipt-backed atomic ``turn.compact`` command. It never overwrites the frozen
legacy placeholder after Turn rows become authoritative.

It REUSES the L2 summary engine verbatim (``_generate_query_aware_summary`` +
the compact state-receipt prompt + objective-anchor protection +
recently-accessed files). It does NOT rewrite any summary logic.

────────────────────────────────────────────────────────────────────────────
⚠️  HARD CONSTRAINT (see docs/modules/context_engineering.md): boundary
    computation and application MUST stay in the SAME index space.

    ``_transform_messages`` (the api-form projection) is length/order-changing:
    one raw assistant row with N ``toolRounds`` expands into many api messages,
    Flow sessions collapse, same-role rows merge. So an index computed on
    the api-form list CANNOT be mapped 1:1 back to the raw
    ``conversations.messages`` list.  Slicing the raw list with an api-space
    index would split a tool round → orphan ``tool`` messages / a broken
    tool_call/result pair → HTTP 400.

    THE RULE — read-project, write-raw, compute-in-raw-space:
      * turn / boundary / anchor are defined on the RAW stored shape; the
        boundary ALWAYS lands on a raw ``user`` index and slicing is on raw.
      * per-turn token estimation is RAW-AWARE (``_raw_estimate_tokens``):
        it projects the turn slice through ``_transform_messages`` then
        ``_estimate_total_tokens`` — it does NOT call ``_estimate_msg_tokens``
        on a raw assistant row (that is api-form-aware and blind to
        ``toolRounds``, so it would massively under-count a tool-heavy turn).
      * ``_transform_messages`` is used ONLY read-only, at two points: to count
        a turn's tokens and to build the summary-input text.  Its output index
        is never used to slice the raw list.

Public surface:
  * ``compact_conversation_now`` — authority-aware orchestrator (load → plan →
    archive → summarize → atomic persist). Idle-only (caller gates on
    activeTaskId).
  * ``plan_manual_compaction``   — pure: computes system_end / boundary /
    anchor / old-region / reserve-region.  No DB, no LLM.
  * ``apply_manual_compaction``  — pure: rebuilds the raw message list.
  * ``_raw_turn_boundary`` / ``_raw_estimate_tokens`` — the raw-space helpers.
"""

from __future__ import annotations

import time
import uuid

from lib.log import audit_log, get_logger
from lib.agent_core.push import push_event
from lib.agent_core.store import get_conversation_store
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _MANUAL_COMPACT_MIN_TOKENS,
    _MANUAL_INTRA_TURN_HOT_ROUNDS,
    _MANUAL_RECONCILE_HARD_ITER_CAP,
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
    _SUMMARY_INPUT_CHAR_CAP,
    manual_reconcile_budget_sec,
)
from lib.tasks_pkg.compaction._layer2 import (
    _extract_current_query,
    _extract_objective_anchor_text,
    _extract_recently_accessed_files,
    _extract_summary_objective,
    _generate_query_aware_summary,
    _objective_anchor_index,
    _split_cold_rounds,
)
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _usable_context,
)
from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

logger = get_logger(__name__)

_SUMMARY_HEADER = '## 上下文已压缩（主动 /compact）'
"""Marker heading prepended to the persisted summary message body."""

# B — live progress channel. A manual /compact runs in the IDLE state (no live
# task carries an SSE stream), so we push the summary as it streams on an
# INDEPENDENT push channel keyed by conv_id. The frontend context-bar subscribes
# to ('compaction', conv_id) around its POST and grows the card from the deltas.
_COMPACTION_CHANNEL = 'compaction'


def _push_compaction(conv_id: str, payload: dict, *, user_id) -> None:
    """Best-effort push on the compaction channel (never breaks compaction).

    The DB rewrite is the source of truth; the live card is a cosmetic overlay.
    A missing subscriber / hub error must never surface as a compaction
    failure, so every exception is swallowed at debug level."""
    try:
        push_event(_COMPACTION_CHANNEL, conv_id, payload, user_id=user_id)
    except Exception as e:
        logger.debug('[ManualCompact] conv=%s push %s skipped: %s',
                     conv_id[:8] if conv_id else '?', payload.get('type'), e)

# §10.1: 档B introduces two hyperparameters (intra-turn hot-round count +
# manual-compact floor). Owner sign-off recorded in JOURNAL 2026-07-16. Emit a
# one-time config_change audit entry documenting the sanctioned values.
_CONFIG_CHANGE_LOGGED = False


def _audit_config_once() -> None:
    global _CONFIG_CHANGE_LOGGED
    if _CONFIG_CHANGE_LOGGED:
        return
    _CONFIG_CHANGE_LOGGED = True
    try:
        audit_log('config_change', change='manual_compaction_intra_turn',
                  intra_turn_hot_rounds=_MANUAL_INTRA_TURN_HOT_ROUNDS,
                  manual_compact_min_tokens=_MANUAL_COMPACT_MIN_TOKENS,
                  summary_input_char_cap=_SUMMARY_INPUT_CHAR_CAP,
                  approved_by='user')
    except Exception as e:
        logger.debug('[ManualCompact] config_change audit skipped: %s', e)


def _project(raw_slice: list, config: dict | None = None) -> list:
    """Project a RAW message slice into api-form (READ-ONLY).

    Expands ``toolRounds`` into assistant(tool_calls)+tool pairs exactly as the
    live request path does, so token counts and summary input match what the
    LLM actually sees.  Always called with an EMPTY config so no system prompt
    is injected into the counted slice.  The returned list's indices are NEVER
    used to slice the raw list (see the module-level hard constraint).
    """
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    try:
        return _transform_messages(list(raw_slice), {})
    except Exception as e:
        logger.warning('[ManualCompact] projection failed (%d raw msgs): %s',
                       len(raw_slice), e)
        return []


def _raw_estimate_tokens(raw_slice: list, config: dict | None = None) -> int:
    """Raw-aware token estimate for a slice of RAW conversation messages.

    Projects the slice to api-form (expanding ``toolRounds``) then sums the
    per-message estimate.  This is the ONLY correct way to size a raw turn:
    ``_estimate_msg_tokens`` on a raw assistant row reads ``content``/
    ``tool_calls`` and is blind to ``toolRounds``, so it under-counts a
    tool-heavy turn by the entire tool-args + tool-output payload.
    """
    return _estimate_total_tokens(_project(raw_slice, config))


def _system_end(raw_messages: list) -> int:
    """Index just past the leading ``system`` block in the RAW list.

    Raw ``conversations.messages`` normally carry no system row (the system
    prompt is injected by ``_transform_messages``), but we skip a leading
    system block defensively so the anchor/boundary never fall inside it.
    """
    end = 0
    for m in raw_messages:
        if isinstance(m, dict) and m.get('role') == 'system':
            end += 1
        else:
            break
    return end


def _raw_turn_boundary(
    raw_messages: list,
    *,
    config: dict | None = None,
    task: dict | None = None,
    max_turns: int = _MAX_PRESERVE_TURNS,
    budget_tokens: int | None = None,
) -> int:
    """Preservation boundary in RAW index space (mirror of _find_turn_boundary).

    A *turn* = ``[user_msg, ...all subsequent non-user raw messages]``.  Turns
    are atomic; the boundary ALWAYS falls on a raw ``user`` index so a tool
    round (a raw assistant row with its ``toolRounds``) is never split.

    Policy (identical to the L2 boundary, but RAW-AWARE token sizing):
      • HARD INVARIANT — the current (most-recent) turn is always preserved.
      • BEST-EFFORT    — older turns added newest→oldest while
        ``preserved + turn_tokens <= budget_tokens`` AND count ``<= max_turns``.
      • REFUSE         — no ``user`` row → returns ``len(raw_messages)`` so the
        caller short-circuits (nothing to compact).
    """
    config = config or {}
    user_idx = [i for i, m in enumerate(raw_messages)
                if isinstance(m, dict) and m.get('role') == 'user']
    if not user_idx:
        return len(raw_messages)

    if budget_tokens is None:
        usable = _usable_context(_get_context_limit(task))
        budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))

    turn_starts = user_idx
    turn_ends = user_idx[1:] + [len(raw_messages)]

    cur_start, cur_end = turn_starts[-1], turn_ends[-1]
    boundary = cur_start
    preserved = _raw_estimate_tokens(raw_messages[cur_start:cur_end], config)
    count = 1

    for k in range(len(turn_starts) - 2, -1, -1):
        if count >= max_turns:
            break
        start, end = turn_starts[k], turn_ends[k]
        tt = _raw_estimate_tokens(raw_messages[start:end], config)
        if preserved + tt > budget_tokens:
            break
        boundary = start
        preserved += tt
        count += 1

    return boundary


def _collect_reserve_folds(
    reserve_raw: list,
    *,
    config: dict | None = None,
    hot_budget_tokens: int | None = None,
) -> list:
    """档B — intra-turn fold plan for the RESERVE region.

    Scans every assistant in the preserved (reserve) region and splits complete
    rounds into COLD (summarize + drop) and one newest chronological HOT suffix
    (keep verbatim). The hot-round count is a maximum; the planner's token
    budget may fold a short but oversized suffix. Returns a list of fold dicts
    (possibly empty), one per folded assistant, in reserve order:

      ``asst_idx``     — index (within ``reserve_raw``) of the heavy assistant
      ``cold_rounds``  — the older ``toolRounds`` to summarize + drop
      ``hot_rounds``   — the most-recent ``toolRounds`` kept verbatim

    Multiple heavy turns share the same chronological suffix budget, matching
    automatic L2. Older assistants may therefore retain no raw tool round; their
    prose remains and every removed complete round enters the summary source.

    HARD CONSTRAINT: each round is self-contained (its own ``toolCallId`` +
    ``toolContent``), so dropping WHOLE cold rounds from one assistant's
    ``toolRounds`` array never orphans a ``tool`` message nor splits a
    tool_call/result pair.  We edit ONLY that message's ``toolRounds`` list —
    never slice across messages (design §4.1).
    """
    # The legacy/no-budget helper keeps its original per-assistant behavior for
    # compatibility with direct callers. The real planner supplies a budget and
    # uses one chronological round sequence across the entire reserve, matching
    # the automatic api-form fold.
    if hot_budget_tokens is None:
        folds = []
        for i, m in enumerate(reserve_raw):
            if not isinstance(m, dict) or m.get('role') != 'assistant':
                continue
            rounds = m.get('toolRounds') or []
            cold, hot = _split_cold_rounds(
                rounds, _MANUAL_INTRA_TURN_HOT_ROUNDS)
            if cold:
                folds.append({
                    'asst_idx': i,
                    'cold_rounds': cold,
                    'hot_rounds': hot,
                })
        return folds

    descriptors = []
    reserve_without_rounds = []
    for i, m in enumerate(reserve_raw):
        copied = dict(m) if isinstance(m, dict) else m
        if isinstance(copied, dict) and copied.get('role') == 'assistant':
            rounds = copied.get('toolRounds') or []
            if isinstance(rounds, list):
                descriptors.extend(
                    {'asst_idx': i, 'round': round_value}
                    for round_value in rounds
                )
                copied['toolRounds'] = []
        reserve_without_rounds.append(copied)

    if not descriptors:
        return []
    base_tokens = _raw_estimate_tokens(reserve_without_rounds, config)

    def _round_cost(descriptor: dict) -> int:
        return _raw_estimate_tokens([{
            'role': 'assistant',
            'content': '',
            'toolRounds': [descriptor['round']],
        }], config)

    cold_descriptors, hot_descriptors = _split_cold_rounds(
        descriptors,
        _MANUAL_INTRA_TURN_HOT_ROUNDS,
        hot_budget_tokens=hot_budget_tokens,
        base_tokens=base_tokens,
        token_cost=_round_cost,
    )
    cold_by_assistant: dict[int, list] = {}
    hot_by_assistant: dict[int, list] = {}
    for descriptor in cold_descriptors:
        cold_by_assistant.setdefault(descriptor['asst_idx'], []).append(
            descriptor['round'])
    for descriptor in hot_descriptors:
        hot_by_assistant.setdefault(descriptor['asst_idx'], []).append(
            descriptor['round'])

    folds = []
    for assistant_index in sorted(cold_by_assistant):
        folds.append({
            'asst_idx': assistant_index,
            'cold_rounds': cold_by_assistant[assistant_index],
            'hot_rounds': hot_by_assistant.get(assistant_index, []),
        })
    return folds


def plan_manual_compaction(
    raw_messages: list,
    *,
    config: dict | None = None,
    task: dict | None = None,
    max_turns: int | None = None,
    total_tokens: int | None = None,
) -> dict | None:
    """Compute the manual-compaction plan in RAW index space (pure, no DB/LLM).

    Returns a plan dict, or ``None`` ONLY when the whole conversation is below
    the manual-compact floor (``_MANUAL_COMPACT_MIN_TOKENS``) — a genuinely
    tiny conversation where compaction would save nothing.

    Two (composable) folding sources — BOTH may apply in one pass:
      • OLD region (``mode='turns'``) — when there is history before the turn
        boundary, summarize it and preserve the recent turns verbatim.
      • RESERVE intra-turn folds (档B, ``intra_folds``) — any assistant in the
        PRESERVED region whose ``toolRounds`` fill the window has its cold
        rounds folded in place.  This is what fixes both the single-giant-turn
        title bug AND the "heavy old turn(s) + giant current turn" case where
        turns-mode alone left the current turn whole (~85% residual).

    ``mode`` is ``'turns'`` when an OLD region exists (even if reserve folds
    also apply), else ``'intra_turn'`` (folds only).  ``None`` is returned ONLY
    when the whole conversation is below the floor OR there is nothing to fold
    in either source.

    Plan keys:
      ``system_raw`` / ``anchor_msg`` / ``reserve_raw`` / ``boundary`` /
      ``old_raw`` (turns mode; ``[]`` in intra_turn) / ``intra_folds`` (list;
      possibly empty in turns mode) / ``mode``.
    """
    config = config or {}
    resolved_max = _MAX_PRESERVE_TURNS if max_turns is None else max(1, int(max_turns))

    system_end = _system_end(raw_messages)

    # FLOOR: the ONLY size-based decline. A conversation whose entire projected
    # size is below the floor genuinely has nothing worth compacting — so a full
    # 1M single-giant-turn conversation is never wrongly refused.
    # Projecting the whole raw list to api-form is the dominant CPU cost on a
    # multi-MB conversation; the DB orchestrator computes it once and threads it
    # in as ``total_tokens`` so we never re-project the full list here (speed).
    if total_tokens is None:
        total_tokens = _raw_estimate_tokens(raw_messages, config)
    if total_tokens < _MANUAL_COMPACT_MIN_TOKENS:
        return None

    usable = _usable_context(_get_context_limit(task))
    preserve_budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))
    boundary = _raw_turn_boundary(
        raw_messages, config=config, task=task, max_turns=resolved_max,
        budget_tokens=preserve_budget_tokens)

    anchor_idx = _objective_anchor_index(raw_messages)
    anchor_msg = None
    if anchor_idx is not None and system_end <= anchor_idx < boundary:
        anchor_msg = raw_messages[anchor_idx]

    old_raw = list(raw_messages[system_end:boundary])
    if anchor_msg is not None:
        old_raw = [m for k, m in enumerate(raw_messages[system_end:boundary],
                                           start=system_end)
                   if k != anchor_idx]

    reserve_raw = list(raw_messages[boundary:])
    # 档B: fold every giant turn PRESERVED in the reserve (composable with the
    # old-region summary below). This is what stops a giant CURRENT turn from
    # surviving turns-mode whole.
    intra_folds = _collect_reserve_folds(
        reserve_raw,
        config=config,
        hot_budget_tokens=preserve_budget_tokens,
    )

    # ── turns mode: a genuine OLD region exists (reserve folds may also apply) ──
    if old_raw:
        return {
            'mode': 'turns',
            'system_end': system_end,
            'boundary': boundary,
            'anchor_msg': anchor_msg,
            'old_raw': old_raw,
            'reserve_raw': reserve_raw,
            'system_raw': list(raw_messages[:system_end]),
            'intra_folds': intra_folds,
        }

    # ── 档B intra-turn only: no old region, but reserve turns fill the window ──
    if not intra_folds:
        # Above the floor but nothing foldable in either source (e.g. one huge
        # single tool result, or a big plain-text turn with <= hot-tail rounds).
        # Decline gracefully rather than risk a cross-message structural break.
        return None

    return {
        'mode': 'intra_turn',
        'system_end': system_end,
        'boundary': boundary,
        'anchor_msg': None,
        'old_raw': [],
        'reserve_raw': reserve_raw,
        'system_raw': list(raw_messages[:system_end]),
        'intra_folds': intra_folds,
    }


def _build_summary_message(
    summary_text: str,
    *,
    archive_id: str | None,
    tokens_after: int,
) -> dict:
    """Build the C1 summary message: a plain assistant row marked as a
    compaction boundary so the frontend renders it as a fold card and headless
    consumers see it as ordinary assistant text."""
    marker = {
        'archiveId': archive_id,
        'trigger': 'manual',
        'tokenCountKind': 'estimated',
        'snapshotKind': 'pre_compaction_transcript',
        'ts': int(time.time()),
    }
    return {
        'role': 'assistant',
        'content': f'{_SUMMARY_HEADER}\n\n{summary_text}',
        '_isCompactionSummary': True,
        '_compactionArchiveId': archive_id,
        # gauge scheme B: no real usage on this synthetic message, so the
        # frontend falls back to this estimate until the next real turn.
        '_estimatedPromptTokens': int(tokens_after or 0),
        '_compactions': [marker],
        'timestamp': int(time.time() * 1000),
    }


def apply_manual_compaction(
    raw_messages: list,
    plan: dict,
    summary_text: str,
    *,
    archive_id: str | None = None,
    tokens_after: int = 0,
) -> list:
    """Rebuild the RAW message list from a plan + summary (pure).

    Unified rebuild (both modes): ``[system...] + [anchor user?] +
    [summary assistant] + [reserve...]`` where every reserve assistant named in
    ``intra_folds`` has its ``toolRounds`` trimmed to the hot tail in place.

    • turns mode has an anchor (when the objective anchor fell in the OLD
      region) and a non-empty ``old_raw`` folded into the summary; reserve folds
      may ALSO apply (a giant current turn preserved in the reserve).
    • intra_turn mode has no anchor / no ``old_raw`` — the summary is built from
      the reserve folds' cold rounds only.

    Because the boundary lands on a user index, the reserve starts with a
    ``user`` row, so the summary (assistant) is always followed by a user row →
    no assistant-assistant adjacency for ``_merge_consecutive_same_role`` to
    collapse (design test 7b).  Each round is self-contained, so trimming a
    reserve assistant's ``toolRounds`` never orphans a tool.
    """
    summary_msg = _build_summary_message(
        summary_text, archive_id=archive_id, tokens_after=tokens_after)

    reserve = [dict(m) for m in plan['reserve_raw']]
    for fold in plan.get('intra_folds', []):
        heavy = reserve[fold['asst_idx']]
        heavy['toolRounds'] = list(fold['hot_rounds'])
        heavy['_intraTurnFolded'] = len(fold['cold_rounds'])

    anchor_block = [plan['anchor_msg']] if plan.get('anchor_msg') is not None else []
    return (list(plan['system_raw'])
            + anchor_block
            + [summary_msg]
            + reserve)


def _msgs_fingerprint(msgs: list) -> str:
    """Stable byte-image of a message slice for conflict detection.

    JSON with sorted keys so two logically-identical rows compare equal
    regardless of dict insertion order. Used to decide whether a concurrent
    write touched the FOLDED region (a real conflict → stale) versus only the
    preserved tail (reconcilable → keep both the compaction and the new tail).
    """
    import json
    return json.dumps(msgs, sort_keys=True, ensure_ascii=False, default=str)


def _fold_watermark(plan: dict) -> int:
    """Highest RAW index the compaction READS/FOLDS (exclusive upper bound).

    Everything at index ``< watermark`` is consumed into the summary or is a
    folded reserve assistant — it MUST be byte-unchanged for the compaction to
    stay valid. Everything ``>= watermark`` is pure preserved tail that a
    concurrent writer may freely append to / modify without invalidating the
    summary.

      • The summarized OLD region + anchor + system block all live at
        ``< boundary``.
      • Each intra-turn-folded reserve assistant sits at raw index
        ``boundary + asst_idx``; folding reads its ``toolRounds``, so it (and
        everything before it) must be stable — hence ``+ asst_idx + 1``.
    """
    boundary = plan['boundary']
    watermark = boundary
    for f in plan.get('intra_folds', []):
        watermark = max(watermark, boundary + int(f['asst_idx']) + 1)
    return watermark


def compact_conversation_now(
    conv_id: str,
    *,
    user_id,
    config: dict | None = None,
    task: dict | None = None,
    keep_recent_turns: int | None = None,
) -> dict:
    """User-triggered persistent compaction of a conversation.

    Reuses the L2 summary engine and commits one semantic ``turn.compact``
    command so subsequent turns start from a small context.
    Idle-only — the caller (REST route) MUST refuse when a task is running.

    Returns a dict: ``{ok, ...}`` on success or ``{ok: False, error}``.
    Error codes: ``not_found`` / ``nothing_to_compact`` / ``summary_failed`` /
    ``turn_protocol_unsupported`` / ``stale``.

    CONCURRENCY: the summary LLM call is slow; a sibling agent turn can write
    the conversation TAIL meanwhile. Such a write only extends the PRESERVED
    region — it does NOT touch the folded (summarized) region — so the
    compaction stays valid and must not discard the concurrent work. After
    summarizing, the engine reloads the CURRENT messages, verifies the folded
    prefix is byte-unchanged, rebuilds over the CURRENT tail, and CAS-es on the
    CURRENT ``updated_at``, retrying within a wall-clock budget
    (``manual_reconcile_budget_sec``) so a sustained write burst cannot surface
    as a 409. ``stale`` is returned ONLY when the folded region itself was
    concurrently rewritten (a genuine conflict), or the budget is exhausted —
    never merely because the tail grew.
    """
    config = config or {}
    log_id = conv_id[:8] if conv_id else '?'
    _audit_config_once()
    store = get_conversation_store()

    loaded = store.load_transcript(conv_id, user_id=user_id)
    if loaded is None:
        logger.warning('[ManualCompact] conv=%s not found', log_id)
        return {'ok': False, 'error': 'not_found'}
    raw_messages, updated_at, _load_rev = loaded
    turn_native = bool(raw_messages) and all(
        isinstance(message, dict) and bool(message.get('_turnId'))
        for message in raw_messages)
    if raw_messages and not turn_native:
        logger.error('[ManualCompact] conv=%s transcript contains a row '
                     'without turn identity', log_id)
        return {'ok': False, 'error': 'turn_protocol_unsupported'}

    # Project the WHOLE raw conversation to api-form exactly ONCE — this is the
    # dominant CPU cost of a manual /compact on a multi-MB conversation. Reuse
    # this single projection for: the plan-floor total (via total_tokens=),
    # tokens_before, and the recently-accessed-files scan below. The old code
    # projected the full list three separate times, which made the button feel
    # slow. Per-turn boundary sizing still projects small turn SLICES (cheap and
    # inherent), not the whole list.
    full_api = _project(raw_messages, config)
    tokens_before = _estimate_total_tokens(full_api)

    plan = plan_manual_compaction(
        raw_messages, config=config, task=task, max_turns=keep_recent_turns,
        total_tokens=tokens_before)
    if plan is None:
        logger.info('[ManualCompact] conv=%s nothing to compact (%d msgs)',
                    log_id, len(raw_messages))
        return {'ok': False, 'error': 'nothing_to_compact'}

    msgs_before = len(raw_messages)

    # ── ARCHIVE the full pre-compaction snapshot BEFORE any rewrite ──
    # (design test 3). trigger='manual'; no SSE event (no live task to carry it).
    archive_id = _archive_transcript(
        conv_id, raw_messages,
        user_id=user_id,
        trigger='manual', task=task, round_num=0,
        tokens_before=int(tokens_before), msgs_before=int(msgs_before),
        reason='manual /compact', emit_event=False)

    # ── SUMMARIZE everything being folded (reuse the L2 engine) ──
    # The summary input is the OLD region (turns mode) PLUS the COLD tool rounds
    # from every folded reserve assistant (档B). Each folded assistant's cold
    # rounds are wrapped in a synthetic single-assistant row so _project expands
    # them exactly as the live request would (self-contained rounds → no orphan
    # tool). Both sources are composable — a giant CURRENT turn preserved in the
    # reserve is folded here even when there is also an old region to summarize.
    fold_input = list(plan.get('old_raw') or [])
    for fold in plan.get('intra_folds', []):
        # The assistant prose stays verbatim in ``reserve_raw``. Feed only the
        # removed rounds to the lossy summary so the resulting receipt does not
        # duplicate already-preserved conclusions.
        fold_input.append({'role': 'assistant',
                           'content': '',
                           'toolRounds': list(fold['cold_rounds'])})
    old_api = _project(fold_input, config)
    current_query = _extract_current_query(_project(plan['reserve_raw'], config))
    anchor_text = _extract_objective_anchor_text(full_api)
    _folded_rounds = sum(len(f['cold_rounds'])
                         for f in plan.get('intra_folds', []))
    _preserved_turns = sum(
        1 for message in plan.get('reserve_raw', [])
        if isinstance(message, dict) and message.get('role') == 'user')
    recent_files = _extract_recently_accessed_files(full_api)
    _summary_usage: dict = {}
    _summarize_ms = 0.0
    _turn_diff_included = False

    def _manual_receipt(
        status: str,
        *,
        summary_value: str = '',
        summary_generated: bool = False,
        outcome_reason: str = '',
        reconcile_attempts: int = 0,
    ) -> dict:
        return build_compaction_receipt(
            trigger='manual',
            status=status,
            strategy='selective_summary',
            implementation='model_summary',
            mode=plan.get('mode') or 'turns',
            continuation_format=(
                'durable_summary_turn' if status == 'completed' else 'none'),
            summary_generated=summary_generated,
            summary_text=summary_value,
            summary_usage=_summary_usage,
            summary_duration_ms=_summarize_ms,
            summarized_messages=len(old_api),
            preserved_turns=_preserved_turns,
            folded_tool_rounds=_folded_rounds,
            objective_anchored=plan.get('anchor_msg') is not None,
            durable_objective_applied=bool(
                _extract_summary_objective(summary_text)),
            retained_user_messages=0,
            recent_files=recent_files,
            turn_diff_included=_turn_diff_included,
            reconcile_attempts=reconcile_attempts,
            outcome_reason=outcome_reason,
        )
    # B — stream the summary to the compaction card. summary_start tells the
    # frontend to show a live-growing card; each on_delta pushes the next chunk;
    # summary_done (below, after persist) carries the final stats. All pushes are
    # best-effort (see _push_compaction) so a dead channel never breaks compaction.
    _push_compaction(
        conv_id,
        {'type': 'summary_start', 'archiveId': archive_id},
        user_id=user_id,
    )

    def _on_summary_delta(chunk: str) -> None:
        _push_compaction(
            conv_id,
            {'type': 'summary_delta', 'text': chunk,
             'archiveId': archive_id},
            user_id=user_id,
        )

    # Phase timing: the summary LLM call dominates a manual /compact's wall clock
    # (measured ~96% on a 3 MB conv — the projection/reconcile CPU is <5%). This
    # is the ONE number that explains "compaction is slow", so log it explicitly
    # with the input size so a slow compaction is diagnosable from app.log alone.
    _summarize_t0 = time.monotonic()
    summary_text = _generate_query_aware_summary(
        old_api, current_query, '[ManualCompact]', conv_id=conv_id, task=task,
        on_delta=_on_summary_delta, usage_out=_summary_usage,
        anchor_text=anchor_text)
    _summarize_ms = (time.monotonic() - _summarize_t0) * 1000
    logger.info('[ManualCompact] conv=%s summary LLM call: %.0f ms  '
                '(fold_msgs=%d, fold_tokens≈%d) — dominant cost of /compact',
                log_id, _summarize_ms, len(old_api),
                _estimate_total_tokens(old_api))
    if not summary_text:
        logger.warning('[ManualCompact] conv=%s summary generation failed — '
                       'leaving conversation intact', log_id)
        failure_receipt = _manual_receipt(
            'failed', outcome_reason='summary_failed')
        if archive_id is not None:
            try:
                store.update_archive_summary(
                    archive_id, '', int(tokens_before), int(msgs_before),
                    user_id=user_id, receipt=failure_receipt)
            except Exception as e:
                logger.debug('[ManualCompact] failed receipt update skipped: %s',
                             e)
        _push_compaction(
            conv_id,
            {'type': 'summary_failed', 'archiveId': archive_id,
             'receipt': failure_receipt},
            user_id=user_id,
        )
        return {'ok': False, 'error': 'summary_failed', 'archiveId': archive_id,
                'receipt': failure_receipt}

    # Same re-pin as the automatic L2 path: an accepted receipt carries the
    # current effective goal, so keep the autopilot objective pin in step.
    from lib.tasks_pkg.autopilot_state import _update_objective_from_receipt
    _update_objective_from_receipt(
        conv_id, _extract_summary_objective(summary_text), user_id=user_id)

    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        summary_text += (
            '\n\n### Recently Accessed Files\n'
            'Use read_files to review current state if needed:\n'
            f'{file_list}')

    # Same turn-diff enrichment as the automatic L2 path (Codex-inspired
    # turn_diff_tracker.rs analogue) — one builder, no drift.
    try:
        from lib.tasks_pkg.commit_round._turn_diff import build_turn_diff_block
        _diff_block = build_turn_diff_block(
            task, config.get('projectPath') or '',
            config.get('projectPaths') or None)
        if _diff_block:
            summary_text += f'\n\n{_diff_block}'
            _turn_diff_included = True
    except Exception as e:
        logger.debug('[ManualCompact] turn-diff block failed: %s', e)

    def _settle_aborted_archive(outcome_reason: str, attempts: int) -> dict:
        receipt = _manual_receipt(
            'aborted', summary_value=summary_text,
            summary_generated=True, outcome_reason=outcome_reason,
            reconcile_attempts=attempts)
        if archive_id is not None:
            try:
                store.update_archive_summary(
                    archive_id, summary_text, int(tokens_before),
                    int(msgs_before), user_id=user_id, receipt=receipt)
            except Exception as e:
                logger.debug('[ManualCompact] aborted receipt update skipped: %s',
                             e)
        _push_compaction(
            conv_id,
            {'type': 'summary_failed', 'archiveId': archive_id,
             'reason': outcome_reason, 'receipt': receipt},
            user_id=user_id,
        )
        return receipt

    # ── RECONCILE + PERSIST ──
    # Root cause of the "manual /compact always 409 on an active conversation":
    # the summary LLM call takes seconds, during which a sibling agent turn
    # appends/patches the conversation TAIL. The old code CAS-ed on the LOAD-time
    # ``updated_at`` guarding the WHOLE row, so ANY concurrent write — even one
    # that only touched the preserved tail and left the folded region untouched —
    # lost the CAS and returned `stale`/409. But compaction and "the tail grew a
    # few rounds" are SEMANTICALLY MERGEABLE: the summary only consumes the FOLDED
    # region (raw indices ``< watermark``); the tail is preserved verbatim either
    # way.
    #
    # So instead of a single-shot CAS on stale state we: reload the CURRENT
    # messages, verify the folded region is byte-unchanged (the ONLY thing that
    # would invalidate the summary), rebuild over the CURRENT tail (so no
    # concurrently-appended turn is lost), and CAS on the CURRENT ``updated_at``.
    # We retry within a WALL-CLOCK BUDGET (not a fixed count): the window is now
    # LLM-free but each pass still costs tens-to-hundreds of ms on a multi-MB
    # conversation, and a real write burst (~1–2 PATCHes/sec) can punch through a
    # small fixed count → a 409 on the must-never-fail path. Retrying against the
    # ever-fresher tail until it lands makes a bounded burst un-surfaceable as a
    # failure. Only a change WITHIN the folded region is a true conflict → stale;
    # exhausting the budget also yields stale (defensive, not expected in prod).
    boundary = plan['boundary']
    watermark = _fold_watermark(plan)
    folded_prefix_fp = _msgs_fingerprint(raw_messages[:watermark])

    budget_sec = manual_reconcile_budget_sec()
    deadline = time.monotonic() + budget_sec
    new_messages = None
    tokens_after = 0
    msgs_after = 0
    reduction_pct = 0.0
    attempts = 0
    compact_command_root = str(uuid.uuid4())
    while True:
        attempts += 1
        reloaded = store.load_transcript(conv_id, user_id=user_id)
        if reloaded is None:
            logger.warning('[ManualCompact] conv=%s vanished during compaction '
                           '(archive %s kept)', log_id, archive_id)
            aborted_receipt = _settle_aborted_archive(
                'conversation_not_found', attempts)
            return {'ok': False, 'error': 'not_found', 'archiveId': archive_id,
                    'receipt': aborted_receipt}
        cur_messages, _cur_updated_at, cur_rev = reloaded

        # RECONCILE gate: the folded region MUST be byte-identical. A concurrent
        # write here means the summarized content itself changed → real conflict.
        if _msgs_fingerprint(cur_messages[:watermark]) != folded_prefix_fp:
            logger.warning('[ManualCompact] conv=%s folded region changed under '
                           'us (real conflict) — aborting stale (archive %s kept)',
                           log_id, archive_id)
            aborted_receipt = _settle_aborted_archive(
                'folded_region_changed', attempts)
            return {'ok': False, 'error': 'stale', 'archiveId': archive_id,
                    'receipt': aborted_receipt}

        # Rebuild over the CURRENT tail so any turn appended during the summary
        # window is preserved. The folded prefix is proven byte-unchanged above,
        # so intra_folds' asst_idx (relative to ``boundary``) still address the
        # same reserve assistants.
        plan_current = dict(plan)
        plan_current['reserve_raw'] = list(cur_messages[boundary:])
        new_messages = apply_manual_compaction(
            cur_messages, plan_current, summary_text,
            archive_id=archive_id, tokens_after=0)
        tokens_after = _raw_estimate_tokens(new_messages, config)
        msgs_after = len(new_messages)
        reduction_pct = round((1 - tokens_after / max(1, tokens_before)) * 100, 1)
        # Stamp the full stats onto the summary message + its compaction marker so
        # the frontend card is a PURE render of a backend-downloaded fact (never a
        # client-side inference). See docs/modules/context_engineering.md.
        for m in new_messages:
            if isinstance(m, dict) and m.get('_isCompactionSummary'):
                m['_estimatedPromptTokens'] = int(tokens_after)
                if _folded_rounds:
                    # 档B: the card describes "folded N tool rounds", not "N msgs".
                    m['_foldedToolRounds'] = _folded_rounds
                for mk in m.get('_compactions', []):
                    mk.update({'tokensBefore': int(tokens_before),
                               'tokensAfter': int(tokens_after),
                               'msgsBefore': int(msgs_before),
                               'msgsAfter': int(msgs_after),
                               'reductionPct': reduction_pct})
                    if _folded_rounds:
                        mk['foldedToolRounds'] = _folded_rounds

        # PERSIST: CAS on the CURRENT rev (refresh msg_count+search+FTS
        # because compaction removes whole messages).
        #
        # The rev-CAS and the folded-prefix fingerprint above are ORTHOGONAL and
        # BOTH are required. The CAS proves the ROW did not move; it says nothing
        # about WHERE it moved. The fingerprint proves that what moved was
        # outside the region this summary consumed. Dropping the fingerprint
        # because "the CAS covers it" would turn every legitimate tail append
        # into a conflict, so a user pressing /compact on an active conversation
        # would get a 409. Token is ``rev``, not ``updated_at``: the latter is
        # supplied by the writer itself, so a clock that fails to advance lets
        # the predicate pass while the data has already changed.
        affected = store.compact_turn_transcript(
            conv_id, cur_messages, new_messages, cur_rev,
            command_id=(f'manual-compact:{conv_id}:'
                        f'{compact_command_root}:{cur_rev}'),
            user_id=user_id)
        if affected:
            break
        # Lost the reload→CAS race to a fresh tail write. Retry against the
        # newer tail as long as we have budget (a burst is bounded; each pass
        # picks up the newest tail). The hard iter cap is an infinite-loop guard
        # only (fires solely if the clock never advances).
        if time.monotonic() >= deadline or attempts >= _MANUAL_RECONCILE_HARD_ITER_CAP:
            logger.warning('[ManualCompact] conv=%s CAS lost %d× within the '
                           '%.1fs reconcile budget — aborting stale (archive %s '
                           'kept)', log_id, attempts, budget_sec, archive_id)
            aborted_receipt = _settle_aborted_archive(
                'reconcile_budget_exhausted', attempts)
            return {'ok': False, 'error': 'stale', 'archiveId': archive_id,
                    'receipt': aborted_receipt}
        logger.info('[ManualCompact] conv=%s reload→CAS raced (attempt %d, '
                    '%.2fs left) — reconciling against fresher tail',
                    log_id, attempts, max(0.0, deadline - time.monotonic()))

    receipt = _manual_receipt(
        'completed', summary_value=summary_text, summary_generated=True,
        reconcile_attempts=attempts)
    if archive_id is not None:
        try:
            store.update_archive_summary(
                archive_id, summary_text, int(tokens_after), int(msgs_after),
                user_id=user_id, receipt=receipt)
        except Exception as e:
            logger.debug('[ManualCompact] archive row update failed: %s', e)

    # Cross-device sync: the body was rewritten (and rev bumped), so push the
    # post-write rev → a sibling tab with this conv open refetches without a
    # manual refresh. Automatic L1 has no persistence/notification authority.
    try:
        store.notify_conversation_changed(conv_id, user_id=user_id)
    except Exception as e:
        logger.debug('[ManualCompact] conv=%s conv-changed notify skipped: %s',
                     log_id, e)

    audit_log('manual_compaction', conv_id=conv_id, archive_id=archive_id,
              tokens_before=int(tokens_before), tokens_after=int(tokens_after),
              msgs_before=int(msgs_before), msgs_after=int(msgs_after))
    logger.info('[ManualCompact] conv=%s DONE  tokens: %d → %d (%.0f%%)  '
                'msgs: %d → %d  archive=%s',
                log_id, tokens_before, tokens_after, reduction_pct,
                msgs_before, msgs_after, archive_id)

    # B — terminal push: the card swaps its live-growing text for the settled
    # boundary card (same stats the REST response carries). Best-effort.
    _push_compaction(conv_id, {
        'type': 'summary_done',
        'archiveId': archive_id,
        'tokensBefore': int(tokens_before),
        'tokensAfter': int(tokens_after),
        'tokenCountKind': 'estimated',
        'msgsBefore': int(msgs_before),
        'msgsAfter': int(msgs_after),
        'reductionPct': reduction_pct,
        'receipt': receipt,
    }, user_id=user_id)

    return {
        'ok': True,
        'archiveId': archive_id,
        'tokensBefore': int(tokens_before),
        'tokensAfter': int(tokens_after),
        'tokenCountKind': 'estimated',
        'msgsBefore': int(msgs_before),
        'msgsAfter': int(msgs_after),
        'reductionPct': reduction_pct,
        'summaryPreview': summary_text[:500],
        'receipt': receipt,
    }
