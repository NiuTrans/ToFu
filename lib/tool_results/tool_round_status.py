"""Tool-round status vocabulary: the one owner of ``toolRounds[].status`` classes.

Entry points: TERMINAL_TOOL_ROUND_STATUSES, FAILED_TOOL_ROUND_STATUSES,
AWAITING_ACTOR_TOOL_ROUND_STATUSES.
Dependencies: standard library only, so execution contracts and task execution
share the sets. A site whose meaning differs derives its set from these (adding
or removing named statuses) instead of restating the vocabulary.
"""

from __future__ import annotations

#: Verdicts that settle a round. A finalize stamps nothing else: any other
#: requested status settles as 'done'.
TERMINAL_TOOL_ROUND_STATUSES = frozenset(
    {'done', 'error', 'rejected', 'aborted', 'unanswerable'})

#: Terminal verdicts other than success.
FAILED_TOOL_ROUND_STATUSES = TERMINAL_TOOL_ROUND_STATUSES - {'done'}

#: Rounds waiting on another actor (a human answer, stdin, a client-side tool
#: result or a write approval) or holding that actor's submitted answer. That
#: actor's path resolves them; a task-end sweep never does.
AWAITING_ACTOR_TOOL_ROUND_STATUSES = frozenset({
    'awaiting_human', 'awaiting_stdin', 'awaiting_client_tool', 'pending_approval',
    'submitted',
})

__all__ = [
    'AWAITING_ACTOR_TOOL_ROUND_STATUSES',
    'FAILED_TOOL_ROUND_STATUSES',
    'TERMINAL_TOOL_ROUND_STATUSES',
]
