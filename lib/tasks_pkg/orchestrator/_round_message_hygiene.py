"""Per-round message hygiene: compaction plus dynamic attachments.

Extracted 2026-07-31 ( slice 18) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Two message-hygiene steps run at the top of every round, AFTER the
ROUND_START / tool-round phase events and BEFORE the swarm-inbox
drain + LLM call:

1. **Two-layer context compaction** (``run_compaction_pipeline``):
   L1 micro-compacts cold tool results every round at zero LLM cost;
   L2 substitutes a smart summary as a synthetic tool result on
   context overflow.
2. **Per-turn attachments** (``compute_turn_attachments`` +
   ``inject_attachments``): dynamic per-turn context injection —
   session memory, tool discovery deltas. Skipped on
   round 0 (system contexts were just injected). Wrapped defensively:
   attachment building is advisory and must never crash an
   otherwise-healthy task — any bug degrades to "no attachments this
   round".
The helper mutates ``messages`` in place and returns nothing — every
step is internally guarded, so it is safe to call unconditionally.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.compaction.api import run_compaction_pipeline
from lib.tasks_pkg.round_prep_timing import round_prep_step

logger = get_logger(__name__)


def run_round_message_hygiene(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    round_num: int,
    tid: str,
    model: str = '',
    project_path: str | None,
    project_enabled: bool,
    remaining_api_rounds: int | None = None,
) -> None:
    """Run the per-round message-hygiene cluster.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict (read by compaction + attachments).
    messages : list[dict[str, Any]]
        Working message list — mutated in place.
    round_num : int
        Current round index (0-based). Attachments are skipped on
        round 0 (system contexts were just injected).
    tid : str
        8-char task id for logging.
    model : str
        Resolved logical model retained for caller compatibility. Compaction
        uses the shared task admission policy, independent of provider dialect.
    project_path : str | None
        Project root path (attachments context).
    project_enabled : bool
        Whether project mode is on (attachments context).
    remaining_api_rounds : int | None
        Hard-budget calls still available, including the current call.
    """
    # One local continuity/admission policy for every route. Remove latches
    # from restored legacy tasks; provider capability is not policy selection.
    task.pop('_nativeCompactionMode', None)
    task.pop('_nativeCompactionPrimary', None)

    # Context compaction: two-layer pipeline
    #   L1: micro-compact cold tool results (every round, zero LLM cost)
    #   L2: source-derived continuity plus model narrative at a closed boundary
    with round_prep_step(task, 'compaction'):
        run_compaction_pipeline(
            messages,
            round_num,
            task=task,
            remaining_api_rounds=remaining_api_rounds,
        )

    # Per-turn attachments: dynamic context injection
    #   Injects session memory, file reminders, tool discovery deltas
    #   each turn.
    #   Wrapped defensively: attachment building is advisory and must
    #   never crash an otherwise-healthy task. Any bug here (e.g. a
    #   malformed tool_call arg from the model) degrades to "no
    #   attachments this round" rather than aborting the task.
    # Startup already injected system context even when continuation starts
    # at a cumulative round rather than zero. Skip only this execution's first
    # dispatch, not the logical Turn's entire resumed suffix.
    if round_num > task.get('_firstDispatchRound', 0):
        try:
            _attachments = compute_turn_attachments(
                messages, task, round_num,
                conv_id=task.get('convId', ''),
                project_path=project_path,
                project_enabled=project_enabled,
            )
            if _attachments:
                inject_attachments(messages, _attachments,
                                    conv_id=task.get('convId') or None,
                                    task=task, round_num=round_num,
                                    model=((task.get('config') or {}).get('model')
                                           or ''))
        except Exception as e:
            logger.error('[Task:%s] compute_turn_attachments failed '
                         'round=%d: %s — continuing without attachments',
                         tid, round_num, e, exc_info=True)
