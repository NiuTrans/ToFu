# HOT_PATH
"""Miscellaneous tool handlers: ask_human, scheduler, desktop, swarm, conv_ref, error_tracker, emit_to_user."""

from __future__ import annotations

import os
import re

from lib.conv_ref import execute_conv_ref_tool
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler import SCHEDULER_TOOL_NAMES, execute_scheduler_tool
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event
from lib.tools import CONV_REF_TOOL_NAMES, EMIT_TO_USER_TOOL_NAMES, ERROR_TRACKER_TOOL_NAMES

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


# ═══ Error tracker handler ══════════════════════════════════════════

@tool_registry.tool_set(ERROR_TRACKER_TOOL_NAMES, category='error_tracker',
                        description='Inspect error logs and manage bug resolution status')
def _handle_error_tracker_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    import time as _time

    t0 = _time.time()
    try:
        if fn_name == 'check_error_logs':
            tool_content = _exec_check_error_logs(fn_args, project_path)
        elif fn_name == 'resolve_error':
            tool_content = _exec_resolve_error(fn_args, project_path)
        else:
            tool_content = f'Unknown error_tracker tool: {fn_name}'
    except Exception as e:
        logger.error('[Tool:%s] failed: %s', fn_name, e, exc_info=True)
        tool_content = f'❌ {fn_name} failed: {e}'

    elapsed = _time.time() - t0
    logger.info('[Tool:%s] returned %d chars in %.1fs', fn_name, len(tool_content), elapsed)

    icon = '🔍' if fn_name == 'check_error_logs' else '✅'
    meta = _build_simple_meta(
        fn_name, tool_content, source='ErrorTracker', icon=icon,
        badge=f'{icon} {elapsed:.1f}s',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


# ═══ emit_to_user handler (terminal — breaks orchestrator loop) ═══════

@tool_registry.tool_set(EMIT_TO_USER_TOOL_NAMES, category='emit',
                        description='End turn by referencing an existing tool result')
def _handle_emit_to_user(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    """Handle emit_to_user — reference an existing tool result for the user."""
    tool_round = fn_args.get('tool_round')
    comment = fn_args.get('comment', '')

    if tool_round is None:
        error_msg = 'Error: tool_round parameter is required for emit_to_user.'
        logger.warning('[Tool:emit_to_user] Missing tool_round param, task=%s', task.get('id', '?')[:8])
        meta = _build_simple_meta(
            fn_name, error_msg, source='Emit',
            title='❌ emit_to_user: missing tool_round',
            badge='❌ error',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, error_msg, False

    ref_found = False
    ref_tool_name = None
    for sr in task.get('searchRounds', []):
        if sr.get('roundNum') == tool_round:
            ref_found = True
            ref_tool_name = sr.get('toolName', '?')
            break

    if not ref_found:
        error_msg = (
            f'Error: tool_round={tool_round} not found in searchRounds. '
            f'Available rounds: {[sr.get("roundNum") for sr in task.get("searchRounds", [])]}.'
        )
        logger.warning('[Tool:emit_to_user] tool_round=%d not found, task=%s, available=%s',
                       tool_round, task.get('id', '?')[:8],
                       [sr.get('roundNum') for sr in task.get('searchRounds', [])])
        meta = _build_simple_meta(
            fn_name, error_msg, source='Emit',
            title=f'❌ emit_to_user: round {tool_round} not found',
            badge='❌ not found',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, error_msg, False

    logger.info('[Tool:emit_to_user] Terminal emit: tool_round=%d (%s), comment=%.200s, task=%s',
                tool_round, ref_tool_name, comment, task.get('id', '?')[:8])

    round_entry['_emit_to_user'] = True
    round_entry['_emit_tool_round'] = tool_round
    round_entry['_emit_comment'] = comment

    meta = _build_simple_meta(
        fn_name, comment, source='Emit',
        title=f'📤 Referencing result from round {tool_round} ({ref_tool_name})',
        snippet=comment[:120],
        badge=f'📤 round {tool_round}',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, comment, False


# ═══ Error tracker implementation helpers ═══════════════════════════

def _exec_check_error_logs(fn_args: dict, project_path: str = None) -> str:
    """Execute check_error_logs tool — inspect error logs."""
    import lib.project_error_tracker as pet

    project_path = project_path or _APP_ROOT

    mode = fn_args.get('mode', 'unresolved')
    n = min(fn_args.get('n', 2000), 10000)
    logger_filter = fn_args.get('logger_filter', '').strip()
    project_name = os.path.basename(project_path)

    if mode == 'discover':
        return pet.get_log_summary(project_path)

    if mode == 'stats':
        stats = pet.error_stats(project_path, n=n)
        lines = [
            f'# Error Log Statistics — {project_name}',
            f'Log files scanned: {stats["log_files_scanned"]}',
            f'Total errors scanned: {stats["total"]}',
            f'Resolved: {stats["resolved_count"]} ({stats["resolution_rate"]}%)',
            f'Unresolved: {stats["unresolved_count"]}',
            f'Unique error patterns: {stats["unique_fingerprints"]}',
            f'  - Resolved patterns: {stats["unique_resolved"]}',
            f'  - Unresolved patterns: {stats["unique_unresolved"]}',
        ]
        if stats['top_unresolved']:
            lines.append('\n## Top Unresolved Patterns')
            for i, g in enumerate(stats['top_unresolved'], 1):
                lines.append(
                    f'{i}. [{g["fingerprint"]}] ({g["count"]}x) '
                    f'{g["logger"]}: {g["sample_message"]}'
                )
        return '\n'.join(lines)

    elif mode == 'all_resolutions':
        resolutions = pet.get_project_resolutions(project_path)
        if not resolutions:
            return f'No resolved error patterns found for {project_name}.'
        lines = [f'# Resolved Error Patterns — {project_name} ({len(resolutions)} total)\n']
        for fp, r in list(resolutions.items())[:50]:
            lines.append(
                f'- [{fp}] {r.get("logger_name", "")}: '
                f'{r.get("sample_message", "")[:150]}\n'
                f'  Resolved by: {r.get("resolved_by") or "unknown"}'
                + (f', ticket: {r["ticket"]}' if r.get("ticket") else '')
                + (f'\n  Notes: {r["notes"][:200]}' if r.get("notes") else '')
            )
        return '\n'.join(lines)

    elif mode == 'recent':
        errors = pet.scan_project_errors(project_path, n=n, error_only=False)
        resolutions = pet.get_project_resolutions(project_path)
        if logger_filter:
            errors = [e for e in errors if e.get('logger', '').startswith(logger_filter)]
        if not errors:
            return 'No recent errors found' + (f' for logger {logger_filter}' if logger_filter else '') + f' in {project_name}.'
        lines = [f'# Recent Errors — {project_name} ({len(errors)} entries)\n']
        for e in errors[:50]:
            fp = pet.compute_fingerprint(e.get('logger', ''), e.get('message', ''))
            resolved = fp in resolutions
            status = '✅ RESOLVED' if resolved else '❌ UNRESOLVED'
            source = f' ({e["source_file"]})' if e.get('source_file') else ''
            lines.append(
                f'[{e.get("timestamp", "?")}] {e.get("level", "?")} '
                f'{e.get("logger", "?")}{source} [{fp}] {status}\n'
                f'  {e.get("message", "")[:300]}'
            )
            if resolved:
                r = resolutions[fp]
                lines.append(
                    f'  → Fixed by {r.get("resolved_by", "?")}'
                    + (f', ticket: {r["ticket"]}' if r.get("ticket") else '')
                )
            lines.append('')
        return '\n'.join(lines)

    else:  # mode == 'unresolved' (default)
        grouped = pet.get_unresolved_grouped(project_path, n=n)
        if logger_filter:
            grouped = [g for g in grouped
                       if g.get('logger', '').startswith(logger_filter)]
        if not grouped:
            return '🎉 No unresolved errors found' + (f' for logger {logger_filter}' if logger_filter else '') + f' in {project_name}! All clear.'
        total_occ = sum(g['count'] for g in grouped)
        lines = [
            f'# Unresolved Error Patterns — {project_name} ({len(grouped)} unique, {total_occ} total occurrences)\n',
            'Each entry shows: [fingerprint] (count×) logger: sample_message\n',
            'Use resolve_error with the fingerprint to mark as fixed after applying a fix.\n',
        ]
        for i, g in enumerate(grouped, 1):
            source = f' in {g["source_file"]}' if g.get('source_file') else ''
            lines.append(
                f'{i}. **[{g["fingerprint"]}]** ({g["count"]}×) '
                f'{g["level"]} {g["logger"]}{source}\n'
                f'   Last seen: {g["last_seen"]}\n'
                f'   Message: {g["sample_message"]}'
            )
        return '\n'.join(lines)


def _exec_resolve_error(fn_args: dict, project_path: str = None) -> str:
    """Execute resolve_error tool — mark error fingerprints as fixed."""
    import lib.project_error_tracker as pet

    project_path = project_path or _APP_ROOT

    notes = fn_args.get('notes', '')
    ticket = fn_args.get('ticket', '')
    resolved_by = 'ai_assistant'
    results = []

    fp = fn_args.get('fingerprint', '').strip()
    if fp:
        pet.mark_resolved(project_path, fp, resolved_by=resolved_by,
                          ticket=ticket, notes=notes)
        results.append(f'✅ Resolved fingerprint: {fp}')

    fps = fn_args.get('fingerprints', [])
    if fps:
        for f in fps:
            f = f.strip()
            if f:
                pet.mark_resolved(project_path, f, resolved_by=resolved_by,
                                  ticket=ticket, notes=notes)
                results.append(f'✅ Resolved fingerprint: {f}')

    logger_name = fn_args.get('logger_name', '').strip()
    if logger_name:
        count = pet.resolve_by_logger(project_path, logger_name,
                                      resolved_by=resolved_by,
                                      ticket=ticket, notes=notes)
        results.append(f'✅ Resolved {count} fingerprints from logger: {logger_name}')

    msg_pattern = fn_args.get('message_pattern', '').strip()
    if msg_pattern:
        count = pet.resolve_by_message_pattern(project_path, msg_pattern,
                                               resolved_by=resolved_by,
                                               ticket=ticket, notes=notes)
        results.append(f'✅ Resolved {count} fingerprints matching pattern: {msg_pattern}')

    if not results:
        return ('❌ No resolution target specified. Provide at least one of: '
                'fingerprint, fingerprints, logger_name, or message_pattern.')

    return '\n'.join(results)
