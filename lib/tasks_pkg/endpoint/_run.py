"""Endpoint-mode core loop — Planner → Worker → Critic.

Extracted from the monolithic ``lib/tasks_pkg/endpoint.py``.  Houses the
main entry point ``run_endpoint_task`` (the outer loop), its early-exit
sentinel ``_EarlyExit``, and ``_finalize`` (completion events + result
persistence).

See the package ``__init__`` docstring for the full three-phase
architecture description.

Dependency direction: imports the leaf helpers from ``._replan`` and
``._translate`` — no cycle (those modules do not import ``._run``).
"""

import time

from lib.log import audit_log, get_logger, log_context

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_review import (
    _accumulate_usage,
    _count_state_changing_rounds,
    _detect_stuck,
    _run_critic_turn,
    _run_planner_turn,
)
from lib.agent_verdict import is_incomplete_stop
from lib.agent_core.events import EventType, build_event
from lib.orchestration.loop_policy import (
    advance_zero_deliverable_streak,
    should_inject_zero_deliverable,
)
from lib.tasks_pkg.manager import (
    append_event,
    finalize_chat_task_error,
    notify_terminal_busy_state,
    persist_task_result,
    stamp_chat_task_terminal,
)
from lib.tasks_pkg.orchestrator import (_run_single_turn,
                                        drain_peer_messages_into)
from lib.tasks_pkg.endpoint_prompts import WORKER_DIRECTIVE_HEADER

from lib.tasks_pkg.endpoint._replan import (
    MAX_ITERATIONS,
    MAX_REPLANS,
    _ZERO_DELIVERABLE_DIRECTIVE,
    _build_progress_summary,
    _build_replan_input_messages,
    _reset_worker_messages_with_plan,
)
from lib.tasks_pkg.endpoint._translate import (
    _store_endpoint_turns_on_task,
    _sync_endpoint_turns_to_conversation,
    _trigger_endpoint_auto_translate,
    _trigger_per_turn_auto_translate,
)


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
        # This DRIVER loop owns peer-message delivery at its own iteration
        # boundary (Pillar #6 fast path for big tasks): drain_peer_messages_into
        # injects a sibling's peer message as a tool turn on the NEXT iteration
        # instead of leaving it in the input-box queue until the whole endpoint
        # task ends. ``_peer_driver_owned`` tells the nested run_task NOT to also
        # drain peer items (the flush inside run_task still emits the chip +
        # de-dups the durable row after the LLM consumes them).
        task['_peer_driver_owned'] = True
        iteration = 0
        while True:
            iteration += 1
            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            # ── Peer-message drain (Pillar #6 fast path) ──
            #   At the TOP of each iteration, before the Worker turn, inject any
            #   pending peer message so it reaches the model this iteration. The
            #   helper respects the unmatched-tool_call guard and stashes items
            #   for the run_task flush (chip + durable-row de-dup) that fires
            #   after the Worker's LLM call — preserving exactly-once / never-zero.
            drain_peer_messages_into(task, messages, round_label=iteration)

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

            previous_role = task.get('_contextRoleBlock')
            task['_contextRoleBlock'] = {
                'name': 'worker',
                'content': WORKER_DIRECTIVE_HEADER,
            }
            try:
                turn_result = _run_single_turn(task, messages_override=messages)
            finally:
                if previous_role is None:
                    task.pop('_contextRoleBlock', None)
                else:
                    task['_contextRoleBlock'] = previous_role

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
            zero_deliverable_streak = advance_zero_deliverable_streak(
                zero_deliverable_streak,
                reported=True,
                state_changing=turn_state_changing,
            )
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
            if should_inject_zero_deliverable(zero_deliverable_streak):
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
        with task['content_lock']:
            task['content'] = accumulated_content
        finalize_chat_task_error(
            task,
            envelope,
            endpoint_reason='fatal',
            append_event_fn=append_event,
            persist_task_result_fn=persist_task_result,
        )
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
    status, finish_reason = 'done', 'stop'
    if stop_reason == 'error':
        status, finish_reason = 'error', 'error'
    elif stop_reason == 'aborted':
        status, finish_reason = 'aborted', 'aborted'
    elif is_incomplete_stop(stop_reason):
        # ★ The loop was CUT OFF by a safety cap (max_iterations / max_replans /
        #   stuck), NOT genuinely finished — the objective is unverified.
        #   Surfacing this as a clean status='done'/finish='stop' (as it did
        #   historically) silently reports a budget-exhausted runaway as
        #   success. Status stays 'done' (it is not an ERROR — real work may
        #   have shipped), but finishReason='incomplete' honestly flags
        #   "stopped early, needs review" for the sidebar + finish bar.
        finish_reason = 'incomplete'
        audit_log('loop_incomplete', task_id=tid, mode='endpoint',
                  reason=stop_reason, iterations=min(iteration, MAX_ITERATIONS))
    # ★ Clear _endpoint_phase once the loop is finalized.  Without this the
    #   state snapshot (see routes/chat.py) still reports endpointPhase='reviewing'
    #   after approval, which the frontend's reconnect paths misinterpret as
    #   "critic still running → start a new worker on the next turn".  The
    #   explicit 'done' phase is the authoritative signal used by
    #   connectToTask / _trySSE state-handler to reject ghost worker creation.
    if not stamp_chat_task_terminal(
        task, status=status, finish_reason=finish_reason,
        endpoint_reason=stop_reason,
    ):
        return

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
    # Terminal busy-state broadcast (pt_3ea0e045) — endpoint holds no
    # finalize latch, so the projection is truthful the moment the status
    # flipped above. Keeps the sidebar/composer from reading "generating"
    # past the settled endpoint turn until the next incidental write.
    notify_terminal_busy_state(task)

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
