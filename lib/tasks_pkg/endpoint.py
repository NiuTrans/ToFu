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
"""

import threading
import time
import uuid

from lib.log import audit_log, get_logger, log_context

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import WORKER_DIRECTIVE_HEADER
from lib.tasks_pkg.endpoint_review import (
    _accumulate_usage,
    _count_state_changing_rounds,
    _detect_stuck,
    _run_critic_turn,
    _run_planner_turn,
)
from lib.agent_verdict import is_incomplete_stop
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event, create_task, persist_task_result
from lib.tasks_pkg.orchestrator import _run_single_turn, run_task

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

# Legacy re-exports for anything that might still import from here
from lib.tasks_pkg.endpoint_prompts import (  # noqa: F401
    CRITIC_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)

__all__ = [
    'run_endpoint_task',
    'run_task_sync',
]


# ══════════════════════════════════════════════════════════
#  Endpoint turn persistence — ensures multi-turn endpoint
#  data survives SSE timeouts, page reloads, and server crashes
# ══════════════════════════════════════════════════════════

def _sync_endpoint_turns_to_conversation(task, endpoint_turns):
    """Write the accumulated endpoint turns into the conversation's messages in the DB.

    In endpoint mode, the planner produces an assistant message, then each
    worker turn produces an assistant message and each critic review produces
    a user message (with _isEndpointReview=true).  These build up over
    multiple iterations.  The frontend creates them via SSE events, but if
    SSE disconnects (timeout, page close, network), the messages only exist
    in JS memory and are never persisted.

    This function writes the full multi-turn structure to the DB so it
    survives SSE disconnects, page reloads, and poll fallback recovery.

    Returns the absolute index in the DB ``messages`` array of the LAST
    appended endpoint turn (i.e. ``endpoint_turns[-1]``), or ``None`` if
    the sync was skipped or failed.  Callers use this to schedule
    per-turn auto-translation right after each turn lands, instead of
    waiting for the end-of-task safety net.
    """
    conv_id = task.get('convId', '')
    tid = task['id'][:8]
    pfx = f'[EndpointSync {tid}]'

    if not endpoint_turns:
        return None

    try:
        from lib.agent_core.store import get_conversation_store
        store = get_conversation_store()
        loaded = store.load_conversation_messages(conv_id)
        if loaded is None:
            logger.warning('%s conv=%s Conversation not found — cannot sync endpoint turns', pfx, conv_id)
            return None
        messages, _updated_at = loaded

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync', pfx, conv_id)
            return None

        # Find where the original conversation ends and endpoint turns begin.
        original_end = 0
        for i, msg in enumerate(messages):
            if not msg.get('_epIteration') and not msg.get('_isEndpointReview') and not msg.get('_isEndpointPlanner'):
                original_end = i + 1

        # Keep the original messages, replace all endpoint turns
        base_messages = messages[:original_end]

        # ★ FIX: Strip trailing assistant messages without endpoint markers.
        # The frontend's startAssistantResponse() creates an empty placeholder
        # that may persist to DB (via syncConversationToServer) before the
        # endpoint sync runs.  In some race conditions, the placeholder may
        # even have content (e.g., planner deltas streamed into it, or worker
        # content copied via loadConversationMessages merge).  Any trailing
        # assistant without _epIteration or _isEndpointPlanner is a ghost
        # and must be removed — the endpoint_turns list has the canonical copies.
        while (base_messages
               and base_messages[-1].get('role') == 'assistant'
               and not base_messages[-1].get('_epIteration')
               and not base_messages[-1].get('_isEndpointPlanner')):
            ghost = base_messages[-1]
            logger.debug('%s conv=%s Removing trailing ghost assistant placeholder '
                         'from base messages (content=%d chars, timestamp=%s)',
                         pfx, conv_id, len(ghost.get('content', '') or ''),
                         ghost.get('timestamp'))
            base_messages.pop()

        # Append the accumulated endpoint turns
        new_messages = base_messages + endpoint_turns

        store.sync_conversation_with_search(conv_id, new_messages)

        logger.info('%s conv=%s ✅ Synced %d endpoint turns to conversation '
                    '(base=%d + endpoint=%d = %d total msgs)',
                    pfx, conv_id, len(endpoint_turns),
                    len(base_messages), len(endpoint_turns), len(new_messages))
        return len(new_messages) - 1
    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to sync endpoint turns: %s',
                     pfx, conv_id, e, exc_info=True)
        return None


def _store_endpoint_turns_on_task(task, endpoint_turns):
    """Store the endpoint turns snapshot on the task dict for poll access."""
    task['_endpoint_turns'] = list(endpoint_turns)


def _trigger_per_turn_auto_translate(task, turn_msg, msg_idx):
    """Kick off auto-translation for a single endpoint turn that just landed
    in the conversation.  Lets translations run in parallel with the next
    LLM phase (pipelined), instead of all firing serially at the end.

    The end-of-task safety net (``_trigger_endpoint_auto_translate``) still
    runs and dedups against any translate task already in-flight here — so
    a turn that misses this hook (e.g. exception, missing msg_idx) will
    still get translated at the end.

    Parameters
    ----------
    task : dict
        Endpoint task dict (needs ``convId``).
    turn_msg : dict
        The endpoint turn message that was just appended.
    msg_idx : int | None
        Absolute DB index returned by ``_sync_endpoint_turns_to_conversation``.
        If ``None`` the call is a no-op (sync failed; safety net will retry).
    """
    if msg_idx is None:
        return
    conv_id = task.get('convId', '')
    if not conv_id:
        return
    content = turn_msg.get('content') or ''
    if not content:
        return
    role = turn_msg.get('role')
    is_critic = bool(turn_msg.get('_isEndpointReview')) and role == 'user'
    is_planner_or_worker = role == 'assistant' and (
        turn_msg.get('_isEndpointPlanner') or turn_msg.get('_epIteration')
    )
    if not (is_critic or is_planner_or_worker):
        return

    try:
        from lib.tasks_pkg.manager import (
            _maybe_auto_translate_assistant,
            _maybe_auto_translate_critic,
        )
    except Exception as e:
        logger.warning('[Endpoint:PerTurnTranslate] task=%s conv=%s '
                       'helper import failed: %s',
                       task.get('id', '?')[:8], conv_id[:8], e)
        return

    try:
        if is_critic:
            _maybe_auto_translate_critic(conv_id, content, msg_idx)
        else:
            _maybe_auto_translate_assistant(conv_id, content, msg_idx)
    except Exception as e:
        logger.warning('[Endpoint:PerTurnTranslate] task=%s conv=%s msg=%s '
                       'failed (non-fatal, safety net will retry): %s',
                       task.get('id', '?')[:8], conv_id[:8], msg_idx, e)


def _trigger_endpoint_auto_translate(task, endpoint_turns):
    """Trigger server-side auto-translation for every assistant turn in an
    endpoint run.

    The single-turn safety net (``_maybe_auto_translate_assistant``) is
    normally invoked from ``_sync_result_to_conversation``, but
    ``persist_task_result`` deliberately skips that path for endpoint tasks
    (the multi-turn sync is done by ``_sync_endpoint_turns_to_conversation``
    instead).  Without this helper, NO endpoint turn — not the planner, not
    any worker iteration — would ever be auto-translated, even when the
    conversation has ``settings.autoTranslate`` ON.

    This helper re-reads the full persisted message list from the DB so it
    can compute the correct ``msg_idx`` for each assistant turn, then calls
    the existing safety-net function once per assistant turn.  The
    safety-net itself handles:
      - per-conversation ``settings.autoTranslate`` gate,
      - already-translated dedup,
      - running frontend-task dedup against ``_translate_tasks``,
      - stale-partial-translation detection,
      - background thread spawning.

    Critic review messages (``role == 'user'``, ``_isEndpointReview``) are
    also translated via ``_maybe_auto_translate_critic`` — same safety-net
    logic, same autoTranslate gate, just annotated with a ``Critic`` log
    prefix for observability.  The critic bubble displays the translation
    via the frontend's updated ``renderMessage`` critic branch.

    Parameters
    ----------
    task : dict
        The endpoint task dict (needs ``convId`` and ``id``).
    endpoint_turns : list
        The final list of endpoint turn messages synced to the DB.
    """
    conv_id = task.get('convId', '')
    tid = task['id'][:8]
    pfx = f'[Endpoint:AutoTranslate {tid}]'

    logger.info('%s conv=%s Entered — endpoint_turns=%d (task._endpoint_turns=%d)',
                pfx, conv_id[:8] if conv_id else '?',
                len(endpoint_turns or []),
                len(task.get('_endpoint_turns') or []))

    if not conv_id:
        logger.warning('%s Missing conv_id — cannot auto-translate', pfx)
        return
    if not endpoint_turns:
        logger.warning('%s conv=%s No endpoint_turns — nothing to auto-translate '
                       '(this may indicate _store_endpoint_turns_on_task was '
                       'never called before _finalize)', pfx, conv_id[:8])
        return

    # Lazy import to avoid circular-import issues between manager <-> endpoint
    try:
        from lib.tasks_pkg.manager import (
            _maybe_auto_translate_assistant,
            _maybe_auto_translate_critic,
        )
    except Exception as e:
        logger.warning('%s conv=%s Failed to import safety-net helper: %s',
                       pfx, conv_id[:8], e)
        return

    try:
        from lib.agent_core.store import get_conversation_store
        loaded = get_conversation_store().load_conversation_messages(conv_id)
        if loaded is None:
            logger.warning('%s conv=%s Conversation not found — skipping auto-translate',
                           pfx, conv_id[:8])
            return
        messages, _updated_at = loaded

        scheduled = 0
        skipped = 0
        per_role_scheduled = {'planner': 0, 'worker': 0, 'critic': 0}
        for idx, msg in enumerate(messages):
            role = msg.get('role')
            is_planner = bool(msg.get('_isEndpointPlanner'))
            is_worker = bool(msg.get('_epIteration')) and not msg.get('_isEndpointReview')
            is_critic = bool(msg.get('_isEndpointReview')) and role == 'user'

            # Only handle endpoint-produced turns.  Everything else
            # (the original user prompt, any non-endpoint assistant msg,
            # etc.) is skipped silently.
            if not (is_planner or is_worker or is_critic):
                continue

            content = msg.get('content') or ''
            if not content:
                skipped += 1
                continue
            # Skip image-generation outputs (nothing to translate) — guard
            # replicated for the critic path even though critics never emit
            # image-gen markers today.
            if msg.get('_igResult') or msg.get('_isImageGen'):
                skipped += 1
                continue

            try:
                if is_planner:
                    ep_tag = 'planner'
                elif is_worker:
                    ep_tag = f"worker#{msg.get('_epIteration')}"
                else:
                    ep_tag = 'critic'

                logger.info('%s conv=%s turn=%d role=%s ep=%s len=%d — scheduling auto-translate',
                            pfx, conv_id[:8], idx, role, ep_tag, len(content))

                if is_critic:
                    _maybe_auto_translate_critic(conv_id, content, idx)
                    per_role_scheduled['critic'] += 1
                else:
                    _maybe_auto_translate_assistant(conv_id, content, idx)
                    if is_planner:
                        per_role_scheduled['planner'] += 1
                    else:
                        per_role_scheduled['worker'] += 1
                scheduled += 1
            except Exception as e:
                logger.warning('%s conv=%s turn=%d auto-translate trigger failed: %s',
                               pfx, conv_id[:8], idx, e)

        logger.info('%s conv=%s Done — scheduled=%d (planner=%d worker=%d critic=%d) '
                    'skipped=%d (messages=%d)',
                    pfx, conv_id[:8], scheduled,
                    per_role_scheduled['planner'], per_role_scheduled['worker'],
                    per_role_scheduled['critic'],
                    skipped, len(messages))
    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to trigger endpoint auto-translate: %s',
                     pfx, conv_id[:8], e, exc_info=True)


# ══════════════════════════════════════════════════════════
#  Main entry: run_endpoint_task
# ══════════════════════════════════════════════════════════

def run_endpoint_task(task):
    """Outer endpoint loop: planner → work → critic → (stop | inject feedback) → ...

    Three-phase architecture:
      Phase 0 (Planner) — runs once, produces structured brief + checklist
      Phase 1 (Worker)  — full LLM + tools, executes the plan
      Phase 2 (Critic)  — full LLM + tools, verifies against checklist

    Both Worker and Critic use ``_run_single_turn()`` which gives them
    identical model, thinking depth, and tool access.
    """
    if 'id' not in task:
        raise ValueError("run_endpoint_task called with a task dict missing 'id'")
    tid = task['id'][:8]

    original_messages = list(task['messages'])   # snapshot for context
    messages = list(task['messages'])            # mutable working copy

    feedback_history = []    # list of feedback strings for stuck detection
    total_usage = {}
    accumulated_content = ''
    stop_reason = 'completed'
    fallback_model = None
    fallback_from  = None
    fallback_reason = None
    fallback_kind = None
    endpoint_turns = []      # accumulated endpoint turn messages for DB persistence

    logger.info('[Endpoint] Starting endpoint task %s — planner → worker → critic loop',
                tid)

    try:
        # ══════════════════════════════════════
        #  Phase 0: PLANNER (runs once)
        # ══════════════════════════════════════
        if task.get('aborted'):
            stop_reason = 'aborted'
            # Jump to finalize
            raise _EarlyExit()

        task['_endpoint_phase'] = 'planning'
        task['_endpoint_iteration'] = 0
        append_event(task, build_event(
            EventType.ENDPOINT_ITERATION,
            iteration=0,
            phase='planning',
        ))
        # Per-phase retry counter starts fresh for the Planner turn (PR3b).
        task['_premature_retry_count_phase'] = 0

        planner_result = _run_planner_turn(task, messages)
        _accumulate_usage(total_usage, planner_result.get('usage', {}))

        # Capture fallback info
        if planner_result.get('fallbackModel'):
            fallback_model = planner_result['fallbackModel']
            fallback_from  = planner_result.get('fallbackFrom', '')
            fallback_reason = planner_result.get('fallbackReason') or fallback_reason
            fallback_kind = planner_result.get('fallbackKind') or fallback_kind

        planner_content = planner_result.get('content', '')
        planner_error   = planner_result.get('error')

        if planner_error:
            logger.warning('[Endpoint] Planner error for task %s: %s', tid, planner_error)
            # Fall back: use the original user message as-is
            planner_content = ''

        # Planner iteration counter — 1 for the initial plan; incremented
        # on every CONTINUE_PLANNER replan so the DB / UI can distinguish
        # multiple planner bubbles in the same task.
        planner_iteration_counter = 1
        replan_count = 0

        # ── Accumulate planner turn for DB persistence ──
        planner_turn_msg = {
            'role': 'assistant',
            'content': planner_content,
            'thinking': planner_result.get('thinking', ''),
            'toolRounds': task.get('toolRounds') or [],
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            '_isEndpointPlanner': True,
            '_epPlannerIteration': planner_iteration_counter,
        }
        if planner_result.get('usage'):
            planner_turn_msg['usage'] = planner_result['usage']
        endpoint_turns.append(planner_turn_msg)

        # ── Emit planner done event ──
        append_event(task, build_event(
            EventType.ENDPOINT_PLANNER_DONE,
            content=planner_content,
            thinking=planner_result.get('thinking', ''),
            usage=planner_result.get('usage', {}),
        ))

        # ── Sync to DB after planner ──
        _store_endpoint_turns_on_task(task, endpoint_turns)
        _planner_idx = _sync_endpoint_turns_to_conversation(task, endpoint_turns)
        _trigger_per_turn_auto_translate(task, planner_turn_msg, _planner_idx)

        if task.get('aborted'):
            stop_reason = 'aborted'
            raise _EarlyExit()

        # ══════════════════════════════════════
        #  Build the working message list for Worker & Critic
        # ══════════════════════════════════════
        # Shape: system → user(planner brief)
        #
        # The planner's output REPLACES the original user message so the
        # Worker (and later the Critic) sees a clean, structured plan as
        # the user request.  This avoids the phantom conversation pattern
        # where an assistant(planner) + synthetic user("Execute…") pair was
        # appended, which confused context and wasted tokens.
        #
        # Frontend display is unchanged:
        #   user(original) → planner(assistant) → agent → critic → …
        # But the LLM working messages are:
        #   system → user(planner_content)
        # inject_search_addendum_to_user is now a no-op (timestamps moved
        # to the static system block as date-only) — kept only to strip
        # legacy 'Current date and time:' lines from resumed conversations.

        if planner_content:
            # Rebuild messages: keep system messages, replace the last user
            # message with the planner's structured brief — wrapped in an
            # imperative directive so the worker clearly understands it is
            # the *executor*, not the planner.  Without this wrapper the
            # planner's first-person narrative ("I've surveyed…") bleeds
            # into the next assistant turn and the worker keeps writing
            # as if it were still planning (see bug: task mo7z1jnu81bdr3).
            messages = _reset_worker_messages_with_plan(messages, planner_content)
            logger.debug('[Endpoint] Planner replaced user message in working '
                         'messages — %d msgs total', len(messages))
        # else: planner failed, fall back to original messages as-is

        # ══════════════════════════════════════
        #  Loop-wide counters (analysis-spiral prevention)
        # ══════════════════════════════════════
        # ``current_plan`` tracks the plan the Worker is currently
        # executing — used when building the re-plan directive so the
        # new Planner can produce a *delta* rather than a rewrite.
        current_plan = planner_content or ''
        # Running total of state-changing tool calls across all worker
        # turns.  Surfaced to the Critic via the Deliverables Snapshot.
        cumulative_state_changing = 0
        # Counter for consecutive zero-deliverable worker turns.  When
        # it hits ``MAX_ZERO_DELIVERABLE_TURNS``, the orchestrator skips
        # the Critic and injects a hard-coded "execute, don't analyze"
        # directive instead.
        zero_deliverable_streak = 0

        # ══════════════════════════════════════
        #  Worker → Critic loop
        # ══════════════════════════════════════
        iteration = 0
        while True:
            iteration += 1
            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            if iteration > MAX_ITERATIONS:
                stop_reason = 'max_iterations'
                logger.warning('[Endpoint] Safety-valve: iteration %d > %d',
                               iteration, MAX_ITERATIONS)
                break

            # ── Emit: iteration started (Worker phase) ──
            task['_endpoint_phase'] = 'working'
            task['_endpoint_iteration'] = iteration
            append_event(task, build_event(
                EventType.ENDPOINT_ITERATION,
                iteration=iteration,
                phase='working',
            ))

            # ── Phase 1: WORKER ──
            accumulated_content = ''
            # Per-phase retry counter resets at each Worker phase boundary.
            task['_premature_retry_count_phase'] = 0

            turn_result = _run_single_turn(task, messages_override=messages)

            turn_content  = turn_result.get('content', '')
            turn_usage    = turn_result.get('usage', {})
            turn_messages = turn_result.get('messages', messages)
            turn_error    = turn_result.get('error')

            # Capture fallback info
            if turn_result.get('fallbackModel'):
                fallback_model = turn_result['fallbackModel']
                fallback_from  = turn_result.get('fallbackFrom', '')
                fallback_reason = turn_result.get('fallbackReason') or fallback_reason
                fallback_kind = turn_result.get('fallbackKind') or fallback_kind

            accumulated_content = turn_content
            _accumulate_usage(total_usage, turn_usage)

            # Update working messages with assistant reply
            messages = list(turn_messages)

            # ── Count deliverables for this worker turn ──
            # Snapshot the toolRounds BEFORE we stash them on the turn msg
            # (so cumulative accounting is off the same data the critic sees).
            _latest_tool_rounds = list(task.get('toolRounds') or [])
            (turn_state_changing,
             turn_exploratory,
             turn_sc_names) = _count_state_changing_rounds(_latest_tool_rounds)
            cumulative_state_changing += turn_state_changing
            if turn_state_changing == 0:
                zero_deliverable_streak += 1
            else:
                zero_deliverable_streak = 0
            logger.info(
                '[Endpoint] Task %s iter=%d deliverables: state_changing=%d '
                '(%s) exploratory=%d cumulative_sc=%d zero_streak=%d',
                tid, iteration, turn_state_changing,
                ','.join(turn_sc_names) or '-',
                turn_exploratory, cumulative_state_changing,
                zero_deliverable_streak,
            )
            audit_log(
                'endpoint_worker_turn',
                task_id=tid,
                iteration=iteration,
                state_changing=turn_state_changing,
                exploratory=turn_exploratory,
                tools=turn_sc_names,
                cumulative_state_changing=cumulative_state_changing,
                zero_deliverable_streak=zero_deliverable_streak,
            )

            # ── Accumulate worker turn for DB persistence ──
            worker_turn_msg = {
                'role': 'assistant',
                'content': turn_content,
                'thinking': turn_result.get('thinking', ''),
                'toolRounds': _latest_tool_rounds,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                '_epIteration': iteration,
                # Expose per-turn deliverable counts on the DB row so the
                # UI can render a small "X edits / Y reads" badge in the
                # future (consumer to come; harmless metadata for now).
                '_epStateChangingCount': turn_state_changing,
                '_epExploratoryCount': turn_exploratory,
            }
            if turn_result.get('usage'):
                worker_turn_msg['usage'] = turn_result['usage']
            endpoint_turns.append(worker_turn_msg)

            # ── Sync to DB after worker turn ──
            _store_endpoint_turns_on_task(task, endpoint_turns)
            _worker_idx = _sync_endpoint_turns_to_conversation(task, endpoint_turns)
            _trigger_per_turn_auto_translate(task, worker_turn_msg, _worker_idx)

            if turn_error:
                logger.warning('[Endpoint] Worker turn %d error: %s',
                               iteration, turn_error)
                stop_reason = 'error'
                break

            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            # ══════════════════════════════════════════════════
            #  Zero-deliverable guard — SKIP the Critic and inject
            #  a hard-coded "execute, don't analyze" directive.
            # ══════════════════════════════════════════════════
            # Rationale: when the worker produces a narrative-only turn
            # (zero state-changing tool calls), the Critic's LLM-level
            # pre-check usually catches it and emits CONTINUE_WORKER with
            # similar feedback.  But on top of that being an expensive
            # extra full-tool LLM turn, it sometimes mis-routes to
            # CONTINUE_PLANNER and starts the spiral.  For consecutive
            # zero-deliverable turns we short-circuit: synthesise the
            # feedback, skip the critic, and send the worker back in.
            #
            # First zero-deliverable turn still goes through the critic
            # (which may genuinely need to answer a clarifying question);
            # only at ``MAX_ZERO_DELIVERABLE_TURNS`` do we bypass.
            if zero_deliverable_streak >= MAX_ZERO_DELIVERABLE_TURNS:
                logger.warning(
                    '[Endpoint] Task %s iter=%d — %d consecutive '
                    'zero-deliverable worker turns, bypassing Critic and '
                    'injecting execute-not-analyze directive',
                    tid, iteration, zero_deliverable_streak,
                )
                audit_log(
                    'endpoint_zero_deliverable_guard',
                    task_id=tid,
                    iteration=iteration,
                    streak=zero_deliverable_streak,
                )
                # Synthesize the critic turn so the UI still sees a
                # review bubble and the DB stays consistent.
                synthetic_feedback = _ZERO_DELIVERABLE_DIRECTIVE
                critic_turn_msg = {
                    'role': 'user',
                    'content': synthetic_feedback,
                    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    '_isEndpointReview': True,
                    '_epIteration': iteration,
                    '_epApproved': False,
                    '_epNextPhase': 'worker',
                    '_isStuck': False,
                    '_isSyntheticCritic': True,
                    'done': True,
                }
                endpoint_turns.append(critic_turn_msg)
                append_event(task, build_event(
                    EventType.ENDPOINT_CRITIC_MSG,
                    iteration=iteration,
                    content=synthetic_feedback,
                    next_phase='worker',
                    should_stop=False,
                    is_stuck=False,
                    synthetic=True,
                ))
                _store_endpoint_turns_on_task(task, endpoint_turns)
                _synth_idx = _sync_endpoint_turns_to_conversation(task, endpoint_turns)
                _trigger_per_turn_auto_translate(task, critic_turn_msg, _synth_idx)

                messages.append({
                    'role': 'user',
                    'content': synthetic_feedback,
                })

                # Reset the streak counter so the guard doesn't fire
                # again on the same turn index if the worker does one
                # more zero-deliverable pass (it'll re-accumulate from 1
                # in the next iteration's counting block, then fire
                # again at 2, which is what we want).
                zero_deliverable_streak = 0

                if iteration + 1 > MAX_ITERATIONS:
                    stop_reason = 'max_iterations'
                    logger.info('[Endpoint] Max iterations after '
                                'zero-deliverable guard, stopping')
                    break
                append_event(task, build_event(
                    EventType.ENDPOINT_NEW_TURN,
                    iteration=iteration + 1,
                ))
                continue

            # ── Phase 2: CRITIC ──
            task['_endpoint_phase'] = 'reviewing'
            append_event(task, build_event(
                EventType.ENDPOINT_ITERATION,
                iteration=iteration,
                phase='reviewing',
            ))
            # Per-phase retry counter resets for the Critic turn.
            task['_premature_retry_count_phase'] = 0

            critic_result = _run_critic_turn(
                task,
                original_messages=original_messages,
                worker_messages=messages,
                iteration=iteration,
                latest_tool_rounds=_latest_tool_rounds,
                # Exclude the current turn's count from cumulative so the
                # snapshot's "Latest" + "Cumulative" line up correctly.
                cumulative_state_changing=
                    cumulative_state_changing - turn_state_changing,
            )

            _accumulate_usage(total_usage, critic_result.get('usage', {}))

            feedback    = critic_result['feedback']
            next_phase  = critic_result.get('next_phase',
                                            'stop' if critic_result.get('should_stop') else 'worker')
            should_stop = (next_phase == 'stop')
            plan_defect = critic_result.get('plan_defect') or ''

            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            # ── Stuck detection (only on CONTINUE_WORKER) ──
            # Stuck is computed on the worker-feedback history only.  When
            # the Critic chooses CONTINUE_PLANNER, we treat that as a clean
            # restart and reset the history so two different plans don't
            # falsely trigger stuck.
            is_stuck = False
            if next_phase == 'worker':
                feedback_history.append(feedback)
                if _detect_stuck(feedback_history):
                    is_stuck = True
                    should_stop = True
                    next_phase = 'stop'
                    stop_reason = 'stuck'
                    logger.info('[Endpoint] Stuck detected at iteration %d',
                                iteration)

            # ── Accumulate critic review for DB persistence ──
            critic_turn_msg = {
                'role': 'user',
                'content': feedback,
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                '_isEndpointReview': True,
                '_epIteration': iteration,
                '_epApproved': should_stop,
                '_epNextPhase': next_phase,
                '_isStuck': is_stuck,
                'done': True,
            }
            endpoint_turns.append(critic_turn_msg)

            # ── Emit critic feedback event ──
            append_event(task, build_event(
                EventType.ENDPOINT_CRITIC_MSG,
                iteration=iteration,
                content=feedback,
                # New field — drives frontend placeholder creation:
                next_phase=next_phase,
                # Legacy mirror for any clients that haven't upgraded yet:
                should_stop=should_stop,
                is_stuck=is_stuck,
            ))

            # ── Sync to DB after critic review ──
            _store_endpoint_turns_on_task(task, endpoint_turns)
            _critic_idx = _sync_endpoint_turns_to_conversation(task, endpoint_turns)
            _trigger_per_turn_auto_translate(task, critic_turn_msg, _critic_idx)

            # ══════════════════════════════════════════════════
            #  Three-way branch on critic verdict
            # ══════════════════════════════════════════════════
            if next_phase == 'stop':
                if not is_stuck:
                    stop_reason = 'approved'
                logger.info('[Endpoint] %s at iteration %d',
                            'Stuck — stopping' if is_stuck else 'Critic approved',
                            iteration)
                break

            if next_phase == 'planner':
                # ── CONTINUE_PLANNER: run a fresh Planner turn ──
                if replan_count >= MAX_REPLANS:
                    stop_reason = 'max_replans'
                    logger.warning(
                        '[Endpoint] Max replans (%d) reached, stopping',
                        MAX_REPLANS,
                    )
                    break
                replan_count += 1
                audit_log(
                    'endpoint_replan_chosen',
                    task_id=tid,
                    iteration=iteration,
                    replan_count=replan_count,
                    plan_defect=plan_defect[:300] if plan_defect else '',
                    prior_plan_chars=len(current_plan),
                    feedback_preview=feedback[:200],
                )

                # Emit planning phase + frontend placeholder event.
                task['_endpoint_phase'] = 'planning'
                append_event(task, build_event(
                    EventType.ENDPOINT_ITERATION,
                    iteration=iteration,
                    phase='planning',
                    replan=True,
                ))

                # Run the new planner turn.  The planner now sees:
                #   - the PLAN_DEFECT reason (hard structural diagnosis)
                #   - the prior plan verbatim (for delta production)
                #   - the critic's full feedback
                # plus an explicit "DO NOT grow the plan" directive.
                replan_input = _build_replan_input_messages(
                    original_messages, feedback,
                    prior_plan=current_plan,
                    plan_defect=plan_defect,
                    replan_count=replan_count,
                )
                # Per-phase retry counter resets for the replan Planner turn.
                task['_premature_retry_count_phase'] = 0
                with log_context('endpoint_replan', logger=logger):
                    replan_result = _run_planner_turn(
                        task, replan_input,
                        planner_tag=f'replan-{replan_count}',
                    )
                _accumulate_usage(total_usage, replan_result.get('usage', {}))

                new_plan = replan_result.get('content', '')
                replan_error = replan_result.get('error')
                if replan_error:
                    logger.warning(
                        '[Endpoint] Replan error: %s — falling back to worker retry',
                        replan_error,
                    )
                    audit_log('endpoint_replan_failed', task_id=tid,
                              iteration=iteration, replan_count=replan_count,
                              reason='error', detail=str(replan_error)[:200])
                    # Fall through to CONTINUE_WORKER behaviour below
                    next_phase = 'worker'
                elif not new_plan:
                    logger.warning(
                        '[Endpoint] Replan produced empty plan — falling back '
                        'to worker retry',
                    )
                    audit_log('endpoint_replan_failed', task_id=tid,
                              iteration=iteration, replan_count=replan_count,
                              reason='empty')
                    next_phase = 'worker'
                else:
                    # ── Plan-size growth guard ──
                    # The Planner is instructed to produce a DELTA and
                    # not grow the plan.  If it does grow — often
                    # significantly — it usually means the planner is
                    # folding in scope creep that will just extend the
                    # spiral.  We log (audit) a warning when growth
                    # exceeds 50%.  We deliberately do NOT reject the
                    # plan (that could loop infinitely); we just surface
                    # the violation for tuning.
                    if current_plan and len(new_plan) > 0:
                        growth_ratio = len(new_plan) / max(1, len(current_plan))
                        if growth_ratio > 1.5:
                            logger.warning(
                                '[Endpoint] Replan grew plan %.1f× '
                                '(old=%d chars → new=%d chars) — expected '
                                'a delta, not a rewrite.  Accepting plan '
                                'but auditing.',
                                growth_ratio, len(current_plan), len(new_plan),
                            )
                            audit_log(
                                'endpoint_replan_size_violation',
                                task_id=tid,
                                iteration=iteration,
                                replan_count=replan_count,
                                old_chars=len(current_plan),
                                new_chars=len(new_plan),
                                growth_ratio=round(growth_ratio, 2),
                            )

                    planner_iteration_counter += 1
                    new_planner_turn_msg = {
                        'role': 'assistant',
                        'content': new_plan,
                        'thinking': replan_result.get('thinking', ''),
                        'toolRounds': task.get('toolRounds') or [],
                        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                        '_isEndpointPlanner': True,
                        '_epPlannerIteration': planner_iteration_counter,
                    }
                    if replan_result.get('usage'):
                        new_planner_turn_msg['usage'] = replan_result['usage']
                    endpoint_turns.append(new_planner_turn_msg)

                    append_event(task, build_event(
                        EventType.ENDPOINT_PLANNER_DONE,
                        content=new_plan,
                        thinking=replan_result.get('thinking', ''),
                        usage=replan_result.get('usage', {}),
                        plannerIteration=planner_iteration_counter,
                    ))

                    # Sync new planner turn to DB
                    _store_endpoint_turns_on_task(task, endpoint_turns)
                    _replan_idx = _sync_endpoint_turns_to_conversation(task, endpoint_turns)
                    _trigger_per_turn_auto_translate(task, new_planner_turn_msg, _replan_idx)

                    # Reset the worker context under the NEW plan, but
                    # carry over a compact progress summary so the worker
                    # doesn't re-explore from scratch.  This is the single
                    # biggest change vs. the previous re-plan path — see
                    # _build_progress_summary docstring.
                    progress_summary = _build_progress_summary(endpoint_turns)
                    messages = _reset_worker_messages_with_plan(
                        original_messages, new_plan,
                        progress_summary=progress_summary,
                    )
                    logger.info(
                        '[Endpoint] Task %s replan: carried progress summary '
                        '(%d chars) into new worker context',
                        tid, len(progress_summary),
                    )

                    # Update the current_plan tracker so the NEXT replan
                    # can measure its growth against THIS plan (delta
                    # discipline is cumulative).
                    current_plan = new_plan

                    # Reset stuck-detection history — we're starting a new plan.
                    feedback_history = []
                    # Reset the zero-deliverable streak — the new plan
                    # deserves a fresh chance, and the guard shouldn't
                    # fire on turns predating the new plan.
                    zero_deliverable_streak = 0

                    # Guard against replan that bumps iteration past MAX_ITERATIONS
                    if iteration + 1 > MAX_ITERATIONS:
                        stop_reason = 'max_iterations'
                        logger.info(
                            '[Endpoint] Max iterations (%d) reached after replan, stopping',
                            MAX_ITERATIONS,
                        )
                        break

                    # Tell frontend to start a new worker turn under the new plan
                    append_event(task, build_event(
                        EventType.ENDPOINT_NEW_TURN,
                        iteration=iteration + 1,
                    ))
                    logger.info(
                        '[Endpoint] Iteration %d: CONTINUE_PLANNER — new plan '
                        '(%d chars, defect=%r), replan_count=%d',
                        iteration, len(new_plan), plan_defect[:80], replan_count,
                    )
                    continue  # back to top of while — iteration += 1 happens there

            # ── CONTINUE_WORKER: inject critic feedback as user message ──
            # ``feedback`` has already been cleaned by _parse_verdict() —
            # the [VERDICT:] tag and any trailing "### Verdict" header have
            # been stripped.  We only need to wrap it in an imperative
            # directive so the worker treats it as reviewer feedback, not
            # as its own next sentence (see bug: task mo7z1jnu81bdr3 where
            # the worker impersonated the critic and emitted "[VERDICT: …]"
            # due to the conditioning tail).
            wrapped_feedback = (
                '[Feedback from reviewer — address every ❌ / unresolved item '
                'below by actually editing files with your tools, then '
                'summarize the concrete changes you made]\n\n'
                + feedback
            )
            messages.append({'role': 'user', 'content': wrapped_feedback})

            # ── Guard: don't start new turn if we'd exceed max ──
            if iteration + 1 > MAX_ITERATIONS:
                stop_reason = 'max_iterations'
                logger.info('[Endpoint] Max iterations (%d) reached after '
                            'critic, stopping', MAX_ITERATIONS)
                break

            # ── Tell frontend to start new worker turn ──
            append_event(task, build_event(
                EventType.ENDPOINT_NEW_TURN,
                iteration=iteration + 1,
            ))

            logger.debug('[Endpoint] Iteration %d: CONTINUE_WORKER, injecting '
                         'critic feedback (%d chars)', iteration, len(feedback))

        # ══════════════════════════════════════
        #  Finalize
        # ══════════════════════════════════════
        _finalize(task, accumulated_content, total_usage, iteration,
                  stop_reason, fallback_model, fallback_from,
                  replan_count=replan_count,
                  fallback_reason=fallback_reason, fallback_kind=fallback_kind)

    except _EarlyExit as _e_audit:
        logger.debug('[endpoint] run_endpoint_task caught %s: %s', type(_e_audit).__name__, _e_audit)
        _finalize(task, accumulated_content, total_usage, 0,
                  stop_reason, fallback_model, fallback_from,
                  replan_count=0,
                  fallback_reason=fallback_reason, fallback_kind=fallback_kind)

    except Exception as e:
        logger.error('[Endpoint] run_endpoint_task FATAL error task=%s',
                     tid, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model=task.get('model', '') or task.get('config', {}).get('model', ''),
            context='endpoint-fatal', source='endpoint',
        )
        task['error'] = envelope
        task['status'] = 'error'
        task['finishReason'] = 'error'
        with task['content_lock']:
            task['content'] = accumulated_content
        err_done = build_event(EventType.DONE, error=envelope, finishReason='error')
        if task.get('preset'): err_done['preset'] = task['preset']
        if task.get('model'):  err_done['model']  = task['model']
        append_event(task, err_done)
        persist_task_result(task)
        # Even on error, any completed endpoint turns (e.g. planner + a
        # worker iteration) should still get auto-translated.
        try:
            _trigger_endpoint_auto_translate(task, task.get('_endpoint_turns') or [])
        except Exception as _ate:
            logger.warning('[Endpoint] Post-error auto-translate trigger failed task=%s: %s',
                           tid, _ate)


class _EarlyExit(Exception):
    """Internal signal for early exit from the endpoint loop (abort, etc.)."""
    pass


def _finalize(task, accumulated_content, total_usage, iteration,
              stop_reason, fallback_model, fallback_from, *, replan_count=0,
              fallback_reason=None, fallback_kind=None):
    """Emit completion events and persist final task result."""
    tid = task['id'][:8]

    with task['content_lock']:
        task['content'] = accumulated_content
    task['usage'] = total_usage
    # A worker turn that returned an error (stop_reason='error') or a
    # user/superseded abort (stop_reason='aborted') breaks out of the loop and
    # falls through here.  Surfacing those as a clean status='done'/finish='stop'
    # masks a real failure (often with empty content) and silently drops
    # task['error'] set by the failed turn.  Mirror the single-turn orchestrator
    # contract (orchestrator.py:1932) and the FATAL path below: report the
    # true terminal state and carry the error envelope onto the DONE event.
    if stop_reason == 'error':
        task['status'] = 'error'
        task['finishReason'] = 'error'
    elif stop_reason == 'aborted':
        task['status'] = 'aborted'
        task['finishReason'] = 'aborted'
    elif is_incomplete_stop(stop_reason):
        # ★ The loop was CUT OFF by a safety cap (max_iterations / max_replans /
        #   stuck), NOT genuinely finished — the objective is unverified.
        #   Surfacing this as a clean status='done'/finish='stop' (as it did
        #   historically) silently reports a budget-exhausted runaway as
        #   success. Status stays 'done' (it is not an ERROR — real work may
        #   have shipped), but finishReason='incomplete' honestly flags
        #   "stopped early, needs review" for the sidebar + finish bar.
        task['status'] = 'done'
        task['finishReason'] = 'incomplete'
        audit_log('loop_incomplete', task_id=tid, mode='endpoint',
                  reason=stop_reason, iterations=min(iteration, MAX_ITERATIONS))
    else:
        task['status'] = 'done'
        task['finishReason'] = 'stop'
    # ★ Clear _endpoint_phase once the loop is finalized.  Without this the
    #   state snapshot (see routes/chat.py) still reports endpointPhase='reviewing'
    #   after approval, which the frontend's reconnect paths misinterpret as
    #   "critic still running → start a new worker on the next turn".  The
    #   explicit 'done' phase is the authoritative signal used by
    #   connectToTask / _trySSE state-handler to reject ghost worker creation.
    task['_endpoint_phase'] = 'done'
    task['_endpoint_stop_reason'] = stop_reason

    complete_evt = build_event(
        EventType.ENDPOINT_COMPLETE,
        totalIterations=min(iteration, MAX_ITERATIONS),
        reason=stop_reason,
        replanCount=replan_count,
    )
    append_event(task, complete_evt)

    done_evt = build_event(
        EventType.DONE,
        usage=total_usage,
        finishReason=task['finishReason'],
        endpointReason=stop_reason,
    )
    if task['finishReason'] == 'incomplete':
        # Explicit human-facing flag: the loop was cut off by a safety cap and
        # the objective is unverified — the frontend renders a "stopped early,
        # needs review" affordance instead of a clean-done finish bar.
        done_evt['incomplete'] = True
    if task.get('error'):
        done_evt['error'] = task['error']
    if task.get('preset'):
        done_evt['preset'] = task['preset']
    if task.get('model'):
        done_evt['model'] = task['model']
    if task.get('thinkingDepth'):
        done_evt['thinkingDepth'] = task['thinkingDepth']
    if task.get('toolSummary'):
        done_evt['toolSummary'] = task['toolSummary']
    if task.get('apiRounds'):
        done_evt['apiRounds'] = task['apiRounds']
    if fallback_model:
        done_evt['fallbackModel'] = fallback_model
        done_evt['fallbackFrom']  = fallback_from or ''
        if fallback_reason:
            done_evt['fallbackReason'] = fallback_reason
            task['_fallback_reason'] = fallback_reason
        if fallback_kind:
            done_evt['fallbackKind'] = fallback_kind
            task['_fallback_kind'] = fallback_kind
    append_event(task, done_evt)
    persist_task_result(task)

    # ── Server-side auto-translate safety net (endpoint mode) ──
    # persist_task_result deliberately skips _sync_result_to_conversation
    # for endpoint tasks, which also skips the single-turn auto-translate
    # trigger.  We re-fire the safety-net here, once per assistant turn,
    # so planner + every worker iteration gets translated even if the
    # frontend tab is closed / offline / switched away.  The safety-net
    # itself checks settings.autoTranslate and dedups against running
    # frontend translate tasks, so duplicate work is avoided.
    try:
        _trigger_endpoint_auto_translate(task, task.get('_endpoint_turns') or [])
    except Exception as e:
        logger.warning('[Endpoint] Auto-translate trigger failed (non-fatal) task=%s: %s',
                       tid, e)

    logger.info('[Endpoint] Task %s complete — reason=%s iterations=%d',
                tid, stop_reason, min(iteration, MAX_ITERATIONS))


# ══════════════════════════════════════════════════════════
#  run_task_sync — synchronous wrapper for Feishu/API consumers
# ══════════════════════════════════════════════════════════
def _format_progress_event(ev: dict) -> str:
    """Render a task event into a one-line progress string for a non-streaming
    consumer (Feishu bot), or '' when the event is not progress-worthy.

    Only ``tool_start`` is surfaced — it's the glanceable "the bot is doing
    something" signal. Everything else (deltas, usage, snapshots) is noise for
    a chat-bot progress ping.
    """
    if not isinstance(ev, dict):
        return ''
    if ev.get('type') == EventType.TOOL_START:
        name = ev.get('toolName') or 'tool'
        query = (ev.get('query') or '').strip()
        return f'Running {name}: {query}' if query else f'Running {name}…'
    return ''


def _drain_progress(task: dict, cursor: int, progress_fn) -> int:
    """Forward any task events appended since ``cursor`` to ``progress_fn``.

    Returns the new cursor. Errors raised by ``progress_fn`` are swallowed
    (logged at debug) — progress reporting must never break the task.
    """
    try:
        with task['events_lock']:
            new = list(task['events'][cursor:])
    except Exception as e:
        logger.debug('[run_task_sync] progress drain failed (ignored): %s', e)
        return cursor
    advanced = cursor + len(new)
    for ev in new:
        line = _format_progress_event(ev)
        if not line:
            continue
        try:
            progress_fn(line)
        except Exception as e:
            logger.debug('[run_task_sync] progress_fn raised (ignored): %s', e)
    return advanced


def run_task_sync(config: dict, *, timeout: float = 600, progress_fn=None) -> str:
    """Run a task synchronously and return the final content string.

    This is the entry point for non-streaming consumers (Feishu bot,
    scheduled tasks, etc.) that just need the final answer text.

    Spawns ``run_task`` in a dedicated daemon thread (matching the web-UI
    pattern) and waits for completion via ``threading.Event``.

    Parameters
    ----------
    config : dict
        Task config dict with 'model', 'messages', and optional tool settings.
    timeout : float
        Maximum seconds to wait (default 600 = 10 min).
    progress_fn : callable, optional
        Called with a one-line progress string each time a long-running tool
        starts (e.g. ``"Running web_search: latest news"``). Lets a
        non-streaming consumer stream intermediate progress while the task
        runs. Exceptions from the callback are swallowed (logged at debug).

    Returns
    -------
    str
        The assistant's final response text, or an error message.
    """
    cfg = dict(config)
    conv_id = cfg.pop('conversationId', f'sync-{uuid.uuid4().hex[:8]}')
    messages = cfg.pop('messages', [])

    task = create_task(conv_id, messages, cfg)
    done_event = threading.Event()
    result_box: list = []

    def _worker():
        try:
            run_task(task)
        except Exception as exc:
            logger.error('[run_task_sync] Task %s failed: %s',
                         task['id'][:8], exc, exc_info=True)
            from lib.error_envelope import from_exception as _err_from_exc
            task['error'] = _err_from_exc(
                exc, model=cfg.get('model', ''),
                context='run_task_sync', source='endpoint-sync',
            )
            task['status'] = 'error'
        finally:
            with task['content_lock']:
                result_box.append(task.get('content', ''))
            done_event.set()

    worker = threading.Thread(target=_worker, daemon=True,
                              name=f'run_task_sync-{task["id"][:8]}')
    worker.start()

    if progress_fn is None:
        finished = done_event.wait(timeout=timeout)
    else:
        # Poll for new events while waiting so a non-streaming consumer can
        # forward live tool-start progress. ``done_event.wait(0.5)`` returns
        # True the instant the worker finishes, so this adds at most ~0.5s of
        # post-completion latency and zero overhead once done.
        import time as _time
        _deadline = _time.monotonic() + timeout
        _cursor = 0
        finished = False
        while True:
            if done_event.wait(timeout=0.5):
                _cursor = _drain_progress(task, _cursor, progress_fn)
                finished = True
                break
            _cursor = _drain_progress(task, _cursor, progress_fn)
            if _time.monotonic() >= _deadline:
                break

    if not finished:
        task['aborted'] = True
        logger.error('[run_task_sync] Task %s timed out after %.0fs',
                     task['id'][:8], timeout)
        return f'Task timed out after {timeout:.0f}s'

    content = result_box[0] if result_box else task.get('content', '')
    _err = task.get('error')
    if _err:
        _err_summary = _err.get('detail') or _err.get('message') or _err.get('raw') if isinstance(_err, dict) else str(_err)
        logger.warning('[run_task_sync] Task %s completed with error: %s',
                       task['id'][:8], _err_summary)
    return content or ''
