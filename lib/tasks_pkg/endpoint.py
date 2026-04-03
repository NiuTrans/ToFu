"""Endpoint mode — Planner → Worker → Critic autonomous loop.

Three-phase architecture:

  Phase 0 (Planner): Runs ONCE at the start.  Rewrites the user's raw
  request into a structured brief with a checklist and acceptance criteria.
  The planner's output becomes an assistant message that replaces the
  original user query — both Worker and Critic operate on the refined plan.

  Phase 1 (Worker): Full LLM + tools.  Executes the plan.
  Phase 2 (Critic): Full LLM + tools.  Reviews against the checklist.
  If CONTINUE → inject feedback → back to Phase 1.
  If STOP → done.

Conversation shape visible to Worker & Critic (LLM working messages):
  system → user(planner brief)  [first worker turn]
  system → user(planner brief) → assistant(worker) → user(critic feedback) → ...  [later turns]

  The planner's output REPLACES the original user message so the worker
  sees a clean, structured plan as its user request.  This avoids the old
  phantom pattern where assistant(planner) + user("Execute…") were appended.

Conversation shape in the DB / frontend (display):
  user(original) → assistant(planner, _isEndpointPlanner) → assistant(worker, _epIteration=1)
  → user(critic, _isEndpointReview) → assistant(worker, _epIteration=2) → ...

Termination guardrails:
  1. Critic verdict — STOP means approved
  2. Stuck detection — similar feedback in 2+ consecutive rounds
  3. Max iterations — hard cap at MAX_ITERATIONS (default 10)
  4. Abort — user can abort at any time
"""

import json
import threading
import time
import uuid

from lib.log import get_logger

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
            SET messages=?, updated_at=?, msg_count=?, search_text=?,
                search_tsv=to_tsvector('simple', left(?, 50000))
            WHERE id=? AND user_id=1''',
            (messages_json, now_ms, len(new_messages), search_text, search_text, conv_id))

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

        # ── Accumulate planner turn for DB persistence ──
        planner_turn_msg = {
            'role': 'assistant',
            'content': planner_content,
            'thinking': planner_result.get('thinking', ''),
            'searchRounds': task.get('searchRounds') or [],
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            '_isEndpointPlanner': True,
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
            # message with the planner's structured brief.
            working_messages = []
            user_replaced = False
            for msg in reversed(messages):
                if msg.get('role') == 'user' and not user_replaced:
                    # Replace the last user message with the planner's output
                    working_messages.insert(0, {
                        'role': 'user',
                        'content': planner_content,
                    })
                    user_replaced = True
                else:
                    working_messages.insert(0, dict(msg))

            if not user_replaced:
                # Edge case: no user message found — append as user
                working_messages.append({
                    'role': 'user',
                    'content': planner_content,
                })

            messages = working_messages
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
                'searchRounds': task.get('searchRounds') or [],
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

            feedback  = critic_result['feedback']
            should_stop = critic_result['should_stop']

            if task.get('aborted'):
                stop_reason = 'aborted'
                break

            # ── Stuck detection ──
            is_stuck = False
            if not should_stop:
                feedback_history.append(feedback)
                if _detect_stuck(feedback_history):
                    is_stuck = True
                    should_stop = True
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
                '_isStuck': is_stuck,
                'done': True,
            }
            endpoint_turns.append(critic_turn_msg)

            # ── Emit critic feedback event ──
            append_event(task, {
                'type': 'endpoint_critic_msg',
                'iteration': iteration,
                'content': feedback,
                'should_stop': should_stop,
                'is_stuck': is_stuck,
            })

            # ── Sync to DB after critic review ──
            _store_endpoint_turns_on_task(task, endpoint_turns)
            _sync_endpoint_turns_to_conversation(task, endpoint_turns)

            # ── Check: STOP? ──
            if should_stop:
                if not is_stuck:
                    stop_reason = 'approved'
                logger.info('[Endpoint] %s at iteration %d',
                            'Stuck — stopping' if is_stuck else 'Critic approved',
                            iteration)
                break

            # ── Inject critic feedback as user message for next worker turn ──
            messages.append({'role': 'user', 'content': feedback})

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

            logger.debug('[Endpoint] Iteration %d: CONTINUE, injecting '
                         'critic feedback (%d chars)', iteration, len(feedback))

        # ══════════════════════════════════════
        #  Finalize
        # ══════════════════════════════════════
        _finalize(task, accumulated_content, total_usage, iteration,
                  stop_reason, fallback_model, fallback_from)

    except _EarlyExit:
        _finalize(task, accumulated_content, total_usage, 0,
                  stop_reason, fallback_model, fallback_from)

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


class _EarlyExit(Exception):
    """Internal signal for early exit from the endpoint loop (abort, etc.)."""
    pass


def _finalize(task, accumulated_content, total_usage, iteration,
              stop_reason, fallback_model, fallback_from):
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
