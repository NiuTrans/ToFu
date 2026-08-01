"""Terminal-signal gate — ONE definition of "may this snapshot advertise that
the turn is over?", shared by every non-terminal snapshot transport.

WHY THIS MODULE EXISTS
----------------------
``finishReason`` / ``usage`` are TERMINAL signals: the frontend reads either one
as "this turn has settled". ``static/js/ui/finish_info.js`` states the contract
outright::

    const _terminal = msg.finishReason || msg.usage;

and its comment asserts the backend's side of the deal — "the mid-stream
checkpoint deliberately writes ``model`` but WITHHOLDS finishReason/usage until
completion".

That assertion was NOT true on the wire. ``lib/tasks_pkg/orchestrator/_finalize.py``
stamps ``task['finishReason']`` ~111 lines BEFORE it flips
``task['status'] = 'done'``, and the intervening span contains the BLOCKING
``_generate_tool_summary`` LLM call — so the window is seconds wide, not
microseconds. Every snapshot builder that copied metadata with a bare
``if meta.get(key)`` therefore emitted the self-contradictory pair::

    {"status": "running", "finishReason": "stop"}

Consequences measured on 2026-07-31 (conv ms8c0645hwl327): the frontend's
``assistantTailIsPriorTurn`` classified the task's OWN live bubble as a finished
prior turn, ``connectToTask`` pushed a fresh placeholder, the deltas moved to
it, and the original bubble froze mid-sentence while BOTH rendered — one
conversation-message entry, two agent bubbles. A second, independent consumer
(``renderFinishInfo``) paints a settled finish bar on a still-generating turn
from the same contradiction; it never passes through the reducer, which is why
the frontend guard alone cannot cover this.

The contradiction was reachable from THREE snapshot builders (poll in-memory,
poll DB-row, SSE ``state``). Gating each one separately is how the four
metadata paths drifted before (see ``extract_task_meta``'s docstring). So the
rule lives HERE, once, and every builder calls :func:`filtered_snapshot_meta`.

WHY NOT INSIDE ``extract_task_meta``
------------------------------------
Its output feeds BOTH non-terminal ``state`` snapshots AND genuinely terminal
``done`` events (``lib/chat_dispatch.py`` late/synthetic done,
``routes/chat.py`` late done), which MUST carry the terminal fields. Gating
there would strip them from the very events whose purpose is to deliver them.
The gate belongs at the SNAPSHOT boundary, keyed on the status that snapshot is
about to report — which is exactly what this module is.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# Task statuses that mean "this turn is over". Derived from the real status
# vocabulary written across lib/ + routes/ ('done' / 'error' / 'aborted' are
# assigned by the orchestrator and endpoint loops; 'interrupted' is synthesized
# by the poll/recovery path for a task whose process died mid-run).
TERMINAL_STATUSES = frozenset({'done', 'error', 'aborted', 'interrupted'})

# Fields that MEAN "the turn has settled" and must never ride a non-terminal
# snapshot.
#
# Scope is deliberately NARROW — exactly the fields the frontend's terminal
# predicate reads, plus `preset`, which is stamped in the SAME assignment block
# as finishReason/usage in _finalize.py and so shares their timing.
#
# NOT gated (each for a measured reason):
#   * `model` / `thinkingDepth` / `provider_id` — set at task birth, needed
#     mid-stream to render the model tag on the live bubble. finish_info.js
#     explicitly treats a model-only message as NOT terminal.
#   * `toolRounds` / `content` / `thinking` / `phase` — live progress; this gate
#     must never slow the stream down.
#   * `error` — an error can legitimately surface mid-run and the UI must show
#     it immediately; it is not a settlement claim.
#   * `apiRounds` / `modifiedFiles` / `modifiedFileList` / `toolSummary` — grow
#     during the turn and are read as progress, not as settlement.
TERMINAL_ONLY_KEYS = frozenset({'finishReason', 'usage', 'preset'})


def is_terminal_status(status: str | None) -> bool:
    """True when ``status`` means the turn is over."""
    return (status or '') in TERMINAL_STATUSES


def filtered_snapshot_meta(meta: dict, status: str | None) -> dict:
    """Return ``meta`` with terminal-only fields removed unless ``status`` is terminal.

    THE single chokepoint for the "a running snapshot must not claim the turn is
    finished" invariant. Call it with the metadata dict you are about to splice
    into a snapshot and the status that SAME snapshot reports, so the two can
    never disagree.

    Args:
        meta: metadata dict (from ``extract_task_meta`` / ``extract_db_meta``).
        status: the status this snapshot is about to report — NOT a separately
            re-read ``task['status']``, which may have advanced since.

    Returns:
        A new dict. Terminal-status snapshots pass through unchanged; otherwise
        the terminal-only keys are dropped.
    """
    if not meta:
        return {}
    if is_terminal_status(status):
        return dict(meta)
    dropped = [k for k in meta if k in TERMINAL_ONLY_KEYS]
    out = {k: v for k, v in meta.items() if k not in TERMINAL_ONLY_KEYS}
    if dropped:
        # Not a warning: this is the gate doing its job during the finalize
        # window. Logged so the window is observable when diagnosing a
        # "why did my finish bar arrive late" report.
        logger.debug('[TerminalGate] withheld %s from a status=%s snapshot '
                     '(terminal fields are only valid on a terminal snapshot)',
                     dropped, status or '?')
    return out


__all__ = [
    'TERMINAL_STATUSES',
    'TERMINAL_ONLY_KEYS',
    'is_terminal_status',
    'filtered_snapshot_meta',
]
