"""lib/agent_verdict/_stuck.py — Non-convergence detectors for the agent loops.

Two independent guards:

  * Jaccard "stuck" detection on consecutive verifier feedbacks
    (``STUCK_JACCARD`` / ``_jaccard`` / ``detect_stuck``);
  * the diminishing-returns / no-value-progress guard on the per-turn ledger
    (``DIMINISHING_*`` constants / ``autopilot_progress_window`` /
    ``detect_diminishing_returns``).

Pure logic — imports only the standard library and ``lib.log``.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Stuck detection
# ══════════════════════════════════════════════════════════

STUCK_JACCARD = 0.60


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of two strings' lowercased word sets (0.0 when
    either side is empty)."""
    sa = set((a or '').lower().split())
    sb = set((b or '').lower().split())
    if not sa or not sb:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def detect_stuck(feedback_history, *, threshold: float = STUCK_JACCARD,
                 window: int = 2) -> bool:
    """Return True when the loop is repeating itself and not converging.

    Compares the last ``window`` entries of ``feedback_history`` pairwise
    (consecutive) on Jaccard word-set similarity; returns True iff EVERY
    adjacent pair in that window exceeds ``threshold`` — i.e. the verifier
    emitted ``window`` near-identical messages in a row.

    ``window`` defaults to 2: compare the last two feedbacks and return True
    when their overlap exceeds ``threshold``. Flow loops use that default;
    Autopilot passes ``window=3`` (see
    :data:`AUTOPILOT_STUCK_WINDOW`): two near-identical VU nudges can be a
    legitimate "you didn't do it, try again", but three in a row is a genuine
    non-converging loop.
    """
    if window < 2:
        window = 2
    if not feedback_history or len(feedback_history) < window:
        return False
    tail = feedback_history[-window:]
    for i in range(len(tail) - 1):
        if _jaccard(tail[i], tail[i + 1]) <= threshold:
            return False
    return True


# ══════════════════════════════════════════════════════════
#  Diminishing-returns / no-value-progress guard
# ══════════════════════════════════════════════════════════
#
#  WHY this exists — repetition (Jaccard) detection is NECESSARY BUT NOT
#  SUFFICIENT.  A verifier that keeps flagging a genuine-but-tiny item emits
#  NON-similar feedback each turn (so ``detect_stuck`` never fires) while the
#  worker ships a REAL edit each turn (so the zero-deliverable guard never
#  fires) — yet the loop makes no net progress toward the objective, burning
#  its whole budget polishing a triviality or tuning the same parameter.  This
#  guard catches exactly that: state-changing work that RE-TOUCHES the same
#  targets without resolving NEW objective items.
#
#  The signal is HARD, not prose-heuristic: the virtual user emits a structured
#  ``[PROGRESS: resolved=X remaining=Y]`` line each turn (X = acceptance-criteria
#  items verified DONE so far).  The per-turn ``resolved_delta`` (new items this
#  turn) + the worker's touched-file set feed a small ledger; the guard fires
#  only when the whole window carries a hard progress signal AND shows churn
#  without net progress.  Absent the structured signal it FAILS OPEN (cannot
#  prove no-progress → never fires).

DIMINISHING_WINDOW = 4          # consecutive edit-shipping turns to consider
DIMINISHING_MIN_RESOLVED = 1    # net NEW resolved items below this = no progress
DIMINISHING_TARGET_OVERLAP = 0.5  # per-pair touched-file Jaccard = "same spot"


def autopilot_progress_window() -> int:
    """Diminishing-returns window (``TOFU_AUTOPILOT_PROGRESS_WINDOW``, default 4).

    FAIL-OPEN like :func:`autopilot_max_turns`: unset→default, ``0``/<=1→
    DISABLED (the guard never fires), garbage→default.  A window < 2 is
    meaningless (need at least two turns to see churn) so it disables.
    """
    raw = os.environ.get('TOFU_AUTOPILOT_PROGRESS_WINDOW', '').strip()
    if not raw:
        return DIMINISHING_WINDOW
    try:
        val = int(raw)
    except (ValueError, TypeError):
        logger.warning('[Autopilot] TOFU_AUTOPILOT_PROGRESS_WINDOW=%r not an int '
                       '— using default %d', raw, DIMINISHING_WINDOW)
        return DIMINISHING_WINDOW
    return val if val >= 2 else 0


def detect_diminishing_returns(ledger, *, window: int = DIMINISHING_WINDOW,
                               min_resolved: int = DIMINISHING_MIN_RESOLVED,
                               overlap: float = DIMINISHING_TARGET_OVERLAP) -> bool:
    """True when the last ``window`` turns churn without net objective progress.

    ``ledger`` is a list of per-turn dicts (oldest→newest), each:
        ``{'resolved_delta': int | None, 'targets': list[str]}``
    where ``resolved_delta`` is the number of NEW acceptance-criteria items the
    VU verified done that turn (``None`` when the turn had no parseable
    ``[PROGRESS]`` line), and ``targets`` is the set of files the worker touched
    that turn.

    Fires (returns True) iff ALL hold over the last ``window`` entries:
      (a) every turn shipped edits (non-empty ``targets``) — this is churn, not
          legitimate read-only investigation;
      (b) every turn carries a hard progress signal (no ``None`` delta) — else
          FAIL OPEN, we cannot prove no-progress;
      (c) the NET new resolved items across the window is ``< min_resolved``
          (essentially zero forward progress); AND
      (d) every consecutive turn re-touched a substantially overlapping target
          set (per-pair Jaccard ``>= overlap``) — re-editing the same spot.

    ``window <= 1`` disables the guard (returns False) — used by the env
    kill-switch.
    """
    if window <= 1:
        return False
    if not ledger or len(ledger) < window:
        return False
    tail = ledger[-window:]

    target_sets = []
    for e in tail:
        tg = (e or {}).get('targets') or []
        if not tg:
            return False  # (a) a read-only / no-edit turn breaks the churn run
        target_sets.append(set(tg))

    deltas = [(e or {}).get('resolved_delta') for e in tail]
    if any(d is None for d in deltas):
        return False  # (b) missing hard signal → fail open

    if sum(deltas) >= min_resolved:
        return False  # (c) real net progress → not stuck

    for i in range(len(target_sets) - 1):
        a, b = target_sets[i], target_sets[i + 1]
        union = a | b
        j = (len(a & b) / len(union)) if union else 0.0
        if j < overlap:
            return False  # (d) turns touched different areas → not fixation
    return True
