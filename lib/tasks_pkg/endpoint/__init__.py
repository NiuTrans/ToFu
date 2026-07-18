"""Endpoint mode — Planner → Worker → Critic autonomous loop.

Three-phase architecture:

  Phase 0 (Planner): Rewrites the user's raw request into a structured
  brief with a checklist and acceptance criteria.  Runs once at the
  start; MAY be re-run mid-task when the Critic emits
  [VERDICT: CONTINUE_PLANNER] to request a full re-plan (e.g. the
  original plan is wrong / out-of-scope).

  Phase 1 (Worker): Full LLM + tools.  Executes the plan.
  Phase 2 (Critic): Full LLM + tools.  Reviews against the checklist.
  Emits one of three verdicts:
    - STOP              → loop terminates.
    - CONTINUE_WORKER   → inject feedback as user msg, loop back to Phase 1.
    - CONTINUE_PLANNER  → feed critic feedback to a fresh Planner turn
                          which produces a NEW brief; worker messages
                          are reset to `[system, user(new brief)]`.

Conversation shape visible to Worker & Critic (LLM working messages):
  system → user(planner brief)  [first worker turn]
  system → user(planner brief) → assistant(worker) → user(critic feedback) → ...  [later turns]
  After a replan: system → user(NEW planner brief)  [worker re-starts clean]

  The planner's output REPLACES the original user message so the worker
  sees a clean, structured plan as its user request.  This avoids the old
  phantom pattern where assistant(planner) + user("Execute…") were appended.

Conversation shape in the DB / frontend (display):
  user(original)
  → assistant(planner, _isEndpointPlanner, _epPlannerIteration=1)
  → assistant(worker, _epIteration=1)
  → user(critic, _isEndpointReview, _epNextPhase='worker'|'planner')
  → assistant(worker, _epIteration=2)
  → (replan →) assistant(planner, _isEndpointPlanner, _epPlannerIteration=2)
  → assistant(worker, _epIteration=3)  ... etc.

Termination guardrails:
  1. Critic verdict — STOP means approved.
  2. Stuck detection — similar worker feedback in 2+ consecutive rounds;
     history resets on replan so two distinct plans don't falsely trigger.
  3. Max iterations — hard cap at MAX_ITERATIONS (default 10).
  4. Max replans — hard cap at MAX_REPLANS (default 3) to prevent
     planner ping-pong.
  5. Kill switch — ``TOFU_ENDPOINT_REPLAN=0`` downgrades
     CONTINUE_PLANNER to CONTINUE_WORKER at the parser layer.
  6. Abort — user can abort at any time.

────────────────────────────────────────────────────────────────────────
This module is a FACADE PACKAGE.  The implementation was split out of the
former single-file ``lib/tasks_pkg/endpoint.py`` into cohesive sub-modules,
but the import path is UNCHANGED — every ``from lib.tasks_pkg.endpoint
import X`` call site keeps working byte-identically.

Sub-modules:
  * ``._replan``    — plan-building helpers + loop constants.
  * ``._translate`` — endpoint turn DB persistence + auto-translate.
  * ``._run``       — the core Planner→Worker→Critic loop (+ _finalize).
  * ``._sync``      — the synchronous run_task_sync wrapper + progress.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Legacy prompt re-exports (were imported at the top + line 329 of the old
#  endpoint.py).  Other code imports these prompt names via
#  ``lib.tasks_pkg.endpoint`` — preserve the facade path.
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.endpoint_prompts import (  # noqa: E402,F401
    WORKER_DIRECTIVE_HEADER,
    CRITIC_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Plan-building helpers + loop constants  (from ._replan)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.endpoint._replan import (  # noqa: E402,F401
    MAX_ITERATIONS,
    MAX_REPLANS,
    MAX_ZERO_DELIVERABLE_TURNS,
    _ZERO_DELIVERABLE_DIRECTIVE,
    _replan_enabled,
    _build_worker_directive,
    _reset_worker_messages_with_plan,
    _build_progress_summary,
    _build_replan_input_messages,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoint turn persistence + auto-translate  (from ._translate)
#  ★ Also imported directly by lib/orchestration_endpoint_runner.py.
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.endpoint._translate import (  # noqa: E402,F401
    _sync_endpoint_turns_to_conversation,
    _store_endpoint_turns_on_task,
    _trigger_per_turn_auto_translate,
    _trigger_endpoint_auto_translate,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Core loop  (from ._run)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.endpoint._run import (  # noqa: E402,F401
    run_endpoint_task,
    _EarlyExit,
    _finalize,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Synchronous wrapper + progress  (from ._sync)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.endpoint._sync import (  # noqa: E402,F401
    run_task_sync,
    _format_progress_event,
    _drain_progress,
)


__all__ = [
    'run_endpoint_task',
    'run_task_sync',
]
