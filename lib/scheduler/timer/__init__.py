"""lib/scheduler/timer/ — Timer Watcher: async poll → decide → continue.

The Timer Watcher is a simplified, conversation-inline variant of the
proactive agent.  An agent tool call creates a timer; a background thread
polls independently until conditions are met, then injects a follow-up
user message and kicks off a new agentic task.

Key design decisions:
  • Each poll is *independent* — no cross-poll history (token-saving).
  • The poll optionally runs a shell command first and feeds its output
    to the LLM for grounded decision-making.
  • Single-shot by default (auto-cancels after triggering).
  • Timer threads are daemon threads so they don't block server shutdown.

This is a pure re-export facade — all implementation lives in the sibling
sub-modules.  The import path ``lib.scheduler.timer`` is UNCHANGED and every
symbol resolves byte-identically to the pre-split module:

  ._state — process-wide shared registry state (_active_timers/_timers_lock/
            _last_cmd_outputs/_cmd_outputs_lock). ⚠️ ONE per process, shared
            BY REFERENCE. executor.py imports _cmd_outputs_lock +
            _last_cmd_outputs DIRECTLY from here (via this facade).
  ._crud  — create/cancel/force-trigger/get/list/poll-log + resume guardrails.
  ._poll  — poll agent loop, check-command runner, DB poll-status writers.
  ._loop  — continuation dispatch, background poll loop, resume-on-restart.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared process-wide state (MUST be the same objects everywhere) ─────────
# executor.py imports _cmd_outputs_lock + _last_cmd_outputs DIRECTLY via
# ``from lib.scheduler.timer import ...`` — these MUST stay importable here and
# be the SAME objects the poll loop reads/writes.
from ._state import (  # noqa: E402,F401
    _active_timers,
    _cmd_outputs_lock,
    _last_cmd_outputs,
    _timers_lock,
)

# ── CRUD + resume guardrails ────────────────────────────────────────────────
from ._crud import (  # noqa: E402,F401
    _get_timer_row,
    _resume_concurrency_cap,
    _resume_max_age_seconds,
    cancel_timer,
    create_timer,
    force_trigger_timer,
    get_timer,
    get_timer_poll_log,
    has_timer_history,
    list_active_timers,
)

# ── Poll logic + DB status writers ──────────────────────────────────────────
from ._poll import (  # noqa: E402,F401
    _POLL_SYSTEM_PROMPT,
    _apply_reconcile_poll,
    _build_poll_tools,
    _count_trailing_ambiguous_code_polls,
    _execute_poll_tool,
    _increment_poll_count,
    _mark_exhausted,
    _mark_expired,
    _mark_orphaned,
    _reconcile_audit,
    _reconcile_audit_lock,
    _record_poll,
    _run_check_command,
    poll_timer,
)

# ── Continuation dispatch + background loop + resume ────────────────────────
from ._loop import (  # noqa: E402,F401
    _execute_continuation,
    get_active_timer_count,
    resume_active_timers,
    start_timer_loop,
)


# ── Public API — preserved VERBATIM from the pre-split module ───────────────
__all__ = [
    'create_timer', 'cancel_timer', 'force_trigger_timer',
    'get_timer', 'list_active_timers', 'has_timer_history',
    'get_timer_poll_log',
    'poll_timer', 'start_timer_loop', 'resume_active_timers',
    'get_active_timer_count',
    # Used by scheduler/executor.py for inline blocking poll:
    '_record_poll', '_increment_poll_count', '_mark_exhausted',
]
