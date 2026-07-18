"""Prompt constants for the endpoint planner → worker → critic loop.

Three roles:
  1. **Planner** — runs at the start (and on CONTINUE_PLANNER re-plans).
     Rewrites the user's raw request into a clear, structured brief with
     an acceptance checklist.  Its output *replaces* the original user
     message so the Worker and Critic both operate on the refined version.
  2. **Worker** — full-power LLM with tools.  Executes the plan.
  3. **Critic** — full-power LLM with tools.  Reviews Worker output against
     the planner's checklist and either approves or routes back.

Split out of endpoint.py for readability.

Design goal of these prompts (2026-04-26 rewrite): favour *execution* over
*analysis*.  Previously the three roles would spiral into deeper and deeper
analysis with little actual file-editing work — see the "Analysis spiral"
pattern in task ``00d009c6`` (4 plans, 7 iterations, 0 deliverables).
The prompts below bias every role toward "shipped work" rather than
"thorough prose":

- Planner: short, concrete, 2-8 checklist items; on re-plan produce a
  *delta*, not a fresh sprawling rewrite.
- Worker: START every turn with a state-changing tool call.  Narrative
  is secondary to file edits.  Do not ask clarifying questions unless
  truly blocked.
- Critic: BEFORE verdicting, count state-changing tool calls in the
  worker's latest turn.  Zero state-changing calls on a non-empty
  checklist ⇒ the worker is analysis-paralysed and the correct verdict
  is always CONTINUE_WORKER with "execute, stop analyzing" feedback.
  CONTINUE_PLANNER is reserved for *structural* plan problems and
  requires a mandatory ``[PLAN_DEFECT: …]`` tag; without it the
  orchestrator downgrades to CONTINUE_WORKER.

──────────────────────────────────────────────────────────────────────
This module is a **facade package**: the public constants below are
defined in role-specific sub-modules and re-exported here so that every
``from lib.tasks_pkg.endpoint_prompts import X`` continues to work
byte-identically.  The import path is UNCHANGED.

  - ``PLANNER_SYSTEM_PROMPT``   → ._planner
  - ``WORKER_DIRECTIVE_HEADER`` → ._worker
  - ``CRITIC_SYSTEM_PROMPT``    → ._critic
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts._planner import PLANNER_SYSTEM_PROMPT  # noqa: F401
from lib.tasks_pkg.endpoint_prompts._worker import WORKER_DIRECTIVE_HEADER  # noqa: F401
from lib.tasks_pkg.endpoint_prompts._critic import CRITIC_SYSTEM_PROMPT  # noqa: F401

__all__ = [
    'PLANNER_SYSTEM_PROMPT',
    'WORKER_DIRECTIVE_HEADER',
    'CRITIC_SYSTEM_PROMPT',
]
