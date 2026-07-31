"""lib.conversations.turn_settlement — the single authoritative per-turn settlement verdict.

WHY
---
Three UI behaviours used to re-derive their own conclusion from the SAME
loosely-controlled ``finishReason`` string (stamped by ≈5 different code
paths):

  * the interrupt bubble label   (``static/js/ui/finish_info.js``),
  * the Continue-button gate      (``static/js/ui/chat_render.js``),
  * the resume-mode decision      (``scan_continue_checkpoint`` +
    ``resume_prefill_from_segments``).

That triple inference is why the Continue button over-promises (it shows for
``aborted`` / ``error`` / a missing ``finishReason`` without checking whether a
checkpoint exists) and then silently degrades to a full regeneration, and why
the user can never tell in advance whether Continue will be lossless.

This module computes ONE typed fact — the settlement verdict — so all three
consumers READ instead of RE-INFERRING. See ``docs/TURN_SETTLEMENT.md`` for the
full contract. It is a PURE function (no DB, no network, no Flask, no LLM) so
it is trivially unit-testable and recomputable on a cold reopen from the
persisted message fields.

The verdict is the SSOT for the resume-mode decision. P5 (owner-approved,
epic ``pt_c11c3a9272274848``): for a prefill-capable model with a resumable
terminal tail it reports ``prefill`` (lossless) — matching the case-2 wire the
continue route ALREADY ships (replay the tool batch + prefill the tail,
``contentPrefix`` = the full prior content). ``checkpoint`` is the honest
fallback for a tools turn the provider cannot prefill (Claude / no resumable
tail). Its job is to make the decision visible and consistent, so the Continue
button is honest about whether a resume is actually lossless.
"""

from __future__ import annotations

from typing import Any

from lib.conversations.reconcile import has_real_round
from lib.log import get_logger

logger = get_logger(__name__)

# ── Closed vocabulary: the normalized outcome of a turn ──
OUTCOME_COMPLETED = 'completed'      # clean stop — nothing to resume
OUTCOME_INTERRUPTED = 'interrupted'  # cut by an external cause (stop / restart / offline / gateway)
OUTCOME_TRUNCATED = 'truncated'      # cut by a limit (max_tokens / tool cap / safety cap)
OUTCOME_FAILED = 'failed'            # error / content_filter / abnormal_stop — no honest resume

# ── Closed vocabulary: WHY the turn ended (a single cause dimension) ──
CAUSE_MANUAL = 'manual'              # user clicked Stop
CAUSE_KILLED = 'killed'              # unclean process kill / OOM / crash
CAUSE_RESTART = 'restart'            # controlled server restart (interruptedReason='manual')
CAUSE_UNKNOWN = 'unknown'            # interrupted, but no reason tag (legacy / first_boot)
CAUSE_OFFLINE = 'offline'            # server went offline mid-turn
CAUSE_GATEWAY = 'gateway'            # gateway/proxy premature close
CAUSE_MAX_TOKENS = 'max_tokens'
CAUSE_TOOL_CAP = 'tool_cap'
CAUSE_SAFETY_CAP = 'safety_cap'
CAUSE_CONTENT_FILTER = 'content_filter'
CAUSE_ERROR = 'error'

# ── Closed vocabulary: how the turn can be resumed ──
MODE_PREFILL = 'prefill'        # continue the SAME string (lossless)
MODE_CHECKPOINT = 'checkpoint'  # replay completed tool rounds (drops the prose tail — lossy)
MODE_REGENERATE = 'regenerate'  # no honest resume — start the turn over
MODE_NONE = 'none'              # turn completed — nothing to resume

# finishReason values that mean a clean, complete stop (the green ✓ set).
_CLEAN_FINISH_REASONS = frozenset({'stop', 'end_turn', 'stop_sequence'})


def _cause_from_interrupted_reason(interrupted_reason: Any) -> str:
    """Map the backend's ``interruptedReason`` tag onto the cause vocabulary.

    Mirrors the label branch in ``finish_info.js`` exactly: ``killed`` →
    unclean kill, ``manual`` → controlled restart, absent/unknown → UNKNOWN
    (NOT restart — the bubble honestly shows "原因未知" for a legacy / first_boot
    turn, so the verdict must not over-commit it to a restart).
    """
    if interrupted_reason == 'killed':
        return CAUSE_KILLED
    if interrupted_reason == 'manual':
        return CAUSE_RESTART
    # Absent / unrecognised — the honest "unknown interrupt" the bubble shows.
    return CAUSE_UNKNOWN


def _classify_outcome(msg: dict[str, Any], finish_reason: str | None) -> tuple[str, str | None]:
    """Map a raw ``finishReason`` (+ message context) to ``(outcome, cause)``.

    A missing / unrecognised reason is classified ``interrupted`` with
    ``cause=None`` so the recovery path stays open (mirrors today's "a legacy
    turn with no finishReason still shows Continue").
    """
    fr = finish_reason
    if fr is None:
        return OUTCOME_INTERRUPTED, None
    if fr in _CLEAN_FINISH_REASONS:
        return OUTCOME_COMPLETED, None
    if fr in ('length', 'max_tokens'):
        return OUTCOME_TRUNCATED, CAUSE_MAX_TOKENS
    if fr == 'tool_rounds_exhausted':
        return OUTCOME_TRUNCATED, CAUSE_TOOL_CAP
    if fr == 'incomplete':
        return OUTCOME_TRUNCATED, CAUSE_SAFETY_CAP
    if fr == 'content_filter':
        return OUTCOME_FAILED, CAUSE_CONTENT_FILTER
    if fr in ('error', 'abnormal_stop'):
        return OUTCOME_FAILED, CAUSE_ERROR
    if fr == 'interrupted':
        return OUTCOME_INTERRUPTED, _cause_from_interrupted_reason(msg.get('interruptedReason'))
    if fr == 'server_offline':
        return OUTCOME_INTERRUPTED, CAUSE_OFFLINE
    if fr == 'premature_close':
        return OUTCOME_INTERRUPTED, CAUSE_GATEWAY
    if fr == 'aborted':
        return OUTCOME_INTERRUPTED, CAUSE_MANUAL
    # Unknown / future reason — keep the recovery path open.
    return OUTCOME_INTERRUPTED, None


def _is_empty_turn(msg: dict[str, Any]) -> bool:
    """True if the turn produced no user-visible payload at all.

    Aligns with the ``chat_continue`` empty-guard (content AND thinking AND
    tool rounds all absent) and reuses the SAME ``has_real_round`` predicate
    the reconcile uses, so the verdict can never drift from the ghost sweep.
    """
    if (msg.get('content') or '').strip():
        return False
    if (msg.get('thinking') or '').strip():
        return False
    return not has_real_round(msg)


def _resume(*, mode: str, lossless: bool, reason: str,
            kept_rounds: int = 0, prefill_chars: int = 0) -> dict[str, Any]:
    return {
        'mode': mode,
        'lossless': lossless,
        'keptRounds': kept_rounds,
        'prefillChars': prefill_chars,
        'reason': reason,
    }


def _compute_resume(msg: dict[str, Any], *, outcome: str, finish_reason: str | None,
                    model: str | None, segments: Any) -> dict[str, Any]:
    """Decide HOW the turn can be resumed — once, here, not per consumer.

    P5 precedence (owner-approved flip): prefill BEFORE checkpoint for a
    capable model with a resumable tail (the route's case-2 wire is already
    lossless there); checkpoint is the fallback for a tools turn the provider
    can't prefill. Fail-closed: any uncertainty degrades to ``regenerate``
    (the current fallback), never to a riskier resume.
    """
    if outcome == OUTCOME_COMPLETED:
        return _resume(mode=MODE_NONE, lossless=False, reason='turn_completed')
    if _is_empty_turn(msg):
        return _resume(mode=MODE_REGENERATE, lossless=False, reason='empty_turn')

    # ── Compute BOTH resume anchors. ──
    # kept_rounds (checkpoint scan) — the completed tool rounds (replayed either
    # way; reused from the authoritative scanner /api/chat/continue calls so the
    # keptRounds the verdict reports is EXACTLY what Continue uses).
    kept_rounds = 0
    try:
        from lib.chat.turn_builder import scan_continue_checkpoint
        scan = scan_continue_checkpoint(msg)
        if scan:
            kept_rounds = len(scan.get('kept_rounds') or [])
    except Exception as e:  # fail-closed — never let verdict computing break settle
        logger.debug('[Settlement] checkpoint scan failed: %s', e)

    # prefill gate — a prefill-capable model + a resumable finish + a terminal
    # deliverable tail to continue.
    content = (msg.get('content') or '')
    prefill_ok = False
    try:
        from lib.tasks_pkg.segments._types import RESUMABLE_FINISH_REASONS
        resumable = (finish_reason or '') in RESUMABLE_FINISH_REASONS
    except Exception as e:
        logger.debug('[Settlement] RESUMABLE_FINISH_REASONS import failed: %s', e)
        resumable = False
    if resumable and content.strip() and model:
        try:
            from lib.model_info import model_supports_assistant_prefill
            prefill_ok = bool(model_supports_assistant_prefill(model))
        except Exception as e:
            logger.debug('[Settlement] prefill capability probe failed (fail-closed): %s', e)

    # ── P5 flip (owner-approved): prefer prefill over checkpoint for a capable
    # model with a resumable tail. The continue route ALREADY ships this
    # lossless case-2 wire (replay the tool batch + prefill the tail,
    # contentPrefix = the full prior content) whenever _resume_prefill is set —
    # so reporting prefill makes the verdict (and the Continue button) HONEST
    # about the losslessness the route already delivers. keptRounds is still
    # surfaced (the route replays those rounds alongside the prefill).
    if prefill_ok:
        return _resume(mode=MODE_PREFILL, lossless=True,
                       reason='prefill_continue', kept_rounds=kept_rounds,
                       prefill_chars=len(content))

    # ── Checkpoint fallback: a tools turn the provider can't prefill (Claude /
    # no resumable tail) — replay the completed rounds and REGENERATE the tail
    # (lossy, the only safe option there).
    if kept_rounds > 0:
        return _resume(mode=MODE_CHECKPOINT, lossless=False,
                       reason='tool_checkpoint', kept_rounds=kept_rounds)

    return _resume(mode=MODE_REGENERATE, lossless=False,
                   reason='no_checkpoint_no_prefill')


def compute_turn_settlement(msg: dict[str, Any], *, model: str | None = None,
                            segments: Any = None) -> dict[str, Any] | None:
    """Compute the single authoritative settlement verdict for an assistant turn.

    Pure and deterministic over the persisted message fields, so a cold reopen
    recomputes the identical verdict for a legacy message that lacks a stamped
    ``_settlement``.

    Args:
        msg: the assistant message dict (``role == 'assistant'``).
        model: the model id (for the prefill-capability gate). May be None →
            prefill is declined (fail-closed to checkpoint/regenerate).
        segments: the persisted typed-segment list, when available. Reserved
            for the future prefill-over-checkpoint refinement (§5 P5); the
            current verdict derives the deliverable from ``msg['content']`` so
            it is recomputable without segments.

    Returns:
        The verdict dict ``{outcome, finishReason, cause, resume}``, or None
        when ``msg`` is not an assistant turn.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return None
    finish_reason = (msg.get('finishReason') or '').strip() or None
    outcome, cause = _classify_outcome(msg, finish_reason)
    resume = _compute_resume(msg, outcome=outcome, finish_reason=finish_reason,
                             model=model, segments=segments)
    return {
        'outcome': outcome,
        'finishReason': finish_reason,
        'cause': cause,
        'resume': resume,
    }


__all__ = [
    'OUTCOME_COMPLETED',
    'OUTCOME_INTERRUPTED',
    'OUTCOME_TRUNCATED',
    'OUTCOME_FAILED',
    'CAUSE_MANUAL',
    'CAUSE_KILLED',
    'CAUSE_RESTART',
    'CAUSE_UNKNOWN',
    'CAUSE_OFFLINE',
    'CAUSE_GATEWAY',
    'CAUSE_MAX_TOKENS',
    'CAUSE_TOOL_CAP',
    'CAUSE_SAFETY_CAP',
    'CAUSE_CONTENT_FILTER',
    'CAUSE_ERROR',
    'MODE_PREFILL',
    'MODE_CHECKPOINT',
    'MODE_REGENERATE',
    'MODE_NONE',
    'compute_turn_settlement',
]
