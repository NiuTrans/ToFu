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
  5. Kill switch — ``CHATUI_ENDPOINT_REPLAN=0`` downgrades
     CONTINUE_PLANNER to CONTINUE_WORKER at the parser layer.
  6. Abort — user can abort at any time.
"""

import json
import os
import threading
import time
import uuid

from lib.log import audit_log, get_logger, log_context

logger = get_logger(__name__)

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.tasks_pkg.endpoint_review import (
    _accumulate_usage,
    _detect_stuck,
    _run_critic_turn,
    _run_planner_turn,
)
from lib.tasks_pkg.manager import append_event, create_task, persist_task_result
from lib.tasks_pkg.orchestrator import _run_single_turn, run_task

MAX_ITERATIONS = 10   # hard cap — safety valve to prevent runaway loops
MAX_REPLANS = 3       # hard cap on CONTINUE_PLANNER branches per task


def _replan_enabled() -> bool:
    """Kill-switch: when '0', CONTINUE_PLANNER is downgraded to CONTINUE_WORKER."""
    return os.environ.get('CHATUI_ENDPOINT_REPLAN', '1').strip() != '0'


def _build_worker_directive(plan_content: str) -> str:
    """Wrap a plan body in the standard worker imperative directive.

    Extracted so both the initial planner path AND the replan path produce
    the exact same ``user`` message shape — identical byte-for-byte apart
    from the plan body.  This keeps the prefix-cache discipline in place.
    """
    return (
        'You are the Worker.  Execute the plan below produced by '
        'the Planner.  Work through the checklist items in order, '
        'use your tools to actually edit files / run commands / '
        'verify results — do not just restate the plan.  After '
        'each checklist item, briefly report what you changed.  '
        'Stop only when every checklist item can be verified ✅.\n\n'
        '───── Plan ─────\n\n'
        + plan_content
    )


def _reset_worker_messages_with_plan(original_messages: list, plan_content: str) -> list:
    """Rebuild the worker's working messages: keep system prompts verbatim
    (prefix-cache friendly), replace the last ``user`` with the wrapped plan.

    Used both at initial-plan time and after each CONTINUE_PLANNER replan.
    On replan, the caller passes ``original_messages`` (the task's original
    message list), NOT the accumulated worker/critic turns — the new plan
    starts a clean worker context, while the DB retains the full history
    for display purposes.
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
    return working_messages


def _build_replan_input_messages(original_messages: list, critic_feedback: str) -> list:
    """Build the input message list passed to the Planner for a replan.

    Starts from the ORIGINAL conversation (system + user request) so the
    new plan is grounded in the user's actual ask — not biased by the
    failed worker iterations.  The critic's feedback is appended as an
    imperative user turn that tells the planner what was wrong with the
    previous plan and asks for a revised brief.  Prefix-cache friendly:
    the original [system, ...user] prefix is bitwise identical across the
    first planner call and every subsequent replan.
    """
    planner_input = [dict(m) for m in original_messages]
    revision_directive = (
        '=== Previous plan needs revision ===\n\n'
        f'{critic_feedback}\n\n'
        '=== End revision feedback ===\n\n'
        'Produce a NEW structured execution brief that addresses the '
        'issues raised above.  The previous plan either had the wrong '
        'scope, missed a critical requirement, or chose an approach '
        'that cannot pass its own acceptance criteria.  Your new brief '
        'must use the same format as your original planner role output.'
    )
    planner_input.append({'role': 'user', 'content': revision_directive})
    return planner_input

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
    """
    conv_id = task.get('convId', '')
    tid = task['id'][:8]
    pfx = f'[EndpointSync {tid}]'

    if not endpoint_turns:
        return

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('%s conv=%s Conversation not found — cannot sync endpoint turns', pfx, conv_id)
            return

        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError):
            logger.error('%s conv=%s Failed to parse messages JSON', pfx, conv_id, exc_info=True)
            return

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync', pfx, conv_id)
            return

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

        from lib.database import json_dumps_pg
        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(new_messages)
        search_text = build_search_text(new_messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db, '''UPDATE conversations
            SET messages=?, updated_at=?, msg_count=?, search_text=?
            WHERE id=? AND user_id=1''',
            (messages_json, now_ms, len(new_messages), search_text, conv_id))
        # Update FTS5 index
        if search_text:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                    "SELECT rowid, ? FROM conversations WHERE id = ?",
                    (search_text, conv_id)
                )
                db.commit()
            except Exception as _fts_err:
                logger.debug('[EndpointSync] FTS update failed (non-fatal): %s', _fts_err)

        logger.info('%s conv=%s ✅ Synced %d endpoint turns to conversation '
                    '(base=%d + endpoint=%d = %d total msgs)',
                    pfx, conv_id, len(endpoint_turns),
                    len(base_messages), len(endpoint_turns), len(new_messages))
    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to sync endpoint turns: %s',
                     pfx, conv_id, e, exc_info=True)


def _store_endpoint_turns_on_task(task, endpoint_turns):
    """Store the endpoint turns snapshot on the task dict for poll access."""
    task['_endpoint_turns'] = list(endpoint_turns)


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
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('%s conv=%s Conversation not found — skipping auto-translate',
                           pfx, conv_id[:8])
            return
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('%s conv=%s Failed to parse messages JSON: %s',
                           pfx, conv_id[:8], e)
            return

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
                    _maybe_auto_translate_critic(conv_id, content, idx, db)
                    per_role_scheduled['critic'] += 1
                else:
                    _maybe_auto_translate_assistant(conv_id, content, idx, db)
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
        append_event(task, {
            'type': 'endpoint_iteration',
            'iteration': 0,
            'phase': 'planning',
        })

        planner_result = _run_planner_turn(task, messages)
        _accumulate_usage(total_usage, planner_result.get('usage', {}))

        # Capture fallback info
        if planner_result.get('fallbackModel'):
            fallback_model = planner_result['fallbackModel']
            fallback_from  = planner_result.get('fallbackFrom', '')

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
        append_event(task, {
            'type': 'endpoint_planner_done',
            'content': planner_content,
            'thinking': planner_result.get('thinking', ''),
            'usage': planner_result.get('usage', {}),
        })

        # ── Sync to DB after planner ──
        _store_endpoint_turns_on_task(task, endpoint_turns)
        _sync_endpoint_turns_to_conversation(task, endpoint_turns)

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
        # The inject_search_addendum_to_user naturally adds timestamps to
        # the last user message (now the planner-replaced one).

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
            append_event(task, {
                'type': 'endpoint_iteration',
                'iteration': iteration,
                'phase': 'working',
            })

            # ── Phase 1: WORKER ──
            accumulated_content = ''

            turn_result = _run_single_turn(task, messages_override=messages)

            turn_content  = turn_result.get('content', '')
            turn_usage    = turn_result.get('usage', {})
            turn_messages = turn_result.get('messages', messages)
            turn_error    = turn_result.get('error')

            # Capture fallback info
            if turn_result.get('fallbackModel'):
                fallback_model = turn_result['fallbackModel']
                fallback_from  = turn_result.get('fallbackFrom', '')

            accumulated_content = turn_content
            _accumulate_usage(total_usage, turn_usage)

            # Update working messages with assistant reply
            messages = list(turn_messages)

            # ── Accumulate worker turn for DB persistence ──
            worker_turn_msg = {
                'role': 'assistant',
                'content': turn_content,
                'thinking': turn_result.get('thinking', ''),
                'toolRounds': task.get('toolRounds') or [],
                'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
                '_epIteration': iteration,
            }
            if turn_result.get('usage'):
                worker_turn_msg['usage'] = turn_result['usage']
            endpoint_turns.append(worker_turn_msg)

            # ── Sync to DB after worker turn ──
            _store_endpoint_turns_on_task(task, endpoint_turns)
            _sync_endpoint_turns_to_conversation(task, endpoint_turns)

            if turn_error:
                logger.warning('[Endpoint] Worker turn %d error: %s',
                               iteration, turn_error)
                stop_reason = 'error'
                break

            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            # ── Phase 2: CRITIC ──
            task['_endpoint_phase'] = 'reviewing'
            append_event(task, {
                'type': 'endpoint_iteration',
                'iteration': iteration,
                'phase': 'reviewing',
            })

            critic_result = _run_critic_turn(
                task,
                original_messages=original_messages,
                worker_messages=messages,
            )

            _accumulate_usage(total_usage, critic_result.get('usage', {}))

            feedback    = critic_result['feedback']
            next_phase  = critic_result.get('next_phase',
                                            'stop' if critic_result.get('should_stop') else 'worker')
            should_stop = (next_phase == 'stop')

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
            append_event(task, {
                'type': 'endpoint_critic_msg',
                'iteration': iteration,
                'content': feedback,
                # New field — drives frontend placeholder creation:
                'next_phase': next_phase,
                # Legacy mirror for any clients that haven't upgraded yet:
                'should_stop': should_stop,
                'is_stuck': is_stuck,
            })

            # ── Sync to DB after critic review ──
            _store_endpoint_turns_on_task(task, endpoint_turns)
            _sync_endpoint_turns_to_conversation(task, endpoint_turns)

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
                    feedback_preview=feedback[:200],
                )

                # Emit planning phase + frontend placeholder event.
                task['_endpoint_phase'] = 'planning'
                append_event(task, {
                    'type': 'endpoint_iteration',
                    'iteration': iteration,
                    'phase': 'planning',
                    'replan': True,
                })

                # Run the new planner turn (prefix-cache friendly: input
                # is the original conversation + a single wrapper user).
                replan_input = _build_replan_input_messages(
                    original_messages, feedback,
                )
                with log_context('endpoint_replan', logger=logger):
                    replan_result = _run_planner_turn(task, replan_input)
                _accumulate_usage(total_usage, replan_result.get('usage', {}))

                new_plan = replan_result.get('content', '')
                replan_error = replan_result.get('error')
                if replan_error:
                    logger.warning(
                        '[Endpoint] Replan error: %s — falling back to worker retry',
                        replan_error,
                    )
                    # Fall through to CONTINUE_WORKER behaviour below
                    next_phase = 'worker'
                elif not new_plan:
                    logger.warning(
                        '[Endpoint] Replan produced empty plan — falling back '
                        'to worker retry',
                    )
                    next_phase = 'worker'
                else:
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

                    append_event(task, {
                        'type': 'endpoint_planner_done',
                        'content': new_plan,
                        'thinking': replan_result.get('thinking', ''),
                        'usage': replan_result.get('usage', {}),
                        'plannerIteration': planner_iteration_counter,
                    })

                    # Sync new planner turn to DB
                    _store_endpoint_turns_on_task(task, endpoint_turns)
                    _sync_endpoint_turns_to_conversation(task, endpoint_turns)

                    # Reset the worker context to a CLEAN plan — do NOT
                    # carry over the prior worker/critic turns into the
                    # LLM messages (they remain in the DB for display).
                    # This keeps token budgets sane and avoids having
                    # the worker anchor on the failed approach.
                    messages = _reset_worker_messages_with_plan(
                        original_messages, new_plan,
                    )

                    # Reset stuck-detection history — we're starting a new plan.
                    feedback_history = []

                    # Guard against replan that bumps iteration past MAX_ITERATIONS
                    if iteration + 1 > MAX_ITERATIONS:
                        stop_reason = 'max_iterations'
                        logger.info(
                            '[Endpoint] Max iterations (%d) reached after replan, stopping',
                            MAX_ITERATIONS,
                        )
                        break

                    # Tell frontend to start a new worker turn under the new plan
                    append_event(task, {
                        'type': 'endpoint_new_turn',
                        'iteration': iteration + 1,
                    })
                    logger.info(
                        '[Endpoint] Iteration %d: CONTINUE_PLANNER — new plan '
                        '(%d chars), replan_count=%d',
                        iteration, len(new_plan), replan_count,
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
            append_event(task, {
                'type': 'endpoint_new_turn',
                'iteration': iteration + 1,
            })

            logger.debug('[Endpoint] Iteration %d: CONTINUE_WORKER, injecting '
                         'critic feedback (%d chars)', iteration, len(feedback))

        # ══════════════════════════════════════
        #  Finalize
        # ══════════════════════════════════════
        _finalize(task, accumulated_content, total_usage, iteration,
                  stop_reason, fallback_model, fallback_from,
                  replan_count=replan_count)

    except _EarlyExit:
        _finalize(task, accumulated_content, total_usage, 0,
                  stop_reason, fallback_model, fallback_from,
                  replan_count=0)

    except Exception as e:
        logger.error('[Endpoint] run_endpoint_task FATAL error task=%s',
                     tid, exc_info=True)
        task['error'] = str(e)
        task['status'] = 'error'
        task['finishReason'] = 'error'
        with task['content_lock']:
            task['content'] = accumulated_content
        err_done = {'type': 'done', 'error': str(e), 'finishReason': 'error'}
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
              stop_reason, fallback_model, fallback_from, *, replan_count=0):
    """Emit completion events and persist final task result."""
    tid = task['id'][:8]

    with task['content_lock']:
        task['content'] = accumulated_content
    task['usage'] = total_usage
    task['status'] = 'done'
    task['finishReason'] = 'stop'

    complete_evt = {
        'type': 'endpoint_complete',
        'totalIterations': min(iteration, MAX_ITERATIONS),
        'reason': stop_reason,
        'replanCount': replan_count,
    }
    append_event(task, complete_evt)

    done_evt = {
        'type': 'done',
        'usage': total_usage,
        'finishReason': 'stop',
        'endpointReason': stop_reason,
    }
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
def run_task_sync(config: dict, *, timeout: float = 600) -> str:
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
            task['error'] = str(exc)
            task['status'] = 'error'
        finally:
            with task['content_lock']:
                result_box.append(task.get('content', ''))
            done_event.set()

    worker = threading.Thread(target=_worker, daemon=True,
                              name=f'run_task_sync-{task["id"][:8]}')
    worker.start()

    finished = done_event.wait(timeout=timeout)

    if not finished:
        task['aborted'] = True
        logger.error('[run_task_sync] Task %s timed out after %.0fs',
                     task['id'][:8], timeout)
        return f'❌ Task timed out after {timeout:.0f}s'

    content = result_box[0] if result_box else task.get('content', '')
    if task.get('error'):
        logger.warning('[run_task_sync] Task %s completed with error: %s',
                       task['id'][:8], task['error'])
    return content or ''
