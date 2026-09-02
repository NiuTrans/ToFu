"""Per-round crash-recovery checkpoint + RENDER_CONTRACT round close.

Extracted 2026-07-31 ( slice 20) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Two steps run at the natural end of a tools-executed iteration,
AFTER the tool execution pipeline and the consecutive-timeout
circuit breaker:

1. **Crash-recovery checkpoint** (``checkpoint_task_partial``):
   persists current content/thinking to task_results + conversation
   so data survives a server crash. Throttled to at most once every
   5 seconds to avoid DB pressure. Failure is non-fatal (logged at
   WARNING with exc_info) — a checkpoint bug must never break an
   otherwise-healthy round.
2. **RENDER_CONTRACT Phase 3 round close**: emits
   ROUND_END(reason='tools') pairing the ROUND_START emitted at this
   round's top. Reached only at the natural end of a tools-executed
   iteration — an early ``continue`` for a premature-close retry does
   NOT reach here, so it never emits a spurious end for a round being
   re-run; every early ``break`` path emits its own reason-tagged
   ROUND_END upstream (budget / aborted / tool_timeout).

Both steps are control-flow-free — the helper mutates
``rs.last_checkpoint_ts`` in place and returns nothing.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event, checkpoint_task_partial

logger = get_logger(__name__)


def run_round_checkpoint_and_close(
    task: dict[str, Any],
    rs: Any,
    *,
    round_num: int,
    tid: str,
) -> None:
    """Persist the throttled partial checkpoint + emit ROUND_END.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read by the checkpoint).
    rs : RoundState
        Loop-state carrier (mutated: ``last_checkpoint_ts``).
    round_num : int
        Current round index (0-based).
    tid : str
        8-char task id for logging.
    """
    # ══════════════════════════════════════════
    #  Crash-recovery checkpoint: persist partial state to DB
    # ══════════════════════════════════════════
    # After each tool execution round, save current content/thinking
    # to task_results + conversation so data survives a server crash.
    # Throttled to at most once every 5 seconds to avoid DB pressure.
    _now = time.time()
    if _now - rs.last_checkpoint_ts >= 5:
        try:
            checkpoint_task_partial(task, force=True)
            rs.last_checkpoint_ts = _now
        except Exception as e:
            logger.warning('[%s] Checkpoint after round %d failed (non-fatal): %s',
                           tid, round_num + 1, e, exc_info=True)

    # RENDER_CONTRACT Phase 3: explicit round-end boundary for a round
    #   that issued tool calls and is about to loop into the next round.
    #   Reached only at the natural end of a tools-executed iteration
    #   (an early `continue` for a premature-close retry does NOT reach
    #   here, so it never emits a spurious end for a round being re-run).
    append_event(task, build_event(EventType.ROUND_END,
                                   roundNum=round_num, reason='tools'))
