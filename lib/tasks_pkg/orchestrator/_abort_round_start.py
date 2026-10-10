"""Abort-at-round-start gate: break the stream loop on a pre-round abort.

Extracted 2026-07-31 ( slice 23) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Runs at the very TOP of every round, before ROUND_START is emitted.
When ``task['aborted']`` is set:

1. Stamps ``rs.abort_phase`` ('loop_start_round_<n>') and
   ``rs.exit_reason`` ('aborted_at_round_<n>').
2. Logs a DEBUG line with the abort-signal age (from
   ``task['_abort_timestamp']``, 'unknown' when unset) and the
   content length so far — the forensic trail for abort-latency
   postmortems.
3. Returns True so the caller breaks. NO ROUND_END is emitted here:
   the round never opened (no ROUND_START was emitted for it), so
   there is nothing to pair — the PREVIOUS round's end was already
   emitted at its own exit (RENDER_CONTRACT Phase 3).

Returns False when the task is not aborted and the round may open.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def handle_abort_at_round_start(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
) -> bool:
    """Break the stream loop when the task was aborted between rounds.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read: ``aborted``, ``_abort_timestamp``,
        ``content``).
    rs : RoundState
        Loop-state carrier (mutated: ``abort_phase``, ``exit_reason``).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.

    Returns
    -------
    bool
        ``True`` when the caller must ``break`` (abort handled),
        ``False`` to open the round.
    """
    if not task['aborted']:
        return False

    rs.abort_phase = f'loop_start_round_{round_num}'
    rs.exit_reason = f'aborted_at_round_{round_num}'
    _abort_ts = task.get('_abort_timestamp', 0)
    _now = time.time()
    _delay = f'{_now - _abort_ts:.1f}s ago' if _abort_ts else 'unknown'
    logger.debug('[%s] Task aborted at START of round %d model=%s '
                 '(abort signal arrived %s, content so far: %dchars)',
                 tid, round_num, rs.model, _delay, len(task.get('content') or ''))
    # RENDER_CONTRACT Phase 3: explicit round-end boundary even on
    #   the abort-at-start path (the round never opened, so no
    #   round_start was emitted for it — close nothing here; the
    #   PREVIOUS round's end was already emitted at its own exit).
    return True
