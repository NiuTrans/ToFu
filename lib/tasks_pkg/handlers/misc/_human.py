# HOT_PATH
"""Human-facing ``ask_human`` and structured-checklist handlers.

This module owns the handler dependency bindings used by tests and runtime.
A Project Router's question is also relayed to the owner's Global Router
(``lib.router.relay.relay_project_question``, docs/ROUTER.md R3).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.executor import (
    _build_simple_meta,
    _finalize_tool_round,
    tool_registry,
)
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

_NONBLOCKING_CONTENT = (
    'Question shown to the user without waiting (id {guidance_id}). Keep working '
    'on whatever does not depend on the answer; if you assume a default meanwhile, '
    'say which. Their reply will arrive later as a normal user message quoting '
    'this question.')
_SUPERSEDED_CONTENT = (
    'The user replied in chat instead of answering this question; their message '
    'follows. Treat it as their answer or new direction.')


def _requests_nonblocking(fn_args):
    """Only an explicit false opts out of waiting; anything else keeps the default."""
    value = fn_args.get('blocking')
    return value is False or (isinstance(value, str) and value.strip().lower() == 'false')


def _question_has_reader(task):
    """Whether a person reads this task's questions.

    Besides attended chat tasks, the currently bound project Coordinator's own
    root task qualifies even when unattended (a result-driven continuation):
    its conversation is the user's channel. A failed role lookup has no reader.
    """
    if not task.get('_attended'):
        try:
            from lib.tasks_pkg.coordinator_policy import coordinator_scope
            return coordinator_scope(task) is not None
        except Exception:
            logger.warning('[Executor] ask_human: Coordinator role lookup failed; '
                           'treating task=%s as unattended',
                           task.get('id', '?')[:8], exc_info=True)
            return False
    return True


def _is_human_author(author, owner):
    if not isinstance(author, dict) or author.get('actor') != 'human':
        return False
    from lib.turn_initiation import require_human_turn, steer_author_turn
    try:
        require_human_turn(steer_author_turn(author), user_id=owner)
    except ValueError:
        return False
    except Exception:
        # An unreadable authority delays the answer to the next poll, like a receipt read.
        logger.warning('[Executor] ask_human: steer author verification failed', exc_info=True)
        return False
    return True


def _human_message_pending(task):
    """Whether a human chat message waits in this turn's steer lane.

    The lane also carries nonhuman input (channel events, relayed reports and
    questions, forwarded context; docs/ROUTER.md). The steer seam records a
    human author only for human input (``turn_initiation.steer_author``), and a
    Global Router forward of the owner's message counts only once
    ``require_human_turn`` verifies it (R2), so only the owner's own words can
    answer or supersede a question.
    """
    from lib.agent_inbox import pending_items
    from lib.tasks_pkg.manager import task_user_id
    owner = task_user_id(task)
    return any(_is_human_author(item.get('steerAuthor'), owner)
               for item in pending_items(task['convId'], modes=['user-steer']))


def _await_answer(task, guidance_id, publish_request):
    """Wait for the card answer; a human chat message sent into this turn ends the wait.

    Returns ``(response, superseded)``. The shared wait reads an accepted answer
    before this check, so an answer that arrived first still wins; Stop keeps
    its abort result because the caller checks ``aborted`` first.
    """
    from lib.tasks_pkg.human_guidance import request_human_guidance
    conversation_id = task.get('convId')
    if not conversation_id:
        return request_human_guidance(
            guidance_id, task=task, on_registered=publish_request), False
    replied = False

    def replied_in_chat():
        nonlocal replied
        replied = replied or _human_message_pending(task)
        return replied

    response = request_human_guidance(
        guidance_id, task=task, on_registered=publish_request,
        abort_check=replied_in_chat)
    return response, response is None and replied


def _relay_to_global_router(task, tc_id, round_entry, blocking):
    """A Project Router's question also reaches the owner's Global Router (docs/ROUTER.md R3).

    The notice points at this card; a relay failure never fails the question.
    """
    try:
        from lib.tasks_pkg.coordinator_policy import coordinator_scope
        scope = coordinator_scope(task)
        if scope is None:
            return
        from lib.router.relay import relay_project_question
        relay_project_question(scope['user_id'], {
            'projectPath': scope['project_path'], 'coordinatorId': scope['conversation_id'],
            'turnId': task['_turnId'], 'toolCallId': tc_id, 'guidanceId': round_entry['guidanceId'],
            'blocking': blocking, 'question': round_entry['guidanceQuestion'],
            'options': round_entry['guidanceOptions'],
            'questions': round_entry.get('guidanceBatchQuestions') or []})
    except Exception:
        logger.warning('[Executor] ask_human: Global Router question relay failed: '
                       'guidance_id=%s, task=%s', round_entry.get('guidanceId'),
                       task.get('id', '?')[:8], exc_info=True)


def _finalize_nonblocking(task, fn_name, rn, round_entry, title, extra):
    """Settle a shown question at once; its reply arrives as a chat message."""
    round_entry['guidanceBlocking'] = False
    tool_content = _NONBLOCKING_CONTENT.format(guidance_id=extra['guidanceId'])
    logger.info('[Executor] ask_human shown without waiting: guidance_id=%s, '
                'task=%s', extra['guidanceId'], task.get('id', '?')[:8])
    meta = _build_simple_meta(
        fn_name, tool_content, source='HumanGuidance', title=title,
        snippet='Asked without waiting for an answer', badge='asked',
        extra={**extra, 'blocking': False})
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tool_content


def _finalize_superseded(task, fn_name, rn, round_entry, title, extra):
    """Settle a waiting question the user answered with a chat message instead."""
    round_entry['guidanceSuperseded'] = True
    logger.info('[Executor] ask_human superseded by a chat message: '
                'guidance_id=%s, task=%s', extra['guidanceId'],
                task.get('id', '?')[:8])
    meta = _build_simple_meta(
        fn_name, _SUPERSEDED_CONTENT, source='HumanGuidance', title=title,
        snippet='The user replied in chat instead', badge='superseded',
        extra=extra)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return _SUPERSEDED_CONTENT


@tool_registry.handler('ask_human', category='human_guidance',
                       description='Ask the user a question and wait for their response')
def _handle_ask_human(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle ask_human tool — block until the user responds unless blocking=false."""
    from lib.ids import short_id

    # Batch mode: questions[] takes precedence over single-question fields.
    questions = fn_args.get('questions')
    if isinstance(questions, list) and len(questions) >= 2:
        return _handle_ask_human_batch(
            task, tc, fn_name, tc_id, fn_args, rn, round_entry,
            cfg, project_path, project_enabled, questions)

    question = fn_args.get('question', '')
    response_type = fn_args.get('response_type', 'free_text')
    options = fn_args.get('options', [])
    # Defensive: models sometimes return options as a JSON string, a dict,
    #   or omit it entirely. Normalize to a list of dicts so the frontend's
    #   options.map(…) call can never crash on a string/object.
    if isinstance(options, str):
        try:
            import json as _json
            options = _json.loads(options)
        except (ValueError, TypeError) as _e:
            logger.warning('[Executor] ask_human: options arrived as a '
                           'non-JSON string, coercing to []: %s', _e)
            options = []
    if not isinstance(options, list):
        logger.warning('[Executor] ask_human: options not a list '
                       '(type=%s), coercing to []',
                       type(options).__name__)
        options = []
    # Normalise each option to a dict so the frontend receives a
    # uniform shape even if the model emitted bare strings.
    _norm_opts = []
    for _o in options:
        if isinstance(_o, dict):
            _norm_opts.append(_o)
        elif isinstance(_o, str):
            _norm_opts.append({'label': _o})
        else:
            logger.debug('[Executor] ask_human: dropping non-dict/str '
                         'option of type=%s', type(_o).__name__)
    options = _norm_opts

    if not question:
        logger.warning('[Executor] ask_human called with empty question, task=%s',
                       task.get('id', '?')[:8])
        tool_content = 'Error: question parameter is required.'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title='Missing question', snippet='No question provided',
            badge='error',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    guidance_id = short_id('hg_', 12)
    logger.info('[Executor] ask_human: question=%.200s, type=%s, '
                'options=%d, guidance_id=%s, task=%s',
                question, response_type, len(options), guidance_id,
                task.get('id', '?')[:8])

    round_entry['status'] = 'awaiting_human'
    round_entry['guidanceId'] = guidance_id
    round_entry['guidanceQuestion'] = question
    round_entry['guidanceType'] = response_type
    round_entry['guidanceOptions'] = options

    def publish_request():
        append_event(task, {
            'type': 'human_guidance_request',
            'roundNum': rn,
            'toolCallId': tc_id,
            'guidanceId': guidance_id,
            'question': question,
            'responseType': response_type,
            'options': options,
        })

    if not _question_has_reader(task):
        publish_request()
        # No interactive session can answer this. Only the three routes/chat.py
        # entry points stamp task['_attended']=True; every headless/autonomous
        # surface (/api/v1/agent/run, scheduler, compat OpenAI/Anthropic,
        # swarm sub-agents) omits it.
        # Blocking here would wedge the task on the 120s abort-poll with nobody
        # to resolve it (mirrors the attendance-aware write-approval gate in
        # tool_dispatch.py). Return a clear sentinel so the model proceeds on
        # its best assumption instead.
        logger.warning('[Executor] ask_human in an unattended execution mode '
                       '(no interactive session) — returning sentinel instead '
                       'of blocking: guidance_id=%s, task=%s',
                       guidance_id, task.get('id', '?')[:8])
        user_response = None
        tool_content = ('Cannot ask the user in this execution mode — no '
                        'interactive session is available to answer. Proceed '
                        'with your best assumption or use the tools available '
                        'to you to find the answer yourself.')
        # Mark the round resolved (not awaiting_human) so no UI/consumer waits.
        round_entry['status'] = 'unanswerable'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title=question[:2000],
            snippet='No interactive session — proceeding without an answer',
            badge='unanswerable',
            extra={
                'guidanceId': guidance_id,
                'question': question,
                'responseType': response_type,
                'unanswerable': True,
            },
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False
    guidance_meta = {
        'guidanceId': guidance_id,
        'question': question,
        'responseType': response_type,
    }
    blocking = not _requests_nonblocking(fn_args)
    _relay_to_global_router(task, tc_id, round_entry, blocking)
    if not blocking:
        tool_content = _finalize_nonblocking(
            task, fn_name, rn, round_entry, question[:2000], guidance_meta)
        return tc_id, tool_content, False
    logger.info('[Executor] ask_human blocking indefinitely for user '
                'response: guidance_id=%s, task=%s',
                guidance_id, task.get('id', '?')[:8])
    user_response, superseded = _await_answer(task, guidance_id, publish_request)

    if task.get('aborted') or (user_response is None and not superseded):
        logger.warning('[Executor] ask_human aborted/cancelled: '
                       'guidance_id=%s, task=%s, aborted=%s — round left '
                       'awaiting_human so the turn settlement keeps '
                       'offering the answer_guidance late-answer resume',
                       guidance_id, task.get('id', '?')[:8],
                       task.get('aborted', False))
        # Do NOT finalize this round with a placeholder result. Finalizing
        # closes the replayable gap: the settlement then stops offering
        # answer_guidance and the interactive question card collapses into
        # a dead static receipt the moment the user presses Stop. The
        # dangling-round sweep never touches awaiting_human rounds, and
        # the settlement's missing_tool_result handling offers the
        # late-answer resume for exactly this shape.
        return tc_id, '[Task was aborted while waiting for human guidance]', False
    if superseded:
        tool_content = _finalize_superseded(
            task, fn_name, rn, round_entry, question[:2000], guidance_meta)
        return tc_id, tool_content, False
    tool_content = f'Human response: {user_response}'
    logger.info('[Executor] ask_human received response: '
                'guidance_id=%s, response_len=%d, task=%s',
                guidance_id, len(user_response), task.get('id', '?')[:8])

    # No title/snippet truncation: the user specifically flagged that
    #   "incomplete displays are not allowed" — the original 80-char title
    #   and 120-char snippet caps were producing cut-off question text
    #   ending mid-word (e.g. "…exercise t"). Pass the full strings; the
    #   frontend renderer already word-wraps. We only need a soft upper
    #   bound so a pathological 100 KB prompt doesn't bloat every SSE
    #   event — cap at 2000 chars with an ellipsis for safety, which is
    #   well above any legitimate ask_human question.
    _FULL_LIMIT = 2000
    def _clip(s):
        if not s:
            return s
        return s if len(s) <= _FULL_LIMIT else s[:_FULL_LIMIT - 1] + '…'
    meta = _build_simple_meta(
        fn_name, tool_content, source='HumanGuidance',
        title=_clip(question),
        snippet=_clip(user_response or 'No response'),
        badge='answered' if user_response else 'aborted',
        extra={
            'guidanceId': guidance_id,
            'question': question,
            'responseType': response_type,
            'userResponse': user_response,
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


def _normalize_batch_questions(questions):
    """Validate and normalize the questions[] array for batch mode."""
    normalized = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            logger.warning('[Executor] ask_human batch: dropping non-dict '
                           'question at index %d', i)
            continue
        question_text = str(q.get('question') or '').strip()
        if not question_text:
            logger.warning('[Executor] ask_human batch: dropping empty '
                           'question at index %d', i)
            continue
        rt = q.get('response_type', 'free_text')
        if rt not in ('free_text', 'choice'):
            rt = 'free_text'
        opts = q.get('options') or []
        if isinstance(opts, str):
            try:
                import json as _json
                opts = _json.loads(opts)
            except (ValueError, TypeError):
                opts = []
        if not isinstance(opts, list):
            opts = []
        norm_opts = []
        for o in opts:
            if isinstance(o, dict):
                norm_opts.append(o)
            elif isinstance(o, str):
                norm_opts.append({'label': o})
        normalized.append({
            'question': question_text,
            'response_type': rt,
            'options': norm_opts,
        })
    return normalized


def _handle_ask_human_batch(task, tc, fn_name, tc_id, fn_args, rn,
                            round_entry, cfg, project_path, project_enabled,
                            raw_questions):
    """Handle ask_human batch mode — block until the user answers all questions."""
    from lib.ids import short_id

    questions = _normalize_batch_questions(raw_questions)
    if len(questions) < 2:
        # Fall back to single-question mode with the first valid question.
        fn_args = {key: value for key, value in fn_args.items()
                   if key != 'questions'}
        if questions:
            fn_args['question'] = questions[0]['question']
            fn_args['response_type'] = questions[0]['response_type']
            fn_args['options'] = questions[0]['options']
        return _handle_ask_human(
            task, tc, fn_name, tc_id, fn_args, rn, round_entry,
            cfg, project_path, project_enabled)

    guidance_id = short_id('hg_', 12)
    logger.info('[Executor] ask_human batch: %d questions, guidance_id=%s, '
                'task=%s', len(questions), guidance_id,
                task.get('id', '?')[:8])

    # Use a synthetic top-level question that summarizes the batch.
    summary = 'Multiple questions (batch form):'
    for i, q in enumerate(questions):
        summary += f'\n{i + 1}. {q["question"][:200]}'

    round_entry['status'] = 'awaiting_human'
    round_entry['guidanceId'] = guidance_id
    round_entry['guidanceQuestion'] = summary
    round_entry['guidanceType'] = 'batch'
    round_entry['guidanceBatchQuestions'] = questions
    round_entry['guidanceOptions'] = []

    def publish_request():
        append_event(task, {
            'type': 'human_guidance_request',
            'roundNum': rn,
            'toolCallId': tc_id,
            'guidanceId': guidance_id,
            'question': summary,
            'responseType': 'batch',
            'batchQuestions': questions,
            'options': [],
        })

    if not _question_has_reader(task):
        publish_request()
        logger.warning('[Executor] ask_human batch in an unattended execution '
                       'mode — returning sentinel: guidance_id=%s, task=%s',
                       guidance_id, task.get('id', '?')[:8])
        user_response = None
        tool_content = ('Cannot ask the user in this execution mode — no '
                        'interactive session is available to answer. Proceed '
                        'with your best assumption or use the tools available '
                        'to you to find the answer yourself.')
        round_entry['status'] = 'unanswerable'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title=summary[:200],
            snippet='No interactive session — proceeding without an answer',
            badge='unanswerable',
            extra={
                'guidanceId': guidance_id,
                'question': summary,
                'responseType': 'batch',
                'batchQuestions': questions,
                'unanswerable': True,
            },
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False
    guidance_meta = {
        'guidanceId': guidance_id,
        'question': summary,
        'responseType': 'batch',
        'batchQuestions': questions,
    }
    blocking = not _requests_nonblocking(fn_args)
    _relay_to_global_router(task, tc_id, round_entry, blocking)
    if not blocking:
        tool_content = _finalize_nonblocking(
            task, fn_name, rn, round_entry, summary[:200], guidance_meta)
        return tc_id, tool_content, False
    logger.info('[Executor] ask_human batch blocking indefinitely for '
                'user response: guidance_id=%s, task=%s',
                guidance_id, task.get('id', '?')[:8])
    user_response, superseded = _await_answer(task, guidance_id, publish_request)

    if task.get('aborted') or (user_response is None and not superseded):
        logger.warning('[Executor] ask_human batch aborted/cancelled: '
                       'guidance_id=%s, task=%s, aborted=%s — round left '
                       'awaiting_human so the turn settlement keeps '
                       'offering the answer_guidance late-answer resume',
                       guidance_id, task.get('id', '?')[:8],
                       task.get('aborted', False))
        return tc_id, '[Task was aborted while waiting for human guidance]', False
    if superseded:
        tool_content = _finalize_superseded(
            task, fn_name, rn, round_entry, summary[:200], guidance_meta)
        return tc_id, tool_content, False

    # Parse the batch response JSON.
    import json as _json
    answers = None
    try:
        parsed = _json.loads(user_response)
        if isinstance(parsed, list):
            answers = parsed
    except (ValueError, TypeError):
        pass

    if answers is None:
        # User submitted a single text response for the whole batch.
        answers = [{'index': i, 'answer': user_response}
                   for i in range(len(questions))]

    # Build the tool result from the answers.
    result_parts = []
    for i, q in enumerate(questions):
        answer_entry = next(
            (a for a in answers
             if isinstance(a, dict) and a.get('index') == i),
            None,
        )
        answer_text = (answer_entry.get('answer', '')
                       if answer_entry else '(no answer)')
        result_parts.append(f'Q{i + 1}: {q["question"][:200]}')
        result_parts.append(f'A{i + 1}: {answer_text}')
        result_parts.append('')

    tool_content = 'Human responses (batch):\n' + '\n'.join(result_parts)
    logger.info('[Executor] ask_human batch received %d answers: '
                'guidance_id=%s, task=%s',
                len(answers), guidance_id, task.get('id', '?')[:8])

    _FULL_LIMIT = 2000
    def _clip(s):
        if not s:
            return s
        return s if len(s) <= _FULL_LIMIT else s[:_FULL_LIMIT - 1] + '…'

    meta = _build_simple_meta(
        fn_name, tool_content, source='HumanGuidance',
        title=_clip(summary),
        snippet=_clip(user_response or 'No response'),
        badge='answered' if user_response else 'aborted',
        extra={
            'guidanceId': guidance_id,
            'question': summary,
            'responseType': 'batch',
            'batchQuestions': questions,
            'batchAnswers': answers,
            'userResponse': user_response,
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.handler('todo_write', category='task',
                       description='Maintain the structured task checklist')
def _handle_todo_write(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Apply one flat progress snapshot and finalize its receipt.

    Note: NOT a state-changing tool (no file mutation) — so it correctly does
    NOT reset the zero-deliverable streak; the two guards stay orthogonal.
    """
    from lib.tools.todo import apply_todo_write_to_task, public_todo_state

    todos, tool_content, outcome = apply_todo_write_to_task(task, fn_args)
    if project_enabled and project_path:
        try:
            from lib.conversations.project_brain import note_todo_signal
            note_todo_signal(
                task,
                project_path,
                todos,
                accepted=not bool(outcome.get('rejected')),
            )
        except Exception as exc:
            # Coordination derivation is fail-soft; a valid checklist must
            # never be rolled back by an unavailable project projection.
            logger.debug('[ProjectBrain] todo signal skipped: %s', exc)
    state = public_todo_state(outcome['state'])
    total = len(todos)
    done = sum(1 for t in todos if t.get('status') == 'completed')
    in_prog = sum(1 for t in todos if t.get('status') == 'in_progress')
    logger.info('[Executor] todo_write: %d items, %d done, %d in_progress, '
                'updates=%d noop=%s rejected=%s (task=%s)', total, done, in_prog,
                state.get('update_count', 0), outcome.get('no_op'),
                outcome.get('rejected'), task.get('id', '?')[:8])

    badge = ('rejected' if outcome.get('rejected') else
             ('unchanged' if outcome.get('no_op') else
              (f'{done}/{total}' if total else 'cleared')))
    meta = _build_simple_meta(
        fn_name, tool_content, source='Checklist',
        title=f'Checklist · {done}/{total} done' if total else 'Checklist cleared',
        snippet=tool_content[:200],
        badge=badge,
        # Structured payload so the frontend renders a live progress panel
        # off engine data, not by re-parsing the result prose.
        extra={
            'todos': todos,
            'todoState': state,
            'todoNoop': bool(outcome.get('no_op')),
            'todoRejected': bool(outcome.get('rejected')),
            'todoRejectReason': outcome.get('reason') or '',
            'todoUpdateCount': state.get('update_count', 0),
            'todoExplanation': state.get('explanation') or '',
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
