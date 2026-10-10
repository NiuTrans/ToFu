"""Abort-during-prep gate: honor a user Stop that lands while run_task is
still in its startup / prep phase (before round 0 opens).

Incident anchor (2026-08-05, conv msftgnt3ezhmtt, task 456bf5c7): the abort
was received at elapsed=5.0s, yet the orchestrator only consulted
``task['aborted']`` at three points INSIDE the round loop (round start /
post-stream / pre-tools). Everything BEFORE round 0 — turn prelude, provider
binding, config resolution, project-context setup, tool assembly (incl. the
MCP load), MsgStore tool-history rebuild, context injection, memory-prefetch
join — was abort-blind. On FUSE-slow storage that prep measured 88s, so the
task kept "running" for 85s after the user's Stop: the server-authoritative
busy projection kept the turn alive-looking, and every further Stop click
was a no-op duplicate ("abort DUPLICATE — already aborted") — the
"I have to click pause multiple times" report.

This module mirrors ``_abort_round_start.handle_abort_at_round_start`` for
the prep phase: one cheap sticky-flag check at each expensive stage
boundary collapses the kill latency from "all of prep" to "the current
stage". On a trip the caller skips the round loop and falls through to
``finalize_after_loop`` — the SAME finalize the round-0 abort gate already
reaches (finishReason=aborted, autopilot skipped, DONE event + persist), so
every downstream consumer (persist / SSE terminal frames / busy-projection
clear) behaves identically to a between-rounds abort.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def handle_abort_during_prep(
    task: dict[str, Any],
    rs: Any,
    *,
    stage: str,
    tid: str,
) -> bool:
    """True when the task was aborted during startup/prep — caller skips the loop.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read: ``aborted``, ``_abort_timestamp``,
        ``content``).
    rs : RoundState
        Loop-state carrier (mutated on a trip: ``abort_phase`` ←
        ``prep_<stage>``, ``exit_reason`` ← ``aborted_during_prep_<stage>``).
    stage : str
        Prep-stage label for the exit-reason stamp and log line
        (e.g. ``'startup'`` / ``'project_setup'`` / ``'tool_setup'`` /
        ``'context_inject'`` / ``'prefinal'``). The FIRST tripped stage wins, so the stamp tells
        the postmortem exactly which stretch of prep absorbed the abort.
    tid : str
        8-char task id for logging.

    Returns
    -------
    bool
        ``True`` when the caller must skip the round loop and fall through
        to finalize. NO events are emitted here: no round opened, so there
        is no round-boundary event to pair (the same contract the
        round-start gate documents).
    """
    if not task.get('aborted'):
        return False

    rs.abort_phase = f'prep_{stage}'
    rs.exit_reason = f'aborted_during_prep_{stage}'
    _abort_ts = task.get('_abort_timestamp', 0)
    _now = time.time()
    _delay = f'{_now - _abort_ts:.1f}s ago' if _abort_ts else 'unknown'
    # INFO (not debug): a prep-phase abort means the user watched a "dead"
    # task keep preparing — exactly the window this gate exists to close,
    # and the log line is the proof the kill landed before any LLM call.
    logger.info('[%s] Task aborted during PREP stage=%s — skipping the round '
                'loop (abort signal arrived %s, content so far: %dchars)',
                tid, stage, _delay, len(task.get('content') or ''))
    return True
