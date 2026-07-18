"""Endpoint-mode plan-building helpers + loop constants.

Extracted from the monolithic ``lib/tasks_pkg/endpoint.py``.  Houses the
pure message-shaping helpers that both the initial-plan path and the
CONTINUE_PLANNER replan path share, plus the loop-wide safety-valve
constants (``MAX_ITERATIONS`` / ``MAX_REPLANS`` / ``MAX_ZERO_DELIVERABLE_TURNS``)
and the zero-deliverable directive string.

Dependency direction: leaf module — imports only ``endpoint_prompts`` and
``endpoint_review`` (no other endpoint sub-module), so ``_run`` can import it
freely without a cycle.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import WORKER_DIRECTIVE_HEADER
from lib.tasks_pkg.endpoint_review import _count_state_changing_rounds

MAX_ITERATIONS = 10   # hard cap — safety valve to prevent runaway loops
MAX_REPLANS = 3       # hard cap on CONTINUE_PLANNER branches per task

# Zero-deliverable guard — if the worker produces zero state-changing tool
# calls for this many consecutive turns, the orchestrator skips the Critic
# and injects a hard-coded "execute, don't analyze" directive instead,
# advancing the iteration counter.  This pre-empts the analysis-paralysis
# mode where the worker + critic agree that "more investigation is needed"
# and burn token budget without shipping anything.
MAX_ZERO_DELIVERABLE_TURNS = 2


def _replan_enabled() -> bool:
    """Kill-switch: when '0', CONTINUE_PLANNER is downgraded to CONTINUE_WORKER.

    Reads ``TOFU_ENDPOINT_REPLAN`` (documented in CLAUDE.md §9).
    """
    from lib.env_compat import getenv_compat
    return getenv_compat('TOFU_ENDPOINT_REPLAN',
                         default='1').strip() != '0'


def _build_worker_directive(plan_content: str) -> str:
    """Wrap a plan body in the standard worker imperative directive.

    Extracted so both the initial planner path AND the replan path produce
    the exact same ``user`` message shape — identical byte-for-byte apart
    from the plan body.  This keeps the prefix-cache discipline in place.

    The directive header (``WORKER_DIRECTIVE_HEADER`` from
    ``endpoint_prompts``) hard-codes the execution rules: start with a
    state-changing tool call, no clarifying questions unless blocked,
    narrative is secondary, etc.  See endpoint_prompts.py for the
    rationale.
    """
    return WORKER_DIRECTIVE_HEADER + plan_content


def _reset_worker_messages_with_plan(
    original_messages: list,
    plan_content: str,
    *,
    progress_summary: str = '',
) -> list:
    """Rebuild the worker's working messages: keep system prompts verbatim
    (prefix-cache friendly), replace the last ``user`` with the wrapped plan.

    Used both at initial-plan time and after each CONTINUE_PLANNER replan.
    On replan, the caller passes ``original_messages`` (the task's original
    message list), NOT the accumulated worker/critic turns — the new plan
    starts a clean worker context, while the DB retains the full history
    for display purposes.

    Parameters
    ----------
    progress_summary : str, optional
        When supplied (re-plan path), a compacted summary of what the
        worker already accomplished under the previous plan is appended
        as a user turn after the plan directive.  This preserves the
        worker's partial progress across re-plans so it doesn't
        re-explore the codebase from scratch — which was one of the
        biggest causes of the analysis-spiral pattern (see task
        ``00d009c6``).  The summary is bounded in size (see
        ``_build_progress_summary``).
    """
    worker_directive = _build_worker_directive(plan_content)
    working_messages = []
    user_replaced = False
    for msg in reversed(original_messages):
        if msg.get('role') == 'user' and not user_replaced:
            working_messages.insert(0, {
                'role': 'user',
                'content': worker_directive,
            })
            user_replaced = True
        else:
            working_messages.insert(0, dict(msg))
    if not user_replaced:
        # Edge case: no user message found — append as user
        working_messages.append({
            'role': 'user',
            'content': worker_directive,
        })
    # Append the progress summary AFTER the plan, as an assistant turn
    # (it's the worker's own "memory" of prior work), so the model treats
    # it as established context rather than a new directive.
    if progress_summary:
        working_messages.append({
            'role': 'assistant',
            'content': progress_summary,
        })
        # And a nudge user turn so the model's next reply is grounded as
        # "continue from here" rather than "respond to the assistant".
        working_messages.append({
            'role': 'user',
            'content': (
                'Continue from the state summarised above.  Apply the '
                'revised plan to any remaining or re-opened checklist '
                'items.  Your first tool call MUST be a state-changing '
                'one — do not re-explore the codebase.'
            ),
        })
    return working_messages


def _build_progress_summary(endpoint_turns: list) -> str:
    """Compact summary of prior worker deliverables to carry across re-plans.

    The orchestrator keeps the full ``endpoint_turns`` for DB / UI display,
    but the LLM working-messages are reset on re-plan.  Without carryover,
    the worker loses track of everything it already did and tends to
    re-read the same files, re-analyze, and produce a *new* zero-deliverable
    turn.  This helper scans the worker turns for their state-changing
    tool calls and produces a ≤ ~1500-char summary that fits cleanly into
    the new worker context.

    Empty / omitted when there are no worker turns or no deliverables yet.
    """
    if not endpoint_turns:
        return ''

    # Collect worker turns (role=assistant + _epIteration).
    lines = []
    lines.append('=== Progress summary from prior worker iterations ===')
    for msg in endpoint_turns:
        if msg.get('role') != 'assistant':
            continue
        if msg.get('_isEndpointPlanner'):
            continue
        it = msg.get('_epIteration')
        if not it:
            continue
        tool_rounds = msg.get('toolRounds') or []
        sc_count, exp_count, sc_names = _count_state_changing_rounds(tool_rounds)
        # Summarise by tool name + count
        counts: dict[str, int] = {}
        for n in sc_names:
            counts[n] = counts.get(n, 0) + 1
        name_parts = [f'{n}×{c}' if c > 1 else n for n, c in counts.items()]
        names_str = ', '.join(name_parts) if name_parts else '(no state-changing calls)'
        # Snippet of what the worker wrote at the end (narrative)
        content = (msg.get('content') or '').strip()
        if len(content) > 400:
            content = content[:380] + '…'
        lines.append(
            f'\n— Iteration {it}: state-changing={sc_count} '
            f'[{names_str}], exploratory={exp_count}\n'
            f'  Worker notes: {content}'
        )

    body = '\n'.join(lines)
    if len(body) > 4000:
        body = body[:3800] + '\n\n…(older iterations truncated for brevity)'
    body += (
        '\n\n=== End progress summary ===\n\n'
        'Treat the above as established context.  Do NOT redo work that '
        'already succeeded; focus on the items the revised plan calls '
        'out as still needing attention.'
    )
    return body


def _build_replan_input_messages(
    original_messages: list,
    critic_feedback: str,
    *,
    prior_plan: str = '',
    plan_defect: str = '',
    replan_count: int = 1,
) -> list:
    """Build the input message list passed to the Planner for a replan.

    Starts from the ORIGINAL conversation (system + user request) so the
    new plan is grounded in the user's actual ask — not biased by the
    failed worker iterations.  The critic's feedback, the *prior plan*,
    and the PLAN_DEFECT diagnosis are appended as an imperative user turn
    that tells the planner exactly what to revise.  Prefix-cache friendly:
    the original ``[system, ...user]`` prefix is bitwise identical across
    the first planner call and every subsequent replan.

    Parameters
    ----------
    prior_plan : str
        The plan that failed.  The planner is explicitly instructed to
        produce a *delta* (not a sprawling rewrite), keeping what worked
        and amending only what the PLAN_DEFECT diagnosis identifies as
        broken.  Without this, re-plans tend to grow unboundedly (10k
        → 13k → 15k chars over 3 revisions — see task ``00d009c6``).
    plan_defect : str
        The structured PLAN_DEFECT reason extracted from the critic's
        verdict.  Empty when the orchestrator triggered the re-plan
        through some other channel (shouldn't happen post-rewrite, but
        defensive).
    replan_count : int
        1-based re-plan counter for surfacing in the directive.
    """
    planner_input = [dict(m) for m in original_messages]

    defect_line = (
        f'- PLAN_DEFECT identified by the critic: {plan_defect}\n'
        if plan_defect
        else ''
    )
    prior_plan_block = (
        '\n───── Previous plan (for reference — produce a DELTA) ─────\n'
        f'{prior_plan}\n'
        '───── End previous plan ─────\n'
        if prior_plan
        else ''
    )

    revision_directive = (
        f'=== Previous plan needs revision (replan #{replan_count}) ===\n\n'
        f'{defect_line}'
        f'Critic feedback:\n{critic_feedback}\n\n'
        f'{prior_plan_block}\n'
        '=== End revision feedback ===\n\n'
        'Produce a NEW structured execution brief.  HARD RULES:\n'
        '1. This is a DELTA, not a rewrite.  Keep whatever was correct '
        'about the prior plan.  Amend only what the PLAN_DEFECT / critic '
        'feedback says is broken.\n'
        '2. The new plan MUST NOT be longer than the prior plan in '
        'characters or in number of checklist items.  Condense where '
        'you can; delete items that turned out to be unnecessary.\n'
        '3. In your ``## Context`` section, state up-front in one '
        'sentence: "Revising because <PLAN_DEFECT summary>."\n'
        '4. If the PLAN_DEFECT suggests the task is genuinely out of '
        'scope under any plan, emit a minimal 1-2-item plan that asks '
        'the user to clarify / narrow scope, rather than trying to '
        'force a workaround.\n'
        '5. Same output format as your original planner role.'
    )
    planner_input.append({'role': 'user', 'content': revision_directive})
    return planner_input


# ──────────────────────────────────────
#  Zero-deliverable injection (orchestrator-side guard)
# ──────────────────────────────────────

_ZERO_DELIVERABLE_DIRECTIVE = (
    '[Orchestrator directive — execute, do not analyze]\n\n'
    'Your previous turn produced ZERO state-changing tool calls '
    '(no write_file / apply_diff / insert_content / run_command / '
    'create_project).  That is analysis paralysis; it does not advance '
    'the plan.\n\n'
    'Your next tool call MUST be a state-changing one on a checklist '
    'item that is still ❌.  Do not read more files.  Do not produce '
    'more narrative.  Pick the next actionable checklist step and '
    'execute it with a tool.\n\n'
    'If you genuinely cannot make progress because the plan is '
    'impossible (not because you have unanswered questions), stop '
    'calling tools and state precisely why in ONE paragraph — the '
    'critic will route this to a re-plan if that diagnosis holds up.'
)
