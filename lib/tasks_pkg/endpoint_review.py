"""Planner, critic turn, and helper functions for the endpoint loop.

Three roles:
  1. Planner  — runs at start (+ on CONTINUE_PLANNER), rewrites user goal
                into structured brief.
  2. Worker   — full LLM + tools, executes the plan (handled by orchestrator).
  3. Critic   — full LLM + tools, verifies against the planner's checklist.

Split out of endpoint.py for readability.  All symbols are re-imported
by the main ``endpoint`` module so external callers are unaffected.

Anti-analysis-spiral (2026-04-26 rewrite):

- ``_parse_verdict`` now requires an explicit ``[PLAN_DEFECT: ...]`` tag
  for CONTINUE_PLANNER.  Without it, the verdict is downgraded to
  CONTINUE_WORKER and an audit log entry is written.  This blocks the
  "rationalize a replan out of nothing" failure mode.
- STOP-with-unresolved-markers now downgrades to **CONTINUE_WORKER**
  (not CONTINUE_PLANNER, as in the previous implementation).  Residual
  ❌ is almost always a worker-execution problem, not a structural plan
  problem.
- ``_count_state_changing_rounds`` inspects ``task['toolRounds']`` and
  returns the number of *deliverable* tool calls (write_file / edit_file /
  run_command / create_project / image gen).  The
  critic invocation prompt carries a "Deliverables Snapshot" so the
  LLM-Critic can make the right call, and endpoint.py uses the same
  counter for its orchestrator-side zero-deliverable guard.
- ``_run_planner_turn`` accepts an optional ``planner_tag`` kwarg so re-
  plan calls surface cleanly in logs and audit records.
"""

from lib.agent_verdict import (
    STATE_CHANGING_TOOLS,  # noqa: F401  — re-exported for endpoint.py / external callers
    accumulate_usage as _accumulate_usage,  # noqa: F401  — re-exported (endpoint.py imports it)
    classify_verdict,
    count_state_changing_rounds as _count_state_changing_rounds,
    detect_stuck as _detect_stuck,  # noqa: F401  — re-exported (endpoint.py imports it)
    replan_enabled as _replan_enabled,  # noqa: F401  — re-exported for external callers
)
from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import CRITIC_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
from lib.tasks_pkg.orchestrator import _run_single_turn


# ``_replan_enabled``, ``STATE_CHANGING_TOOLS`` and
# ``_count_state_changing_rounds`` now live in ``lib.agent_verdict`` (the
# single source of truth) and are imported above under their historical
# names so external callers of this module are unaffected.


def _format_deliverables_snapshot(
    latest_sc: int,
    latest_explore: int,
    latest_names: list[str],
    cumulative_sc: int,
    iteration: int,
) -> str:
    """Render a short "Deliverables Snapshot" block for the critic prompt.

    Returns a human-readable markdown block summarising the worker's
    activity in the latest turn AND across the whole task, so the critic
    can use it as the pre-check trigger described in its system prompt.
    """
    # Compact name summary — "apply_diff×2, write_file"
    counts: dict[str, int] = {}
    for n in latest_names:
        counts[n] = counts.get(n, 0) + 1
    parts = []
    for n, c in counts.items():
        parts.append(f'{n}×{c}' if c > 1 else n)
    names_str = ', '.join(parts) if parts else '(none)'

    if latest_sc == 0 and iteration >= 1:
        verdict_hint = (
            'WORKER ANALYSIS-PARALYSED THIS TURN.  The correct verdict is '
            'almost always CONTINUE_WORKER with feedback "execute the plan, '
            'stop analyzing".  Do NOT emit CONTINUE_PLANNER — this is a '
            'worker problem, not a plan problem.'
        )
    elif latest_sc > 0:
        verdict_hint = (
            'Worker produced real deliverables this turn.  Verify the '
            'edits closed the checklist items before verdicting.'
        )
    else:
        verdict_hint = ''

    return (
        '───── Deliverables Snapshot (orchestrator-injected) ─────\n'
        f'- Latest worker turn (#{iteration}): '
        f'{latest_sc} state-changing tool calls, '
        f'{latest_explore} exploratory calls.\n'
        f'- State-changing calls this turn: {names_str}\n'
        f'- Task total state-changing calls: {cumulative_sc}.\n'
        f'{("- GUIDANCE: " + verdict_hint) if verdict_hint else ""}\n'
        '─────────────────────────────────────────────────────────'
    )


# ══════════════════════════════════════════════════════════
#  Planner turn
# ══════════════════════════════════════════════════════════

def _run_planner_turn(task, messages, *, planner_tag: str = 'initial'):
    """Execute the planner: rewrite the user's request into a structured brief.

    **Prefix-cache-friendly construction**.  The original ``system`` message
    and all prior conversation turns are passed to the planner EXACTLY
    AS-IS so the LLM provider's KV / prefix cache stays hot across planner,
    worker, and critic calls within the same task.  The only delta between
    the original conversation and what the planner sees is the **content of
    the last user message**, which is wrapped with the planner role
    description + "produce a plan" directive.  No extra ``system`` message
    is prepended and no fake ``assistant`` turn is injected.

    Parameters
    ----------
    planner_tag : str
        Diagnostic label — ``'initial'`` for the first plan, ``'replan-N'``
        for subsequent re-plans.  Surfaced in log messages only.

    Returns
    -------
    dict with keys:
        content : str   — the planner's structured brief
        thinking : str  — planner's thinking (if extended thinking enabled)
        usage : dict    — token usage
        messages : list — the message list after the planner turn
        error : str|None — error message if the turn failed
    """
    tid = task['id'][:8]

    # Copy the conversation verbatim. The role is a round-scoped Composer
    # block, so the user's actual request stays byte-identical and keeps its
    # real authority instead of being rewritten into synthetic user text.
    planner_messages = [dict(m) for m in messages]
    planner_role = (
        '=== Your role for THIS turn: Planner ===\n'
        f'{PLANNER_SYSTEM_PROMPT}\n'
        '=== End planner role ===\n\n'
        'Based on the system prompt and conversation history above, '
        'and the latest user request, produce your structured execution '
        'brief for the worker per the format in your planner role.  '
        'You MAY use read-only tools (list_dir, read_files, grep_search) '
        'to explore the codebase, but DO NOT edit files or execute the '
        'task itself — planning only.'
    )

    logger.info(
        '[Planner] Starting planner turn for task %s (%s), %d messages '
        '(prefix-cache friendly)',
        tid, planner_tag, len(planner_messages),
    )

    previous_role = task.get('_contextRoleBlock')
    task['_contextRoleBlock'] = {'name': 'planner', 'content': planner_role}
    try:
        result = _run_single_turn(task, messages_override=planner_messages)
    finally:
        if previous_role is None:
            task.pop('_contextRoleBlock', None)
        else:
            task['_contextRoleBlock'] = previous_role

    content = result.get('content', '')
    error = result.get('error')

    if error:
        logger.warning('[Planner] Planner turn error for %s (%s): %s',
                       tid, planner_tag, error)

    logger.info('[Planner] Task %s (%s) — plan=%d chars',
                tid, planner_tag, len(content))

    return {
        'content': content,
        'thinking': result.get('thinking', ''),
        'usage': result.get('usage', {}),
        'messages': result.get('messages', planner_messages),
        'error': error,
    }


# ══════════════════════════════════════════════════════════
#  Verdict parsing
# ══════════════════════════════════════════════════════════

def _parse_verdict(text: str) -> tuple:
    """Parse the critic's output into ``(feedback_text, next_phase,
    plan_defect_reason)``.

    Thin adapter over :func:`lib.agent_verdict.classify_verdict` (the single
    source of truth).  Endpoint mode uses the STRICT policy: a missing
    ``[VERDICT:]`` tag defaults to CONTINUE_WORKER (no loose plain-language
    fallback), and the cleaned display feedback is returned.

    Returns
    -------
    (str, str, str | None)
        ``(feedback_text, next_phase, plan_defect_reason)`` where
        ``next_phase`` ∈ {``'stop'``, ``'worker'``, ``'planner'``}.

    Gating (STOP-with-unresolved-markers downgrade, CONTINUE_PLANNER
    requires a non-worker-rationalization PLAN_DEFECT tag, and the
    TOFU_ENDPOINT_REPLAN=0 kill-switch) all live in ``classify_verdict``.
    """
    res = classify_verdict(text, loose_fallback=False, strip_feedback=True)
    return res['feedback'], res['phase'], res['plan_defect']


# ══════════════════════════════════════════════════════════
#  Run critic turn
# ══════════════════════════════════════════════════════════

def _run_critic_turn(
    task,
    original_messages,
    worker_messages,
    *,
    iteration: int = 0,
    latest_tool_rounds=None,
    cumulative_state_changing: int = 0,
):
    """Execute one full critic turn using the same LLM + tools as the worker.

    The critic sees:
    - Its role prompt (CRITIC_SYSTEM_PROMPT) embedded in the final user turn.
    - The full conversation history (planner brief + all worker turns).
    - A **Deliverables Snapshot** block counting state-changing vs
      exploratory tool calls in the worker's latest turn.  This drives
      the critic's pre-verdict check (see CRITIC_SYSTEM_PROMPT → BEFORE
      you verdict).

    It runs through ``_run_single_turn`` so it gets the same model, tools,
    thinking depth, etc. as the worker.

    Parameters
    ----------
    task : dict
        The live task dict (must be in ``tasks``).
    original_messages : list
        The original message list snapshot (for extracting the user's goal).
    worker_messages : list
        The current full conversation history after the worker's latest
        turn.  Includes system prompt, planner brief, all assistant
        replies, and any previous critic feedback injected as user
        messages.
    iteration : int
        The current worker iteration number (1-based).
    latest_tool_rounds : list[dict] | None
        The worker's ``toolRounds`` snapshot from the latest turn.  Used
        to build the Deliverables Snapshot.
    cumulative_state_changing : int
        Running total of state-changing tool calls across all worker
        turns in this task.  Used for the snapshot footer.

    Returns
    -------
    dict with keys:
        feedback : str     — natural-language critique (tags stripped)
        next_phase : str   — one of 'stop', 'worker', 'planner'
        should_stop : bool — mirror of (next_phase == 'stop')
        plan_defect : str | None — extracted PLAN_DEFECT reason if any
        content : str      — raw full content from the critic
        thinking : str     — critic's thinking (if extended thinking on)
        usage : dict       — token usage
        error : str|None   — error message if the turn failed
    """
    tid = task['id'][:8]

    # Build the Deliverables Snapshot — critic's pre-verdict trigger.
    latest_sc, latest_explore, latest_names = _count_state_changing_rounds(
        latest_tool_rounds or [],
    )
    deliverables_block = _format_deliverables_snapshot(
        latest_sc=latest_sc,
        latest_explore=latest_explore,
        latest_names=latest_names,
        cumulative_sc=cumulative_state_changing + latest_sc,
        iteration=iteration,
    )

    # The critic sees worker history byte-for-byte. Its round-scoped role and
    # deliverables snapshot enter through Context Composer, not a fabricated
    # user turn.
    critic_messages = [dict(m) for m in worker_messages]
    critic_role = (
            '=== Your role for THIS turn: Critic ===\n'
            f'{CRITIC_SYSTEM_PROMPT}\n'
            '=== End critic role ===\n\n'
            f'{deliverables_block}\n\n'
            'Please review the worker\'s latest response (the assistant turn '
            'immediately above) against the Planner\'s checklist and '
            'acceptance criteria (the wrapped user message earlier in this '
            'conversation).\n\n'
            '**Pre-verdict check (MANDATORY):** consult the Deliverables '
            'Snapshot above.  If ``Latest worker turn`` shows zero '
            'state-changing tool calls and the checklist is non-empty, '
            'emit CONTINUE_WORKER with short, concrete "execute the plan, '
            'stop analyzing" feedback — do NOT emit CONTINUE_PLANNER, '
            'that is a worker problem not a plan problem.\n\n'
            'Otherwise: verify each checklist item using tools if needed, '
            'then provide your structured critique per the format in your '
            'critic role.  Keep it SHORT (≤ 2000 chars).\n\n'
            'If the worker asked any clarifying questions or presented '
            'options, you MUST answer them in the "Answers to Worker '
            'Questions" section — speak as the user would.  Apply the '
            'standing preferences from your critic role.\n\n'
            'End with exactly one of:\n'
            '  [VERDICT: STOP]             — all items ✅ and all acceptance '
            'criteria met.\n'
            '  [VERDICT: CONTINUE_WORKER]  — ❌ items remain, worker just '
            'needs more iterations.  (DEFAULT CONTINUE CASE.)\n'
            '  [PLAN_DEFECT: <one-line structural reason>]\n'
            '  [VERDICT: CONTINUE_PLANNER] — plan itself is structurally '
            'wrong.  REQUIRES the PLAN_DEFECT tag above; without it, '
            'the orchestrator will downgrade to CONTINUE_WORKER.'
    )

    logger.debug('[Critic] Starting critic turn for task %s, %d messages, '
                 'latest_sc=%d, cumulative_sc=%d',
                 tid, len(critic_messages), latest_sc,
                 cumulative_state_changing)

    # Run through _run_single_turn — full tools, full thinking
    previous_role = task.get('_contextRoleBlock')
    task['_contextRoleBlock'] = {'name': 'critic', 'content': critic_role}
    try:
        result = _run_single_turn(task, messages_override=critic_messages)
    finally:
        if previous_role is None:
            task.pop('_contextRoleBlock', None)
        else:
            task['_contextRoleBlock'] = previous_role

    raw_content = result.get('content', '')
    error = result.get('error')

    if error:
        logger.warning('[Critic] Critic turn error for %s: %s', tid, error)
        return {
            'feedback': f'Critic encountered an error: {error}',
            'next_phase': 'worker',
            'should_stop': False,
            'plan_defect': None,
            'content': raw_content,
            'thinking': result.get('thinking', ''),
            'usage': result.get('usage', {}),
            'error': error,
        }

    feedback, next_phase, plan_defect = _parse_verdict(raw_content)
    should_stop = (next_phase == 'stop')

    verdict_label = {
        'stop': 'STOP',
        'worker': 'CONTINUE_WORKER',
        'planner': 'CONTINUE_PLANNER',
    }.get(next_phase, next_phase.upper())
    logger.info(
        '[Critic] Task %s — verdict=%s, feedback=%d chars, latest_sc=%d%s',
        tid, verdict_label, len(feedback), latest_sc,
        f', plan_defect={plan_defect!r}' if plan_defect else '',
    )

    return {
        'feedback': feedback,
        'next_phase': next_phase,
        'should_stop': should_stop,
        'plan_defect': plan_defect,
        'content': raw_content,
        'thinking': result.get('thinking', ''),
        'usage': result.get('usage', {}),
        'error': None,
    }


# ``_detect_stuck`` and ``_accumulate_usage`` are imported from
# ``lib.agent_verdict`` at the top of this module under their historical
# names — endpoint.py re-imports them from here, so the public surface is
# unchanged.
