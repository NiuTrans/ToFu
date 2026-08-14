"""Per-round message hygiene: compaction + attachments + search-addendum cleanup.

Extracted 2026-07-31 (pt_03f4cdf1 slice 18) from
``lib/tasks_pkg/orchestrator/_run.py`` run_task stream loop.

Three message-hygiene steps run at the top of every round, AFTER the
ROUND_START / tool-round phase events and BEFORE the swarm-inbox
drain + LLM call:

1. **Two-layer context compaction** (``run_compaction_pipeline``):
   L1 micro-compacts cold tool results every round at zero LLM cost;
   L2 substitutes a smart summary as a synthetic tool result on
   context overflow.
2. **Per-turn attachments** (``compute_turn_attachments`` +
   ``inject_attachments``): dynamic context injection inspired by
   Claude Code's getAttachments() — session memory, file reminders,
   tool discovery deltas. Skipped on round 0 (system contexts were
   just injected). Wrapped defensively: attachment building is
   advisory and must never crash an otherwise-healthy task — any bug
   degrades to "no attachments this round".
3. **Legacy search-addendum cleanup**
   (``inject_search_addendum_to_user``): strips old "Current date and
   time:" prefixes from user messages (date now lives in the system
   prompt) so old conversations keep a proper cache prefix.

The helper mutates ``messages`` in place and returns nothing — every
step is internally guarded, so it is safe to call unconditionally.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.compaction import run_compaction_pipeline
from lib.tasks_pkg.system_context import inject_search_addendum_to_user

logger = get_logger(__name__)


def run_round_message_hygiene(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    round_num: int,
    tid: str,
    project_path: str | None,
    project_enabled: bool,
    search_enabled: bool,
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
    project_path : str | None
        Project root path (attachments context).
    project_enabled : bool
        Whether project mode is on (attachments context).
    search_enabled : bool
        Whether search is on (search-addendum cleanup).
    """
    # ★ Context compaction: two-layer pipeline
    #   L1: micro-compact cold tool results (every round, zero LLM cost)
    #   L2: smart summary as synthetic tool result (on context overflow)
    run_compaction_pipeline(messages, round_num, task=task)

    # ★ Per-turn attachments: dynamic context injection
    #   Inspired by Claude Code's getAttachments() — injects session
    #   memory, file reminders, tool discovery deltas each turn.
    #   Wrapped defensively: attachment building is advisory and must
    #   never crash an otherwise-healthy task. Any bug here (e.g. a
    #   malformed tool_call arg from the model) degrades to "no
    #   attachments this round" rather than aborting the task.
    if round_num > 0:  # skip round 0 (system contexts just injected)
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

    # ★ Legacy cleanup: strip old "Current date and time:" from user
    #   messages.  Date is now injected in the system prompt (step 4.5)
    #   as date-only format.  This just ensures conversations with
    #   old-format timestamps get cleaned up for proper cache prefix.
    inject_search_addendum_to_user(messages, search_enabled,
                                   round_num=round_num)
