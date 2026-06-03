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
  returns the number of *deliverable* tool calls (write_file / apply_diff
  / insert_content / run_command / create_project / image gen).  The
  critic invocation prompt carries a "Deliverables Snapshot" so the
  LLM-Critic can make the right call, and endpoint.py uses the same
  counter for its orchestrator-side zero-deliverable guard.
- ``_run_planner_turn`` accepts an optional ``planner_tag`` kwarg so re-
  plan calls surface cleanly in logs and audit records.
"""

import re

from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import CRITIC_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
from lib.tasks_pkg.orchestrator import _run_single_turn


# Kill-switch: when '0', CONTINUE_PLANNER is downgraded to CONTINUE_WORKER
# so the redesign can be hot-disabled without a code rollback.
# Defaults to enabled ('1').  Reads ``TOFU_ENDPOINT_REPLAN``
# (documented in CLAUDE.md §9).
def _replan_enabled() -> bool:
    return getenv_compat('TOFU_ENDPOINT_REPLAN',
                         default='1').strip() != '0'


# ══════════════════════════════════════════════════════════
#  Deliverables / state-changing tool counter
# ══════════════════════════════════════════════════════════

# State-changing ("deliverable") tools.  Calls to these are what we count
# as real work; everything else (list_dir, read_files, grep_search,
# find_files, web_search, fetch_url, …) is exploration.
STATE_CHANGING_TOOLS = frozenset({
    'write_file',
    'apply_diff',
    'apply_diffs',
    'insert_content',
    'insert_contents',
    'run_command',
    'create_project',
    'generate_image',
})


def _count_state_changing_rounds(tool_rounds) -> tuple:
    """Count state-changing vs exploratory tool rounds in a single worker turn.

    Parameters
    ----------
    tool_rounds : list[dict] | None
        ``task['toolRounds']`` snapshot — each entry has ``toolName``.

    Returns
    -------
    (int, int, list[str])
        (state_changing_count, exploratory_count, state_changing_tool_names)
        ``state_changing_tool_names`` preserves order + duplicates so the
        deliverables snapshot can show "apply_diff×2, write_file".
    """
    if not tool_rounds:
        return 0, 0, []

    state_changing_names: list[str] = []
    exploratory_count = 0

    for entry in tool_rounds:
        if not isinstance(entry, dict):
            continue
        # Special key used by code_exec rounds whose toolName is different
        # (see executor.py: round_entry may have toolName='code_exec' or
        # tool command).  We treat code_exec as state-changing.
        name = entry.get('toolName') or entry.get('tool_name') or ''
        if name == 'code_exec':
            state_changing_names.append('code_exec')
            continue
        if name in STATE_CHANGING_TOOLS:
            state_changing_names.append(name)
        else:
            exploratory_count += 1

    return len(state_changing_names), exploratory_count, state_changing_names


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

    # Copy the conversation verbatim — same objects in the same order so
    # that [system, ...history] forms an identical prefix across calls.
    planner_messages = [dict(m) for m in messages]

    # Shared planner-directive prefix — identical byte-for-byte whether the
    # underlying user content is a plain string or a multimodal list of
    # blocks.  Keeping this as one constant preserves the LLM provider's
    # prefix-cache discipline (see the docstring above).
    _PLANNER_WRAPPER_PREFIX = (
        '=== Your role for THIS turn: Planner ===\n'
        f'{PLANNER_SYSTEM_PROMPT}\n'
        '=== End planner role ===\n\n'
        'Based on the system prompt and conversation history above, '
        'and the user request below, produce your structured execution '
        'brief for the worker per the format in your planner role.  '
        'You MAY use read-only tools (list_dir, read_files, grep_search) '
        'to explore the codebase, but DO NOT edit files or execute the '
        'task itself — planning only.\n\n'
        '───── User request ─────\n\n'
    )

    # Locate the last user message (= the current turn's request) and
    # wrap ONLY its content with the planner role + directive.
    wrapped = False
    for i in range(len(planner_messages) - 1, -1, -1):
        if planner_messages[i].get('role') == 'user':
            raw_content = planner_messages[i].get('content', '')
            if isinstance(raw_content, list):
                # Multimodal: prepend the planner wrapper as a fresh text block.
                original_blocks = list(raw_content)
                if not original_blocks:
                    new_content = _PLANNER_WRAPPER_PREFIX
                else:
                    logger.info(
                        '[Planner] Task %s (%s) — multimodal user content '
                        'detected (%d blocks); prepending planner wrapper '
                        'as text block',
                        tid, planner_tag, len(original_blocks),
                    )
                    new_content = [
                        {'type': 'text', 'text': _PLANNER_WRAPPER_PREFIX}
                    ] + original_blocks
            else:
                original_content = raw_content or ''
                new_content = _PLANNER_WRAPPER_PREFIX + original_content
            planner_messages[i] = {
                'role': 'user',
                'content': new_content,
            }
            wrapped = True
            break

    if not wrapped:
        logger.warning('[Planner] No user message found for task %s (%s); '
                       'appending a synthetic one', tid, planner_tag)
        planner_messages.append({
            'role': 'user',
            'content': (
                '=== Your role for THIS turn: Planner ===\n'
                f'{PLANNER_SYSTEM_PROMPT}\n'
                '=== End planner role ===\n\n'
                'Produce a structured execution brief for the '
                'conversation above.'
            ),
        })

    logger.info(
        '[Planner] Starting planner turn for task %s (%s), %d messages '
        '(prefix-cache friendly)',
        tid, planner_tag, len(planner_messages),
    )

    result = _run_single_turn(task, messages_override=planner_messages)

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

# Match all three modern tags plus the legacy bare "CONTINUE" (maps to
# CONTINUE_WORKER).
_VERDICT_RE = re.compile(
    r'\[VERDICT:\s*(STOP|CONTINUE_WORKER|CONTINUE_PLANNER|CONTINUE)\s*\]',
    re.IGNORECASE,
)

# Mandatory for CONTINUE_PLANNER: structured plan-defect reason.
# Without this tag in the feedback body, CONTINUE_PLANNER is downgraded
# to CONTINUE_WORKER (see _parse_verdict).
_PLAN_DEFECT_RE = re.compile(
    r'\[PLAN_DEFECT:\s*([^\]]+)\]',
    re.IGNORECASE,
)

# Patterns that indicate the Critic emitted STOP while the feedback still
# contains unresolved items.  Used by the defense-in-depth override.
_UNRESOLVED_EMOJI_RE = re.compile(r'❌')
_UNRESOLVED_PHRASE_RE = re.compile(
    r'\b(?:NOT met|still failing|still NOT met|unresolved)\b',
    re.IGNORECASE,
)


def _parse_verdict(text: str) -> tuple:
    """Parse the critic's output into ``(feedback_text, next_phase,
    plan_defect_reason)``.

    Returns
    -------
    (str, str, str | None)
        - ``feedback_text`` — critic content with verdict + PLAN_DEFECT
          tags + any trailing "### Verdict" header stripped.
        - ``next_phase`` — one of ``'stop'``, ``'worker'``, ``'planner'``.
        - ``plan_defect_reason`` — the extracted PLAN_DEFECT reason if
          the critic supplied one (useful for audit logs + the re-plan
          directive), else ``None``.

    Behaviour:
      * Missing [VERDICT:] tag → defaults to CONTINUE_WORKER.
      * STOP with ❌ / "NOT met" / "still failing" / "unresolved" markers →
        downgraded to CONTINUE_WORKER (previously: CONTINUE_PLANNER).
        Residual ❌ is nearly always a worker-execution problem, not a
        plan-structural problem; forcing a re-plan destroys the worker's
        partial progress and tends to escalate rather than converge.
      * CONTINUE_PLANNER without a [PLAN_DEFECT: ...] tag → downgraded
        to CONTINUE_WORKER.  PLAN_DEFECT gates the expensive planner
        branch so the critic cannot silently escape into an analysis
        spiral.
      * TOFU_ENDPOINT_REPLAN=0 kill-switch → planner downgraded to worker.
    """
    match = None
    # Find the LAST VERDICT match (in case the critic emits more than one)
    for m in _VERDICT_RE.finditer(text):
        match = m

    # Find ANY plan-defect tag — take the last one if multiple.
    defect_match = None
    for m in _PLAN_DEFECT_RE.finditer(text):
        defect_match = m
    plan_defect_reason = defect_match.group(1).strip() if defect_match else None

    if match is None:
        logger.warning('[Critic] No [VERDICT] tag found in critic output '
                       '(%d chars), defaulting to CONTINUE_WORKER', len(text))
        # Still strip any PLAN_DEFECT tag from display text even without
        # a VERDICT — we don't want the tag showing up in the UI.
        feedback = _PLAN_DEFECT_RE.sub('', text).strip()
        return feedback, 'worker', plan_defect_reason

    tag = match.group(1).upper()
    if tag == 'STOP':
        next_phase = 'stop'
    elif tag == 'CONTINUE_PLANNER':
        next_phase = 'planner'
    else:
        # CONTINUE_WORKER or legacy bare CONTINUE
        next_phase = 'worker'

    # Strip the verdict tag from the content
    feedback = text[:match.start()].rstrip()
    # Strip any dangling "### Verdict" markdown header
    feedback = re.sub(
        r'\n*#+\s*Verdict\s*:?\s*$',
        '',
        feedback,
        flags=re.IGNORECASE,
    ).rstrip()
    # Remove the PLAN_DEFECT tag from the display feedback — it is for the
    # orchestrator + re-plan directive, not for the conversation UI.  The
    # raw reason has already been captured in ``plan_defect_reason``.
    feedback = _PLAN_DEFECT_RE.sub('', feedback).rstrip()

    # ── Guard: STOP with unresolved markers → downgrade to CONTINUE_WORKER ──
    if next_phase == 'stop':
        x_count = len(_UNRESOLVED_EMOJI_RE.findall(feedback))
        phrase_hits = _UNRESOLVED_PHRASE_RE.findall(feedback)
        if x_count > 0 or phrase_hits:
            # Flipped from planner → worker in 2026-04-26 rewrite.  A
            # single residual ❌ is almost always "worker didn't finish
            # the last step", not "the plan is structurally wrong".
            # Forcing a re-plan on it wipes the worker's accumulated
            # progress and tends to escalate (see task 00d009c6:
            # 4 plans, 7 iterations, 0 deliverables).  CONTINUE_WORKER
            # lets the worker address the ❌ directly.
            logger.warning(
                '[Critic] Override STOP→CONTINUE_WORKER: feedback still '
                'contains %d ❌ markers and %d unresolved phrases '
                '(previous behaviour: escalate to CONTINUE_PLANNER)',
                x_count, len(phrase_hits),
            )
            audit_log(
                'critic_verdict_override',
                original='stop',
                new='worker',
                x_count=x_count,
                phrase_hits=len(phrase_hits),
                reason='unresolved_markers_in_stop_feedback',
            )
            next_phase = 'worker'

    # ── Guard: CONTINUE_PLANNER without PLAN_DEFECT → downgrade ──
    if next_phase == 'planner':
        if not plan_defect_reason:
            logger.warning(
                '[Critic] Override CONTINUE_PLANNER→CONTINUE_WORKER: no '
                '[PLAN_DEFECT: ...] tag supplied.  Replan requires an '
                'explicit structural reason; a bare CONTINUE_PLANNER is '
                'almost always a worker-execution problem in disguise.'
            )
            audit_log(
                'critic_verdict_override',
                original='planner',
                new='worker',
                reason='missing_plan_defect_tag',
            )
            next_phase = 'worker'
        else:
            # Reject obvious "the worker didn't do X" rationalizations
            # masquerading as plan defects.
            lowered = plan_defect_reason.lower()
            worker_rationalizations = (
                'worker did',
                'worker didn\'t',
                'worker did not',
                'worker needs',
                'worker should',
                'still ❌',
                'remaining ❌',
                'remaining items',
                'more iterations',
            )
            if any(p in lowered for p in worker_rationalizations):
                logger.warning(
                    '[Critic] Override CONTINUE_PLANNER→CONTINUE_WORKER: '
                    'PLAN_DEFECT reason looks like a worker-execution '
                    'problem, not a plan-structural problem: %r',
                    plan_defect_reason,
                )
                audit_log(
                    'critic_verdict_override',
                    original='planner',
                    new='worker',
                    reason='plan_defect_is_worker_problem',
                    defect_preview=plan_defect_reason[:200],
                )
                next_phase = 'worker'

    # ── Kill-switch: downgrade planner→worker when replan disabled ──
    if next_phase == 'planner' and not _replan_enabled():
        logger.info('[Critic] Replan disabled — CONTINUE_PLANNER downgraded to '
                    'CONTINUE_WORKER (TOFU_ENDPOINT_REPLAN=0)')
        next_phase = 'worker'

    return feedback, next_phase, plan_defect_reason


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

    # **Prefix-cache-friendly construction**.  The critic sees the entire
    # worker conversation byte-for-byte identical.  The only delta is ONE
    # freshly appended user message at the end that (a) declares "your
    # role for this turn is Critic" by embedding CRITIC_SYSTEM_PROMPT,
    # (b) asks for the review, and (c) includes the Deliverables Snapshot.
    # The caller discards this ephemeral user turn after parsing the
    # verdict — it never leaks back into worker_messages.
    critic_messages = [dict(m) for m in worker_messages]

    critic_messages.append({
        'role': 'user',
        'content': (
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
        ),
    })

    logger.debug('[Critic] Starting critic turn for task %s, %d messages, '
                 'latest_sc=%d, cumulative_sc=%d',
                 tid, len(critic_messages), latest_sc,
                 cumulative_state_changing)

    # Run through _run_single_turn — full tools, full thinking
    result = _run_single_turn(task, messages_override=critic_messages)

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


# ══════════════════════════════════════════════════════════
#  Stuck detection
# ══════════════════════════════════════════════════════════

def _detect_stuck(feedback_history):
    """Return True if the last two feedback messages are suspiciously similar.

    Uses a simple Jaccard similarity on word sets — if >60% overlap, the
    critic is probably repeating itself.
    """
    if len(feedback_history) < 2:
        return False

    def _word_set(text):
        return set(text.lower().split())

    prev = _word_set(feedback_history[-2])
    curr = _word_set(feedback_history[-1])

    if not curr or not prev:
        return False

    intersection = prev & curr
    union = prev | curr
    jaccard = len(intersection) / len(union) if union else 0

    return jaccard > 0.60


# ══════════════════════════════════════════════════════════
#  Usage accumulation
# ══════════════════════════════════════════════════════════

def _accumulate_usage(total, delta):
    """Merge delta usage dict into total (in-place)."""
    for k, v in (delta or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
