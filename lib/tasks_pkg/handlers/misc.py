# HOT_PATH
"""Miscellaneous tool handlers: ask_human, scheduler, desktop, swarm, conv_ref, emit_to_user."""

from __future__ import annotations

import os

from lib.conv_ref import execute_conv_ref_tool
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler import SCHEDULER_TOOL_NAMES, execute_scheduler_tool
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event
from lib.tools import CONV_REF_TOOL_NAMES, EMIT_TO_USER_TOOL_NAMES

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

    if not question:
        logger.warning('[Executor] ask_human called with empty question, task=%s',
                       task.get('id', '?')[:8])
        tool_content = 'Error: question parameter is required.'
        meta = _build_simple_meta(
            fn_name, tool_content, source='HumanGuidance',
            title='❌ Missing question', snippet='No question provided',
            badge='❌ error',
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
        'guidanceId': guidance_id,
        'question': question,
        'responseType': response_type,
        'options': options,
    })

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

    meta = _build_simple_meta(
        fn_name, tool_content, source='HumanGuidance',
        title=f'🙋 {question[:80]}',
        snippet=(user_response or 'No response')[:120],
        badge='✅ answered' if user_response else '⛔ aborted',
        extra={
            'guidanceId': guidance_id,
            'question': question,
            'responseType': response_type,
            'userResponse': user_response,
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(SCHEDULER_TOOL_NAMES, category='scheduler',
                        description='Schedule reminders and recurring tasks')
def _handle_scheduler_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    import time as _time
    tid = task.get('id', '?')[:8]
    _log_args = {k: v for k, v in fn_args.items() if not k.startswith('_')}
    logger.info('[Task %s] [Scheduler] %s called with args=%s', tid, fn_name, str(_log_args)[:300])
    t0 = _time.time()
    fn_args['_source_conv_id'] = task.get('convId', '')
    fn_args['_source_task_id'] = task.get('id', '')
    tool_content = execute_scheduler_tool(fn_name, fn_args)
    elapsed = _time.time() - t0
    logger.info('[Task %s] [Scheduler] %s completed in %.1fs (result_len=%d)',
                tid, fn_name, elapsed, len(str(tool_content)))
    meta = _build_simple_meta(fn_name, tool_content, source='Scheduler', icon='⏰')
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(DESKTOP_TOOL_NAMES, category='desktop',
                        description='Interact with the desktop agent')
def _handle_desktop_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from routes.desktop import format_desktop_result, send_desktop_command
    cmd_type = fn_name.replace('desktop_', '', 1)
    result, error = send_desktop_command(cmd_type, fn_args, timeout=30)
    if error:
        tool_content = f'❌ Desktop Agent Error: {error}'
    else:
        tool_content = format_desktop_result(cmd_type, result)
    meta = _build_simple_meta(fn_name, tool_content, source='Desktop Agent', icon='🖥️')
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


# Module-level constant — swarm tool icon dispatch.
_SWARM_ICON_MAP = {
    'spawn_agents': '🐝', 'spawn_more_agents': '🐝➕',
    'check_agents': '📊', 'swarm_done': '✅',
    'store_artifact': '📦', 'read_artifact': '📖', 'list_artifacts': '📋',
}


@tool_registry.tool_set(SWARM_TOOL_NAMES, category='swarm',
                        description='Spawn and manage parallel sub-agents')
def _handle_swarm_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.swarm.integration import execute_swarm_tool
    tool_content = execute_swarm_tool(
        fn_name, fn_args, task,
        on_event=lambda ev: append_event(task, ev),
        project_path=project_path,
        project_enabled=project_enabled,
        model=cfg.get('model'),
        thinking_enabled=cfg.get('thinking_enabled', False),
        search_mode=cfg.get('search_mode', 'multi'),
        cfg=cfg,
        all_tools=all_tools or [],
    )
    icon = _SWARM_ICON_MAP.get(fn_name, '🐝')
    badge = icon
    if fn_name in ('spawn_agents', 'spawn_more_agents'):
        num_agents = len(fn_args.get('agents', []))
        badge = f'{icon} {num_agents} agents'
    meta = _build_simple_meta(fn_name, tool_content, source='Swarm', icon=icon, badge=badge)
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


@tool_registry.tool_set(CONV_REF_TOOL_NAMES, category='conversations',
                        description='List and retrieve past conversations')
def _handle_conv_ref_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    current_conv_id = task.get('convId')
    tool_content = execute_conv_ref_tool(fn_name, fn_args, current_conv_id=current_conv_id)
    icon = '📋' if fn_name == 'list_conversations' else '💬'
    detail = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]
    meta = _build_simple_meta(
        fn_name, tool_content, source='Conversations', icon=icon,
        title=f'{icon} {fn_name}: {detail}',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False




# ═══ emit_to_user handler (terminal — breaks orchestrator loop) ═══════

@tool_registry.tool_set(EMIT_TO_USER_TOOL_NAMES, category='emit',
                        description='End turn by referencing an existing tool result')
def _handle_emit_to_user(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle emit_to_user — reference the most recent tool result for the user.

    Auto-infers the last tool round from toolRounds. The model only needs
    to provide a comment.
    """
    comment = fn_args.get('comment', '')

    # Auto-infer: find the last completed tool round (exclude this emit round itself)
    tool_rounds = task.get('toolRounds', [])
    ref_round = None
    ref_tool_name = None
    for sr in reversed(tool_rounds):
        if sr.get('roundNum') != rn and sr.get('toolName') != 'emit_to_user':
            ref_round = sr
            ref_tool_name = sr.get('toolName', '?')
            break

    if ref_round is None:
        error_msg = 'Error: no prior tool round found to reference.'
        logger.warning('[Tool:emit_to_user] No prior tool round found, task=%s', task.get('id', '?')[:8])
        meta = _build_simple_meta(
            fn_name, error_msg, source='Emit',
            title='❌ emit_to_user: no prior tool round',
            badge='❌ error',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, error_msg, False

    tool_round = ref_round.get('roundNum')

    logger.info('[Tool:emit_to_user] Terminal emit: tool_round=%d (%s), comment=%.200s, task=%s',
                tool_round, ref_tool_name, comment, task.get('id', '?')[:8])

    round_entry['_emit_to_user'] = True
    round_entry['_emit_tool_round'] = tool_round
    round_entry['_emit_comment'] = comment

    meta = _build_simple_meta(
        fn_name, comment, source='Emit',
        title=f'📤 Emit: {ref_tool_name}',
        snippet=comment[:120],
        badge=f'📤 {ref_tool_name}',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, comment, False

