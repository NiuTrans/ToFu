# HOT_PATH
"""Miscellaneous tool handlers: ask_human, scheduler, desktop, swarm, conv_ref."""

from __future__ import annotations

import os

from lib.conv_ref import execute_conv_ref_tool
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler import SCHEDULER_TOOL_NAMES, execute_scheduler_tool
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tasks_pkg.manager import append_event
from lib.tools import CONV_REF_TOOL_NAMES

logger = get_logger(__name__)


# ── Shared constant: application root ──
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@tool_registry.handler('ask_human', category='human_guidance',
                       description='Ask the user a question and wait for their response')
def _handle_ask_human(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle ask_human tool — block indefinitely until user responds."""
    import uuid as _uuid

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

    guidance_id = f'hg_{_uuid.uuid4().hex[:12]}'
    logger.info('[Executor] ask_human: question=%.200s, type=%s, '
                'options=%d, guidance_id=%s, task=%s',
                question, response_type, len(options), guidance_id,
                task.get('id', '?')[:8])

    round_entry['status'] = 'awaiting_human'
    round_entry['guidanceId'] = guidance_id
    round_entry['guidanceQuestion'] = question
    round_entry['guidanceType'] = response_type
    round_entry['guidanceOptions'] = options
    append_event(task, {
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
            user_response = vu_reply or '(no further input)'
            # Resolve so the SSE event consumer (frontend) sees a synthetic
            # response, matching the live human-guidance event shape.
            from lib.tasks_pkg.human_guidance import resolve_human_guidance
            resolve_human_guidance(guidance_id, user_response)
            append_event(task, {
                'type': 'human_guidance_response',
                'roundNum': rn,
                'guidanceId': guidance_id,
                'response': user_response,
                'isVirtualUser': True,
            })
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


# ═══ Adapter-based handlers (simple_call does log→time→exec→meta→finalize) ═══

@tool_registry.tool_set(SCHEDULER_TOOL_NAMES, category='scheduler',
                        description='Schedule reminders and recurring tasks')
def _handle_scheduler_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    fn_args['_source_conv_id'] = task.get('convId', '')
    fn_args['_source_task_id'] = task.get('id', '')
    fn_args['_tool_call_id'] = tc_id
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=execute_scheduler_tool,
        source='Scheduler', module_tag='Scheduler',
    )


def _run_desktop(fn_name, fn_args):
    """Desktop tool executor — wraps send_desktop_command + format_desktop_result."""
    from lib.desktop import format_desktop_result, send_desktop_command
    cmd_type = fn_name.replace('desktop_', '', 1)
    result, error = send_desktop_command(cmd_type, fn_args, timeout=30)
    if error:
        return f'Desktop Agent Error: {error}'
    return format_desktop_result(cmd_type, result)


@tool_registry.tool_set(DESKTOP_TOOL_NAMES, category='desktop',
                        description='Interact with the desktop agent')
def _handle_desktop_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_desktop,
        source='Desktop Agent', module_tag='Desktop',
    )


# Swarm tool badge labels (text only — the frontend renders the SVG icon, so
# no emoji prefix here per CLAUDE.md §3.4; an emoji would duplicate the icon).
_SWARM_BADGE_VERB = {
    'spawn_agents':     'spawn',
    'await_agents':     'await',
    'get_agent_result': 'result',
    'store_artifact':   'store',
    'read_artifact':    'read',
    'list_artifacts':   'list',
}


@tool_registry.tool_set(SWARM_TOOL_NAMES, category='swarm',
                        description='Spawn and manage parallel sub-agents (async)')
def _handle_swarm_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # Custom executor closes over task/cfg/all_tools to preserve the full swarm API
    def _run_swarm(_fn_name, _fn_args):
        from lib.swarm.integration import execute_swarm_tool
        return execute_swarm_tool(
            _fn_name, _fn_args, task,
            on_event=lambda ev: append_event(task, ev),
            project_path=project_path,
            project_enabled=project_enabled,
            model=cfg.get('model'),
            thinking_enabled=cfg.get('thinking_enabled', False),
            search_mode=cfg.get('search_mode', 'multi'),
            cfg=cfg,
            all_tools=all_tools or [],
        )

    verb = _SWARM_BADGE_VERB.get(fn_name, 'swarm')
    badge = verb
    if fn_name == 'spawn_agents':
        num_agents = len(fn_args.get('agents', []))
        badge = f'{num_agents} agents'
    elif fn_name == 'await_agents':
        ids = fn_args.get('ids') or []
        badge = f'await {len(ids) or "all"}'
    elif fn_name == 'get_agent_result':
        badge = f'{(fn_args.get("agent_id") or "?")[:8]}'

    post_build = _build_await_post_build() if fn_name == 'await_agents' else None

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_swarm,
        source='Swarm', badge=badge, module_tag='Swarm',
        post_build=post_build,
    )


def _build_await_post_build():
    """Return a post_build hook that rewrites the await_agents result badge
    from its JSON payload so the UI shows the real outcome.

    Without this every await row gets a generic ``await all`` badge that
    looks identical whether the wait completed cleanly or hit the hard-cap
    timeout. The hook surfaces ``done N/M`` plus a ``timed out`` marker so the
    user has full visibility of partial completions.

    No emoji prefix — the frontend renders the SVG icon and colors the badge
    via ``meta.awaitTimedOut`` (amber) per CLAUDE.md §3.4.
    """
    import json as _json

    def _post_build(meta, tool_content, _fn_args):
        try:
            data = _json.loads(tool_content) if isinstance(tool_content, str) else tool_content
        except (ValueError, TypeError) as e:
            logger.debug('[Swarm] await_agents result not JSON, keeping default badge: %s', e)
            return
        if not isinstance(data, dict):
            return
        if data.get('status') == 'error':
            meta['badge'] = 'no swarm'
            return
        completed = data.get('completed') or []
        still_running = data.get('still_running') or []
        n_done = len(completed)
        n_total = n_done + len(still_running)
        timed_out = bool(data.get('timed_out'))
        if timed_out:
            # Amber warning badge (via awaitTimedOut) — partial result, wait cut short.
            meta['badge'] = f'timed out · {n_done}/{n_total} done'
            meta['awaitTimedOut'] = True
        elif n_total:
            meta['badge'] = f'{n_done}/{n_total} done'
        else:
            # Nothing was waited on (all already finished, or swarm idle).
            meta['badge'] = 'done'
        if still_running:
            meta['awaitStillRunning'] = still_running

    return _post_build


@tool_registry.tool_set(CONV_REF_TOOL_NAMES, category='conversations',
                        description='List and retrieve past conversations')
def _handle_conv_ref_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId')

    def _run(_fn_name, _fn_args):
        return execute_conv_ref_tool(
            _fn_name, _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path,
        )

    detail = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run,
        source='Conversations', module_tag='ConvRef',
        title=f'{fn_name}: {detail}',
    )



