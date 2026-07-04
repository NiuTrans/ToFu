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
poll a finished task, auto-start an orphaned user turn) — it no longer INFERS
settled lifecycle state.

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


def _has_real_round(msg: dict[str, Any]) -> bool:
    """True if the message has at least one settled/result-bearing tool round."""
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
    """Endpoint / autopilot-VU / image-gen turns are never 'empty clutter'."""
    return bool(
        msg.get('_epIteration') is not None and msg.get('_epIteration') != 0
        or msg.get('_isEndpointReview') or msg.get('_isEndpointPlanner')
        or msg.get('_isVirtualUser') or msg.get('_autopilotRunId')
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
    if _has_real_round(msg):
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
    if _has_real_round(msg):
        return None
    return 'interrupt' if (msg.get('thinking') or '').strip() else 'delete'


def reconcile_conversation_messages(
    messages: list[dict[str, Any]],
    cache_prefix_count: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Server-authoritative ghost reconcile for a conversation's message list.

    Applies, in order:
      1. Buried-ghost SWEEP — remove every non-tail assistant that is a buried
         empty ghost (``is_buried_empty_ghost``).
      2. Tail classification — on the (post-sweep) trailing assistant:
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


__all__ = [
    'is_buried_empty_ghost',
    'classify_ghost_tail',
    'reconcile_conversation_messages',
]
