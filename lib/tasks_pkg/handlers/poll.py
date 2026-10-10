"""Unified async-handle handlers: ``poll`` and ``kill``.

``poll`` routes a handle to its owner subsystem — the background command
registry, the conversation's swarm session, or the cross-conversation task
runtime — and ``kill`` stops a handle. Together they absorb the retired
``check_background_command`` / ``await_agents`` / ``await_task`` /
``kill_background_command`` surfaces; those handlers stay registered for
conversation-latched schemas, and name repair routes fresh mistakes here.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.executor import (
    _build_simple_meta,
    _finalize_tool_round,
    tool_registry,
)

logger = get_logger(__name__)

# Mirrors WAIT_BACKGROUND_COMMAND_MAX_S / AWAIT_AGENTS_HARD_CAP_SEC so every
# wait primitive shares one contract: block at most a minute, then report.
POLL_WAIT_MAX_S = 60.0


# ── argument normalization ─────────────────────────────────

def _normalize_ids(fn_args: dict) -> list[str]:
    """Accept ``id`` / ``ids`` plus the legacy per-family key names."""
    raw: list[str] = []
    for key in ('id', 'command_id', 'agent_id', 'task_id'):
        value = fn_args.get(key)
        if isinstance(value, str) and value.strip():
            raw.append(value.strip())
    for key in ('ids', 'agent_ids'):
        values = fn_args.get(key)
        if isinstance(values, list):
            raw.extend(str(v).strip() for v in values if str(v or '').strip())
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _coerce_wait_s(fn_args: dict) -> float:
    # ``timeout_seconds`` is the legacy await_agents spelling.
    value = fn_args.get('wait_s', fn_args.get('timeout_seconds', 0))
    try:
        return min(POLL_WAIT_MAX_S, max(0.0, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


def _user_int(task: dict) -> int | None:
    try:
        return int(task.get('_userId'))
    except (TypeError, ValueError):
        return None


def _swarm_session(task: dict):
    """Live swarm session for this conversation, if any (same resolution as
    the retired await_agents handler, including the cross-turn alias)."""
    try:
        from lib.swarm.integration._config import swarm_key_for
        from lib.swarm.integration._state import _get_session, add_session_alias
        swarm_key = swarm_key_for(task)
        session = _get_session(swarm_key) or _get_session(task['id'])
        if session is not None:
            from lib.swarm.integration._tools import _session_matches_task
            if not _session_matches_task(session, task, task['id']):
                return None
            add_session_alias(task['id'], swarm_key)
        return session
    except Exception as exc:
        logger.debug('[poll] swarm session resolution failed: %s', exc)
        return None


def _owned_task(handle_id: str, task: dict):
    uid = _user_int(task)
    if uid is None:
        return None
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        return chat_task_runtime.get_owned(handle_id, user_id=uid)
    except Exception as exc:
        logger.debug('[poll] task runtime lookup failed: %s', exc)
        return None


def _route(handle_id: str, task: dict, user_id: Any):
    """Route registered handles only: (command | swarm | task | None, payload).

    Durable swarm evidence is poll-only; it cannot grant kill authority.
    """
    from lib.tasks_pkg.background_commands import check_background_command
    snap = check_background_command(handle_id, user_id=user_id, wait_s=0)
    if snap is not None:
        return 'command', snap
    session = _swarm_session(task)
    if session is not None:
        try:
            status = session.get_status() or {}
        except Exception as _e:
            logger.debug('probe swarm status failed: %s', _e)
            status = {}
        if handle_id in status:
            return 'swarm', (session, status[handle_id])
    owned = _owned_task(handle_id, task)
    if owned is not None:
        return 'task', owned
    return None, None


# ── poll ───────────────────────────────────────────────────

def _format_command_section(info: dict, *, waited: bool) -> str:
    command_id = info['command_id']
    if info['status'] == 'completed':
        result_text = info['result'] or '(no output)'
        return (f'== {command_id} (background command) ==\n'
                f'completed (elapsed {info["elapsed_s"]}s):\n\n{result_text}')
    text = (f'== {command_id} (background command) ==\n'
            f'still running (elapsed {info["elapsed_s"]}s). '
            f'Command: {info["command"][:200]}')
    if waited:
        text += (f'\n\nWait cap reached. Use poll(id={command_id!r}, wait_s=60) '
                 'only when no useful work remains, or end the turn — '
                 'completion is delivered automatically.')
    return text


def _dashboard(task: dict, user_id: Any) -> str:
    from lib.tasks_pkg.background_commands import list_background_commands
    lines: list[str] = []
    for cmd in list_background_commands(user_id=user_id):
        conv = f", conv {cmd['conv_id'][:8]}" if cmd.get('conv_id') else ''
        lines.append(f"- {cmd['command_id']} [command, {cmd['status']}, "
                     f"{cmd['elapsed_s']}s{conv}] {cmd['command'][:120]}")
    session = _swarm_session(task)
    if session is not None:
        try:
            status = session.get_status() or {}
        except Exception as _e:
            logger.debug('probe swarm status failed: %s', _e)
            status = {}
        for agent_id, info in status.items():
            if info.get('status') not in ('completed', 'failed', 'cancelled'):
                lines.append(
                    f"- {agent_id} [swarm agent, {info.get('status')}, "
                    f"role={info.get('role')}] {info.get('objective', '')[:120]}")
    uid = _user_int(task)
    if uid is not None:
        try:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            for t in chat_task_runtime.snapshot_owned(user_id=uid):
                if t.get('status') != 'running' or t.get('id') == task.get('id'):
                    continue
                elapsed = round(time.time() - t.get('created_at', time.time()))
                lines.append(
                    f"- {t['id']} [task, running {elapsed}s] "
                    f"conversation {str(t.get('convId') or '?')[:12]}")
        except Exception as exc:
            logger.debug('[poll] task dashboard failed: %s', exc)
    if not lines:
        return 'No background work is currently running.'
    return 'Background handles:\n' + '\n'.join(lines)


def _await_swarm(swarm_ids: list[str], task: dict, mode: str,
                 budget_s: float) -> str:
    from lib.swarm.integration._tools import _handle_await_agents
    return _handle_await_agents(
        {'ids': swarm_ids, 'mode': mode,
         'timeout_seconds': max(1, int(math.ceil(budget_s)))},
        task_id=task['id'], task=task)


def _snapshot_swarm(handle_id: str, task: dict) -> str:
    from lib.swarm.integration._tools import _resolve_one_agent_result
    return json.dumps(
        _resolve_one_agent_result(handle_id, task_id=task['id'], task=task),
        ensure_ascii=False)


def _task_status_text(handle_id: str, task: dict, wait_s: float,
                      deadline: float) -> str:
    from lib.scheduler.executor._await import _execute_await_task
    uid = _user_int(task)
    if uid is None:
        return f'== {handle_id} (task) ==\ncannot resolve owner.'
    if wait_s <= 0:
        return _execute_await_task(
            {'action': 'status', 'task_id': handle_id}, user_id=uid)
    remaining = max(1, int(math.ceil(deadline - time.monotonic())))
    return _execute_await_task(
        {'action': 'wait', 'task_id': handle_id,
         'timeout': min(remaining, int(POLL_WAIT_MAX_S)), 'poll_interval': 2,
         '_parent_task': task},
        user_id=uid)


@tool_registry.handler('poll', category='background',
                       description='Check or wait on any background handle')
def _handle_poll(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                 project_path, project_enabled, all_tools=None):
    user_id = task.get('_userId')
    ids = _normalize_ids(fn_args)
    wait_s = _coerce_wait_s(fn_args)
    mode = str(fn_args.get('mode') or 'any')

    if not ids:
        tool_content = _dashboard(task, user_id)
        meta = _build_simple_meta(
            fn_name, tool_content, source='Poll',
            title='Background handles', snippet=tool_content.split('\n')[0],
            badge='running' if tool_content.startswith('Background') else 'done',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    deadline = time.monotonic() + wait_s
    sections: list[str] = []
    unknown: list[str] = []
    swarm_wait: list[str] = []

    for handle_id in ids:
        kind, payload = _route(handle_id, task, user_id)
        if kind == 'command':
            from lib.tasks_pkg.background_commands import (
                check_background_command)
            remaining = max(0.0, deadline - time.monotonic())
            info = check_background_command(
                handle_id, user_id=user_id, wait_s=remaining)
            sections.append(_format_command_section(
                info or payload, waited=wait_s > 0))
        elif kind == 'swarm':
            if wait_s > 0 and time.monotonic() < deadline:
                swarm_wait.append(handle_id)
            else:
                sections.append(
                    f'== {handle_id} (swarm agent) ==\n'
                    + _snapshot_swarm(handle_id, task))
        elif kind == 'task':
            sections.append(
                f'== {handle_id} (task) ==\n'
                + _task_status_text(handle_id, task, wait_s, deadline))
        else:
            # Reuse the result-read authority, including its foreign-session
            # fence. Recovery never waits/restarts or grants kill authority,
            # and registered command/swarm/task handles never take this path.
            from lib.swarm.integration._tools import _resolve_one_agent_result
            recovered = _resolve_one_agent_result(
                handle_id, task_id=task['id'], task=task)
            if recovered.get('found'):
                sections.append(
                    f'== {handle_id} (recovered swarm evidence) ==\n'
                    + json.dumps(recovered, ensure_ascii=False))
            else:
                unknown.append(handle_id)

    if swarm_wait:
        budget = max(0.0, deadline - time.monotonic())
        sections.append(_await_swarm(swarm_wait, task, mode, budget))

    if unknown:
        sections.append(
            f"Unknown handle(s): {', '.join(unknown)}.\n"
            + _dashboard(task, user_id))

    tool_content = '\n\n'.join(sections)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Poll',
        title=f'poll {len(ids)} handle(s)',
        snippet=tool_content[:200],
        badge='done',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


# ── kill ───────────────────────────────────────────────────

@tool_registry.handler('kill', category='background',
                       description='Stop a background handle')
def _handle_kill(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                 project_path, project_enabled, all_tools=None):
    from lib.tasks_pkg.background_commands import kill_background_command
    user_id = task.get('_userId')
    ids = _normalize_ids(fn_args)
    if not ids:
        tool_content = 'Error: id is required.'
        meta = _build_simple_meta(
            fn_name, tool_content, source='Kill',
            title='Missing id', snippet='No id provided', badge='error',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    sections: list[str] = []
    killed = 0
    for handle_id in ids:
        kind, payload = _route(handle_id, task, user_id)
        if kind == 'command':
            if payload['status'] == 'completed':
                sections.append(f'{handle_id}: already finished.')
            elif kill_background_command(handle_id, user_id=user_id):
                sections.append(
                    f'{handle_id}: kill signalled (background command; '
                    f'process tree receives SIGTERM then SIGKILL).')
                killed += 1
            else:
                sections.append(f'{handle_id}: could not be killed.')
        elif kind == 'swarm':
            session, info = payload
            status = str(info.get('status') or '')
            if status in ('completed', 'failed', 'cancelled'):
                sections.append(f'{handle_id}: already finished ({status}).')
            elif status not in ('running', 'retrying'):
                sections.append(
                    f'{handle_id}: cannot kill — the agent is {status or "pending"}, '
                    'not running. The whole swarm stops with the task\'s Stop.')
            elif session.kill_agent(handle_id):
                sections.append(f'{handle_id}: kill signalled (swarm agent).')
                killed += 1
            else:
                sections.append(f'{handle_id}: could not be killed.')
        elif kind == 'task':
            sections.append(
                f'{handle_id}: cannot kill a task in another conversation '
                'from here — use that conversation\'s Stop control.')
        else:
            sections.append(
                f'{handle_id}: unknown handle. Call poll with no arguments '
                'to list what is running.')

    tool_content = '\n'.join(sections)
    meta = _build_simple_meta(
        fn_name, tool_content, source='Kill',
        title=f'kill {len(ids)} handle(s)',
        snippet=tool_content[:200],
        badge='killed' if killed else 'error',
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
