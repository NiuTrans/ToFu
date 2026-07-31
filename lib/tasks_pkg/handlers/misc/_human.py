# HOT_PATH
"""Human-facing misc handlers: ``ask_human`` (blocking human guidance) and
``todo_write`` (the structured checklist).

MONKEYPATCH PARITY: ``append_event``, ``_build_simple_meta`` and
``_finalize_tool_round`` are resolved THROUGH the package facade
(``lib.tasks_pkg.handlers.misc``) at call time, so a test patching
``misc.append_event`` / ``misc._build_simple_meta`` / ``misc._finalize_tool_round``
steers these handlers exactly as it did before the package split (see
tests/test_tool_audit_tranche1.py).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.executor import tool_registry

logger = get_logger(__name__)


# ── Facade indirection: resolve these collaborators THROUGH the package module
#    (lib.tasks_pkg.handlers.misc) at call time so tests patching the facade
#    steer this handler exactly as before the package split. ──
def _append_event(task, ev):
    from lib.tasks_pkg.handlers import misc as _facade
    return _facade.append_event(task, ev)


def _build_simple_meta(*args, **kwargs):
    from lib.tasks_pkg.handlers import misc as _facade
    return _facade._build_simple_meta(*args, **kwargs)


def _finalize_tool_round(*args, **kwargs):
    from lib.tasks_pkg.handlers import misc as _facade
    return _facade._finalize_tool_round(*args, **kwargs)


@tool_registry.handler('ask_human', category='human_guidance',
                       description='Ask the user a question and wait for their response')
def _handle_ask_human(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle ask_human tool — block indefinitely until user responds."""
    from lib.ids import short_id
    from lib.tasks_pkg.human_guidance import request_human_guidance

    question = fn_args.get('question', '')
    response_type = fn_args.get('response_type', 'free_text')
    options = fn_args.get('options', [])
    # ★ Defensive: models sometimes return options as a JSON string, a dict,
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
    _append_event(task, {
        'type': 'human_guidance_request',
        'roundNum': rn,
        'toolCallId': tc_id,
        'guidanceId': guidance_id,
        'question': question,
        'responseType': response_type,
        'options': options,
    })

    # ── Autopilot: simulated user answers the question instead of blocking ──
    # When autopilot is on, we don't want to wait for a real human; the VU
    # answers using the conversation context.  We still emit the synthetic
    # request → response pair so the frontend renders the autopilot bubble
    # in place of a normal ask_human prompt.
    from lib.tasks_pkg.autopilot import is_autopilot_enabled, run_virtual_user
    if is_autopilot_enabled(task):
        logger.info('[Executor] ask_human → routed to Autopilot VU '
                    '(no human wait): guidance_id=%s, task=%s',
                    guidance_id, task.get('id', '?')[:8])
        # Build a minimal augmented messages list so the VU sees the
        # exact question being asked, not just the last assistant turn.
        _vu_task = task
        try:
            _orig_msgs = list(task.get('messages') or [])
            _augmented = list(_orig_msgs)
            _augmented.append({'role': 'assistant', 'content': question})
            _vu_task = dict(task)
            _vu_task['messages'] = _augmented
        except Exception as _e:
            logger.debug('[Executor] ask_human VU augmentation failed '
                         '(falling back to raw history): %s', _e)
        vu_reply = run_virtual_user(_vu_task)
        if vu_reply is None:
            user_response = None  # task aborted or VU stopped
        else:
            # run_virtual_user returns {'text', 'rounds', 'segments'} — the
            # ANSWER is only ['text'].  Handing the whole dict to
            # resolve_human_guidance stringified rounds/segments metadata
            # into the tool result (and into the human_guidance_response SSE
            # event), and the deliberately-kept [PROGRESS:] machine line in
            # 'text' leaked into model context via this tool-result path
            # (pt_5355329b, sibling of pt_0ae59e94).  Strip machine tokens
            # through the single agent_verdict predicate — unlike the budget
            # guard, NO consumer on this path needs the raw PROGRESS line.
            from lib.agent_verdict import strip_machine_tokens
            user_response = strip_machine_tokens(
                vu_reply.get('text') or '') or '(no further input)'
            # Resolve so the SSE event consumer (frontend) sees a synthetic
            # response, matching the live human-guidance event shape.
            from lib.tasks_pkg.human_guidance import resolve_human_guidance
            resolve_human_guidance(guidance_id, user_response)
            _append_event(task, {
                'type': 'human_guidance_response',
                'roundNum': rn,
                'guidanceId': guidance_id,
                'response': user_response,
                'isVirtualUser': True,
            })
    elif not task.get('_attended'):
        # No interactive session can answer this. Only the three routes/chat.py
        # entry points stamp task['_attended']=True; every headless/autonomous
        # surface (/api/v1/agent/run, scheduler, compat OpenAI/Anthropic,
        # swarm sub-agents) omits it — and autopilot was already handled above.
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
    else:
        logger.info('[Executor] ask_human blocking indefinitely for user '
                    'response: guidance_id=%s, task=%s',
                    guidance_id, task.get('id', '?')[:8])
        user_response = request_human_guidance(guidance_id, task=task)

    if task.get('aborted') or user_response is None:
        tool_content = '[Task was aborted while waiting for human guidance]'
        logger.warning('[Executor] ask_human aborted/cancelled: '
                       'guidance_id=%s, task=%s, aborted=%s',
                       guidance_id, task.get('id', '?')[:8],
                       task.get('aborted', False))
    else:
        tool_content = f'Human response: {user_response}'
        logger.info('[Executor] ask_human received response: '
                    'guidance_id=%s, response_len=%d, task=%s',
                    guidance_id, len(user_response), task.get('id', '?')[:8])

    # ★ No title/snippet truncation: the user specifically flagged that
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


@tool_registry.handler('todo_write', category='task',
                       description='Maintain the structured task checklist')
def _handle_todo_write(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Persist the model's checklist onto ``task['_todos']`` (survives
    compaction — it's on the task dict, not in ``messages``) and feed the
    continuation enforcer.  The list REPLACES the prior state each call.

    Note: NOT a state-changing tool (no file mutation) — so it correctly does
    NOT reset the zero-deliverable streak; the two guards stay orthogonal.
    """
    from lib.tools.todo import apply_todo_write

    todos, tool_content = apply_todo_write(fn_args)
    task['_todos'] = todos

    total = len(todos)
    done = sum(1 for t in todos if t.get('status') == 'completed')
    in_prog = sum(1 for t in todos if t.get('status') == 'in_progress')
    logger.info('[Executor] todo_write: %d item(s) — %d done, %d in_progress '
                '(task=%s)', total, done, in_prog, task.get('id', '?')[:8])

    badge = f'{done}/{total}' if total else 'cleared'
    meta = _build_simple_meta(
        fn_name, tool_content, source='Checklist',
        title=f'Checklist · {done}/{total} done' if total else 'Checklist cleared',
        snippet=tool_content[:200],
        badge=badge,
        # Structured payload so the frontend renders a live progress panel
        # off engine data, not by re-parsing the result prose.
        extra={'todos': todos},
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
