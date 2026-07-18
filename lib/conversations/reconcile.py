"""Backend-authoritative turn-end reconcile for conversation messages.

WHY
---
Historically the frontend `initActiveTasks` Case-D path CLASSIFIED trailing and
buried assistant messages as ghost / interrupted / delete / keep by inspecting
content-length + role in JS (`_classifyGhostTail`, `_isBuriedEmptyGhost`,
`_sweepBuriedGhostAssistants` in ``static/js/main/main_init_tasks.js``). That is
exactly the frontend-only lifecycle *inference* the separation-of-concerns
directive forbids — and it was the source of two regressions:

  * the buried-ghost SWEEP not persisting (the ``allowTruncate`` resurrect bug),
    so swept ghosts came back on every reload; and
  * a ghost ``delete`` pop falling through and auto-firing an unrequested LLM
    turn (the Case-D → Case-E leak).

This module moves the VERDICT to the backend. It is a PURE function (no DB, no
network, no Flask) so it is trivially unit-testable and can be called from any
server context — currently ``recover_stale_tasks_on_startup`` (persists the
cleaned messages in the same commit that recovers the conversation, so there is
no frontend PUT to lose → the resurrect bug is structurally impossible, and no
frontend pop → the auto-fire leak is structurally impossible).

The frontend keeps ONLY the network/DOM ORCHESTRATION (reconnect to a live SSE,
poll a finished task) — it no longer INFERS settled lifecycle state.

Verdict vocabulary (per message, mirrors the JS classifiers byte-for-byte in
predicate logic):
  * A BURIED (non-tail) assistant that carries NO user-visible payload is
    SWEPT (removed) — even if it has a settled finishReason/usage, because
    mid-list it renders as a body-less badge-only bubble = pure clutter.
  * The TRAILING assistant, if a ghost (empty content, no finishReason/usage/
    error, no real tool round):
      - a bare empty husk (no thinking) → DELETE (removed);
      - a thinking-only husk → INTERRUPT (stamp finishReason='interrupted',
        preserving recovered reasoning) — NOT deleted.
  * Everything else is KEPT untouched.

Special turns (endpoint planner/critic/worker, autopilot VU, image-gen) are
NEVER treated as empty clutter even with empty content.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def has_real_round(msg: dict[str, Any]) -> bool:
    """True if the message has at least one settled/result-bearing tool round.

    PUBLIC: the single project-wide definition of "does this turn carry a real
    tool round?". Imported by the terminal write path
    (``manager._sync_result_to_conversation``) so its empty-content skip guard
    shares this exact predicate rather than re-deriving a parallel one — the
    "multiple copies drift" trap this codebase keeps hitting. ``_has_real_round``
    remains as a private alias for existing in-tree importers.
    """
    rounds = msg.get('toolRounds')
    if not isinstance(rounds, list):
        return False
    for r in rounds:
        if not isinstance(r, dict):
            continue
        if r.get('status') == 'done' or r.get('toolContent'):
            return True
        results = r.get('results')
        if isinstance(results, list) and results:
            return True
    return False


def _is_special_turn(msg: dict[str, Any]) -> bool:
    """Endpoint / auto-initiated / image-gen turns are never 'empty clutter'.

    Endpoint (planner/critic/worker-iteration) and image-gen markers stay
    local (they are turn-KIND, not initiator). "Was this auto-initiated?" is
    resolved through the ONE shared resolver — so autopilot-VU AND every other
    auto-initiated source (proactive / timer / brain / peer / operator / swarm)
    is protected from the ghost sweep, not just the two markers this predicate
    used to hardcode.
    """
    from lib.conversations.turn_initiation import is_auto_initiated
    return bool(
        msg.get('_epIteration') is not None and msg.get('_epIteration') != 0
        or msg.get('_isEndpointReview') or msg.get('_isEndpointPlanner')
        or is_auto_initiated(msg)
        or msg.get('_igResult') or msg.get('_igResults') or msg.get('_igError')
    )


def is_buried_empty_ghost(msg: dict[str, Any]) -> bool:
    """Port of the JS ``_isBuriedEmptyGhost`` predicate.

    A BURIED (non-tail) assistant placeholder with NO user-visible payload:
    empty content, empty thinking, no error, no real tool round, and not a
    special turn. Intentionally removes even a settled-but-bodyless bubble
    (aborted/interrupted with no content) because mid-list it is pure clutter.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    if _is_special_turn(msg):
        return False
    if (msg.get('content') or '').strip():
        return False
    if (msg.get('thinking') or '').strip():
        return False
    if msg.get('error'):
        return False
    if has_real_round(msg):
        return False
    return True


def classify_ghost_tail(msg: dict[str, Any]) -> str | None:
    """Port of the JS ``_classifyGhostTail``.

    Returns 'delete' | 'interrupt' | None for a TRAILING assistant message.
    A ghost tail is an assistant turn with no settled output (empty content, no
    finishReason/usage/error, no real tool round). A bare husk → 'delete'; a
    thinking-only husk → 'interrupt' (preserve recovered reasoning). Anything
    settled → None (leave it).
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return None
    if msg.get('content') or msg.get('finishReason') or msg.get('usage') or msg.get('error'):
        return None
    if has_real_round(msg):
        return None
    return 'interrupt' if (msg.get('thinking') or '').strip() else 'delete'


def _is_settled_assistant(msg: dict[str, Any]) -> bool:
    """True if ``msg`` is an assistant turn carrying a genuine settled reply
    (real content, a finish reason, usage, or a result-bearing tool round).

    NOTE: an assistant whose ONLY payload is an ``error`` does NOT count as a
    settled reply here — an error husk cannot 'supersede' another error husk.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    return bool(
        (msg.get('content') or '').strip()
        or msg.get('finishReason')
        or msg.get('usage')
        or has_real_round(msg)
    )


def is_error_husk(msg: dict[str, Any]) -> bool:
    """A buried PURE-error assistant husk: carries an ``error`` but NO other
    user-visible payload (empty content, empty thinking, no real tool round,
    not a special turn).

    This is precisely the ``errAssistant`` the frontend pushes on a
    timeout/abort of an edit/regen/send (``main_regen_continue.js`` /
    ``edit_message.js`` / ``main_send_pipeline.js``): ``{role:'assistant',
    content:'', thinking:'', error:…, toolRounds:[]}``. The buried-ghost sweep
    (``is_buried_empty_ghost``) deliberately SKIPS it (``if msg.get('error')``),
    because in isolation an error block is real user-visible information — so
    when it is NOT superseded it must be kept. It is only clutter when a real
    reply landed right after it (see ``is_superseded_error_husk``).
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    if _is_special_turn(msg):
        return False
    if not msg.get('error'):
        return False
    if (msg.get('content') or '').strip():
        return False
    if (msg.get('thinking') or '').strip():
        return False
    if has_real_round(msg):
        return False
    return True


def is_superseded_error_husk(
    messages: list[dict[str, Any]], idx: int,
) -> bool:
    """True if ``messages[idx]`` is a buried pure-error husk (``is_error_husk``)
    that is DIRECTLY superseded by a settled assistant reply at ``idx+1``.

    This is the late-recovery artifact: the client's edit/regen/send safety
    timer fired and pushed a visible error bubble AFTER the ~30s recovery
    window; the server task then actually appeared, and a later orphan-recovery
    reconnect appended the REAL assistant right below the error. The result is a
    persisted ``[user, error-husk, real-assistant]`` — a "user → error-bubble →
    agent" duplicate for a SINGLE logical exchange. Once the real reply landed,
    the error bubble is stale clutter and is collapsed away.

    Guard rails (deliberately narrow — only the exact artifact, nothing else):
      * ``idx+1`` must be a genuinely SETTLED assistant (``_is_settled_assistant``
        — real content / finishReason / usage / result-bearing round). An error
        husk followed by ANOTHER error husk, or by a still-empty placeholder, or
        by a user turn (a different exchange), is NOT collapsed.
    """
    if idx < 0 or idx + 1 >= len(messages):
        return False
    if not is_error_husk(messages[idx]):
        return False
    return _is_settled_assistant(messages[idx + 1])


def is_superseded_incomplete_fragment(
    messages: list[dict[str, Any]], idx: int,
) -> bool:
    """True if ``messages[idx]`` is a content-bearing assistant fragment that
    carries NO terminal reason (``finishReason`` falsy) and sits DIRECTLY
    adjacent to a genuinely-settled assistant sibling (an assistant with a
    truthy ``finishReason``) for the SAME user turn — i.e. two assistant
    messages in a row with no user turn between them.

    ROOT CAUSE this guards (the reported "truncated answer shown as a finished
    turn" bug): a Stop→Regenerate leaves the aborted task's partial-checkpoint
    fragment persisted with real content but ``finishReason=None`` (the
    checkpoint writer omits terminal fields on purpose), while the regenerate's
    completed answer lands as a sibling assistant message. The fragment then
    renders in full completed chrome (action row, translate footer, branch
    button) even though it is a truncated abort partial. The BACKEND source
    stamp (``_stamp_aborted_fragment_finish_reason``) closes this at write time,
    but this predicate is the reconcile safety net for fragments already in the
    DB (or written by a path that had no stable ``_assistantMsgId`` to target).

    The verdict is a MARK, not a delete: the caller stamps
    ``finishReason='aborted'`` so the content is PRESERVED but renders as the
    aborted partial it truthfully is. Deliberately narrow — fires ONLY on the
    exact two-answers-one-turn artifact:

      * ``messages[idx]`` is an assistant with non-empty content, NO
        finishReason, and is not a special turn.
      * A DIRECTLY-adjacent assistant sibling (``idx-1`` or ``idx+1``) is
        genuinely settled AND carries a truthy ``finishReason``. Adjacency with
        no user turn between is what proves they answer the SAME user turn.
    """
    if idx < 0 or idx >= len(messages):
        return False
    frag = messages[idx]
    if not isinstance(frag, dict) or frag.get('role') != 'assistant':
        return False
    if _is_special_turn(frag):
        return False
    if frag.get('finishReason'):
        return False
    # An `error` field is itself a truthful terminal state (the bubble renders
    # as a failed reply, not completed chrome) — that is the error-husk pass's
    # domain, not this one. Never re-stamp it 'aborted'.
    if frag.get('error'):
        return False
    if not (frag.get('content') or '').strip():
        return False

    def _settled_sibling(j: int) -> bool:
        if j < 0 or j >= len(messages):
            return False
        sib = messages[j]
        return (isinstance(sib, dict) and sib.get('role') == 'assistant'
                and bool(sib.get('finishReason')) and _is_settled_assistant(sib))

    return _settled_sibling(idx - 1) or _settled_sibling(idx + 1)


def mark_superseded_incomplete_fragments(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Stamp ``finishReason='aborted'`` on every superseded incomplete fragment
    in ``messages`` (see :func:`is_superseded_incomplete_fragment`).

    Pure + in-place-safe: it MUTATES a field on matched entries (copying each
    matched dict so the caller's originals are untouched) and NEVER deletes or
    reindexes — so it is both cache-prefix-neutral (``finishReason`` is not a
    wire fingerprint field) and safe to run inside a terminal write path that
    must not change message COUNT. Returns ``(messages, marked_count)``.

    Two call sites share this ONE implementation: the GET/startup-path
    ``reconcile_conversation_messages`` (render self-heal) and the terminal
    sync write of the SUPERSEDING task (so a Stop→Regenerate self-heals the
    aborted sibling at the regenerate's settle, not only on the next GET).
    """
    marked = 0
    for i in range(len(messages)):
        if is_superseded_incomplete_fragment(messages, i):
            m = dict(messages[i])
            m['finishReason'] = 'aborted'
            messages[i] = m
            marked += 1
    return messages, marked


ORPHAN_RESUMABLE_MAX_AGE_MS = 24 * 60 * 60 * 1000  # 24h


def _last_real_turn(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the authoritative trailing turn, or None for an empty list.

    The orphan classifier runs AFTER reconcile has swept buried ghosts and
    deleted/stamped a ghost trailing assistant, so by the time we classify an
    orphan the tail is the genuine settled tail.
    """
    if not isinstance(messages, list) or not messages:
        return None
    return messages[-1] if isinstance(messages[-1], dict) else None


def classify_orphan_resumable(
    messages: list[dict[str, Any]],
    *,
    has_live_task: bool,
    now_ms: int,
    max_age_ms: int = ORPHAN_RESUMABLE_MAX_AGE_MS,
) -> dict[str, Any] | None:
    """Backend-authoritative "does this conv have an orphaned user turn that
    needs a response?" verdict — the fundamental fix for the frontend Case-E
    ``age<5min`` heuristic that could AUTO-FIRE a billed LLM turn.

    Returns a structured marker dict when the AUTHORITATIVE message list ends in
    a user turn with NO assistant response and NO live task — else None. The
    frontend renders an explicit Resume affordance from this marker; it NEVER
    auto-dispatches. Because it consults the real ``messages`` (not the stale
    ``settings.lastMsgRole`` shell metadata), it closes the latent DOUBLE-ANSWER
    bug: a conv whose real tail is already an assistant answer is NOT marked
    resumable — the exact case a metadata-only frontend cannot verify.

    Args:
        messages: the AUTHORITATIVE, already-reconciled message list.
        has_live_task: True if a pending/running task exists for this conv
            (caller supplies it from the runtime — same signal as the GET-path
            live-task gate). A live task means a response IS coming; not orphaned.
        now_ms: current epoch millis (injected for deterministic testing).
        max_age_ms: freshness bound (server policy, default 24h).

    Returns:
        ``{'msgIndex': int, 'timestamp': int, 'isImageGen': bool}`` or None.
    """
    if has_live_task:
        return None
    tail = _last_real_turn(messages)
    if tail is None or tail.get('role') != 'user':
        return None
    # An image-gen user turn is driven by the creative-mode pipeline, NOT the
    # orchestrator; never offer it to startAssistantResponse.
    content = tail.get('content')
    is_image_gen = bool(
        tail.get('_isImageGen')
        or (isinstance(content, str) and content.startswith('\U0001f3a8 '))
    )
    ts = tail.get('timestamp')
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    if (now_ms - int(ts)) > max_age_ms:
        return None
    return {
        'msgIndex': len(messages) - 1,
        'timestamp': int(ts),
        'isImageGen': is_image_gen,
    }


def reconcile_conversation_messages(
    messages: list[dict[str, Any]],
    cache_prefix_count: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Server-authoritative ghost reconcile for a conversation's message list.

    Applies, in order:
      1. Superseded-error-husk COLLAPSE — remove a buried pure-error husk that
         is directly followed by a settled assistant reply
         (``is_superseded_error_husk``); the late-recovery
         ``[user, error-husk, real-assistant]`` "user → error → agent"
         duplicate.
      2. Buried-ghost SWEEP — remove every non-tail assistant that is a buried
         empty ghost (``is_buried_empty_ghost``).
      3. Tail classification — on the (post-sweep) trailing assistant:
           'delete'    → drop it;
           'interrupt' → stamp ``finishReason='interrupted'`` in place.

    Pure: takes and returns plain dicts; performs NO DB/network I/O and NEVER
    auto-starts a turn (the removal of a ghost is a cleanup, never a trigger —
    this is what makes the Case-D→Case-E auto-fire leak impossible server-side).

    ``cache_prefix_count`` is the number of LEADING messages the prompt cache
    treats as immutable (from ``cache_tracking.get_cache_prefix_count``). The
    buried-ghost sweep NEVER removes a message at index < cache_prefix_count:
    deleting an in-prefix message shifts every following byte and busts the
    Anthropic tail-breakpoint cache for the whole prefix. Default 0 (no live
    cache) preserves the original behaviour byte-for-byte — the startup caller,
    which runs when cache state is empty post-restart, passes nothing. The
    future GET-path caller MUST pass the live prefix count so a mid-session
    reconcile stays cache-neutral.

    Returns ``(reconciled_messages, changed)``. ``changed`` is False when
    nothing was swept/deleted/stamped, so the caller can skip a needless write.
    """
    if not isinstance(messages, list) or len(messages) == 0:
        return messages, False

    changed = False
    out = list(messages)

    # ── 0. Superseded-error-husk collapse (the late-recovery artifact) ──
    #    Remove a buried pure-error husk directly followed by a settled reply:
    #    [user, error-husk, real-assistant] → [user, real-assistant]. Runs
    #    FIRST so the following sweep/tail passes see the cleaned list. Honours
    #    the SAME cache-prefix guard as the sweep — never remove an in-prefix
    #    message (it would shift prefix bytes and bust the prompt cache).
    if len(out) >= 2:
        _guard = max(0, cache_prefix_count)
        kept: list[dict[str, Any]] = []
        collapsed = 0
        for i, m in enumerate(out):
            if i >= _guard and is_superseded_error_husk(out, i):
                collapsed += 1
                continue
            kept.append(m)
        if collapsed:
            out = kept
            changed = True
            logger.info('[Reconcile] Collapsed %d superseded error-husk(s) '
                        '(late-recovery user\u2192error\u2192agent duplicate). '
                        'Remaining=%d', collapsed, len(out))

    # ── 0b. Superseded-incomplete-fragment MARK (two answers, one turn) ──
    #    Stamp finishReason='aborted' on a content-bearing assistant fragment
    #    that has NO terminal reason but is directly adjacent to a settled
    #    assistant sibling for the same user turn. This is the render-side
    #    safety net for the "truncated abort fragment shown as a finished turn"
    #    bug (the backend source stamp closes it at write time; this catches
    #    fragments already in the DB). It is a MARK, never a delete — content is
    #    preserved and now renders as the aborted partial it truthfully is.
    #    No cache-prefix concern: this MUTATES a field in place (no reindex),
    #    so it never shifts prefix bytes.
    if len(out) >= 2:
        out, _marked = mark_superseded_incomplete_fragments(out)
        if _marked:
            changed = True
            logger.info('[Reconcile] Marked %d superseded incomplete fragment(s) '
                        'finishReason=aborted (truncated partial adjacent to a '
                        'settled answer for the same turn).', _marked)

    # ── 1. Buried-ghost sweep (all but the tail) ──
    if len(out) >= 2:
        last_idx = len(out) - 1
        _guard = max(0, cache_prefix_count)
        kept: list[dict[str, Any]] = []
        swept = 0
        for i, m in enumerate(out):
            # Never sweep a message inside the immutable cache prefix —
            # removing it shifts the prefix bytes and busts the cache.
            if i < _guard:
                kept.append(m)
                continue
            if i < last_idx and is_buried_empty_ghost(m):
                swept += 1
                continue
            kept.append(m)
        if swept:
            out = kept
            changed = True
            logger.info('[Reconcile] Swept %d buried empty-ghost assistant '
                        'placeholder(s) (mid-list clutter). Remaining=%d',
                        swept, len(out))

    # ── 2. Tail classification ──
    if out:
        verdict = classify_ghost_tail(out[-1])
        if verdict == 'delete':
            out = out[:-1]
            changed = True
            logger.info('[Reconcile] Removed ghost empty trailing assistant '
                        '(started but produced no token). Remaining=%d', len(out))
        elif verdict == 'interrupt':
            # Stamp in place on a shallow copy so we don't mutate the caller's dict.
            tail = dict(out[-1])
            tail['finishReason'] = 'interrupted'
            out = out[:-1] + [tail]
            changed = True
            logger.info('[Reconcile] Stamped finishReason=interrupted on ghost '
                        'thinking-only trailing assistant (preserving reasoning).')

    return out, changed


# Backward-compat private alias for existing in-tree importers
# (tests/test_reconcile_js_backend_equivalence.py, etc.). The public name
# `has_real_round` is the one new code should import.
_has_real_round = has_real_round


__all__ = [
    'has_real_round',
    'is_buried_empty_ghost',
    'classify_ghost_tail',
    'is_error_husk',
    'is_superseded_error_husk',
    'is_superseded_incomplete_fragment',
    'mark_superseded_incomplete_fragments',
    'reconcile_conversation_messages',
    'classify_orphan_resumable',
    'ORPHAN_RESUMABLE_MAX_AGE_MS',
]
