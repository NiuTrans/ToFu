"""Unified async-handle handlers: ``poll`` and ``kill``.

``poll`` routes a handle to its owner subsystem — the background command
registry, the conversation's swarm session, or the cross-conversation task
runtime — and ``kill`` stops a handle. Together they absorb the retired
``check_background_command`` / ``await_agents`` / ``await_task`` /
``kill_background_command`` surfaces; those handlers stay registered for
conversation-latched schemas, and name repair routes fresh mistakes here.

A leaf worker (``lib.swarm.routing.is_leaf_worker``) reaches only the bg_*
commands its own task launched; every other handle is refused with the typed
``background_handle_not_owned`` error before anything is read or waited on.
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

# A blocking poll costs no model request, so it may wait up to the poll tool's
# declared cap, clamped below the stuck-task reaper (reaper_safe_wait_s). It
# wakes on completion and ends early on Stop or on input already waiting for
# this turn's next round boundary: a user steer or another background
# completion. Command waits are sliced so those checks stay prompt.
_WAIT_SLICE_S = 1.0
_PENDING_INPUT_MODES = ['user-steer', 'background-command']
_WAIT_ENDED_EARLY = {
    'cancelled': 'Wait cancelled: the tool call ended; the command remains running.',
    'pending_input': ('Wait ended early: new input is waiting for this turn '
                      '(a user message or another background completion) and '
                      'is delivered right after this tool call. Poll again '
                      'afterwards if this handle still matters.'),
    'aborted': 'Wait cancelled: the task was stopped.',
}


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


def _poll_wait_cap_s() -> float:
    from lib.tasks_pkg.manager import reaper_safe_wait_s
    from lib.tools.poll import POLL_WAIT_CAP_S
    return reaper_safe_wait_s(POLL_WAIT_CAP_S)


def _coerce_wait_s(fn_args: dict) -> float:
    # ``timeout_seconds`` is the legacy await_agents spelling.
    value = fn_args.get('wait_s', fn_args.get('timeout_seconds', 0))
    try:
        requested = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0
    return min(_poll_wait_cap_s(), requested) if requested else 0.0


def _wait_interruption(task: dict, runtime_context=None):
    """Return a predicate naming why a blocking wait must end now, or ''."""
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    conv_id = completion_conversation_id(task)

    def interrupted() -> str:
        if task.get('aborted'):
            return 'aborted'
        if runtime_context is not None and runtime_context.cancellation_requested:
            return 'cancelled'
        if conv_id:
            from lib.agent_inbox import has_pending
            if has_pending(conv_id, modes=_PENDING_INPUT_MODES):
                return 'pending_input'
        return ''
    return interrupted


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


def _own_command_scope(task: dict) -> str | None:
    """The launching task a leaf worker's handles must name, else None (unscoped)."""
    from lib.swarm.routing import is_leaf_worker
    if not is_leaf_worker(task, task.get('_executable_tool_catalog')):
        return None
    return str(task.get('id') or '')


def _refuse_foreign_handles(task: dict, fn_name: str, ids: list[str], user_id: Any,
                            scope: str | None, rn, round_entry) -> str | None:
    """Settle a typed refusal when a leaf worker names any handle it did not launch.

    The whole call is refused before any handle is read, waited on or signalled.
    """
    if scope is None:
        return None
    refused = [handle_id for handle_id in ids
               if _route(handle_id, task, user_id, scope)[0] is None]
    if not refused:
        return None
    from lib.tools.result_envelope import typed_tool_error
    if fn_name == 'kill':
        authority = ('stop only background commands (bg_* handles) it started '
                     'itself; stopping other work is its coordinator\'s authority. '
                     'Nothing was stopped.')
    else:
        authority = ('poll only background commands (bg_* handles) it started '
                     'itself; waiting on another agent, task or command could '
                     'deadlock its coordinator. Nothing was read or waited on.')
    tool_content = typed_tool_error(
        'background_handle_not_owned', retryable=False,
        message=(f'{fn_name} refused {", ".join(refused)}: a delegated worker '
                 f'can {authority}'),
        next_action=('Pass only bg_* ids from your own run_command(background=true) '
                     'receipts; poll with no id lists them. Report any other handle '
                     'in your final result for your coordinator.'),
    ).to_model_text()
    meta = _build_simple_meta(
        fn_name, tool_content, source='Kill' if fn_name == 'kill' else 'Poll',
        title='Handle not owned', snippet=tool_content[:200], badge='error')
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tool_content


def _route(handle_id: str, task: dict, user_id: Any, scope: str | None = None):
    """Route registered handles only: (command | swarm | task | None, payload).

    Durable swarm evidence is poll-only; it cannot grant kill authority. A
    leaf worker's *scope* admits only commands its own task launched.
    """
    from lib.tasks_pkg.background_commands import check_background_command
    if scope is not None:
        snap = (check_background_command(handle_id, user_id=user_id, wait_s=0,
                                         task_id=scope) if scope else None)
        return ('command', snap) if snap is not None else (None, None)
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

def _await_command(handle_id: str, user_id: Any, deadline: float,
                   interrupted, scope: str | None = None) -> tuple[dict | None, str]:
    from lib.tasks_pkg.background_commands import check_background_command
    while True:
        remaining = deadline - time.monotonic()
        info = check_background_command(
            handle_id, user_id=user_id,
            wait_s=min(_WAIT_SLICE_S, max(0.0, remaining)), task_id=scope)
        if info is None or info['status'] == 'completed' or remaining <= 0:
            return info, ''
        reason = interrupted()
        if reason:
            return info, reason


def _format_command_section(info: dict, *, waited: bool,
                            automatic_delivery: bool, ended_by: str = '') -> str:
    command_id = info['command_id']
    if info['status'] == 'completed':
        result_text = info['result'] or '(no output)'
        return (f'== {command_id} (background command) ==\n'
                f'completed (elapsed {info["elapsed_s"]}s):\n\n{result_text}')
    admission = info.get('admission')
    state = (f'not started (elapsed {info["elapsed_s"]}s): {admission}.' if admission
             else f'still running (elapsed {info["elapsed_s"]}s).')
    text = (f'== {command_id} (background command) ==\n'
            f'{state} Command: {info["command"][:200]}')
    if ended_by:
        text += '\n\n' + _WAIT_ENDED_EARLY[ended_by]
    elif waited:
        from lib.tools.background_guidance import background_wait_cap_advice
        text += '\n\nWait cap reached. ' + background_wait_cap_advice(
            command_id, automatic_delivery=automatic_delivery)
    return text


def _dashboard(task: dict, user_id: Any, scope: str | None = None) -> str:
    from lib.tasks_pkg.background_commands import list_background_commands
    lines: list[str] = []
    for cmd in list_background_commands(user_id=user_id, task_id=scope):
        conv = f", conv {cmd['conv_id'][:8]}" if cmd.get('conv_id') else ''
        lines.append(f"- {cmd['command_id']} [command, {cmd['status']}, "
                     f"{cmd['elapsed_s']}s{conv}] {cmd['command'][:120]}")
    if scope is not None:
        return ('Background handles:\n' + '\n'.join(lines) if lines
                else 'No background work is currently running.')
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
                 budget_s: float, cap_s: float, interrupted) -> str:
    from lib.swarm.integration._tools import _handle_await_agents
    # Stop ends this wait through the yield predicate, never as an abort_check:
    # the swarm session outlives this turn and keeps its own Stop control.
    result = _handle_await_agents(
        {'ids': swarm_ids, 'mode': mode,
         'timeout_seconds': max(1, int(math.ceil(budget_s)))},
        task_id=task['id'], task=task,
        pending_input_check=lambda: bool(interrupted()),
        max_wait_s=cap_s)
    if interrupted() == 'aborted':
        return _WAIT_ENDED_EARLY['aborted'] + '\n' + result
    return result


def _snapshot_swarm(handle_id: str, task: dict) -> str:
    from lib.swarm.integration._tools import _resolve_one_agent_result
    return json.dumps(
        _resolve_one_agent_result(handle_id, task_id=task['id'], task=task),
        ensure_ascii=False)


def _task_status_text(handle_id: str, task: dict, wait_s: float,
                      deadline: float, cap_s: float, interrupted) -> str:
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
         'timeout': remaining, 'poll_interval': 2, '_parent_task': task},
        user_id=uid, max_wait_s=cap_s, interrupted=interrupted)


@tool_registry.handler('poll', category='background',
                       description='Check or wait on any background handle')
def _handle_poll(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                 project_path, project_enabled, all_tools=None):
    from lib.tasks_pkg.tool_runtime import active_context_for_call
    runtime_context = active_context_for_call(
        task, round_num=rn, tool_call_id=tc_id, round_entry=round_entry)
    user_id = task.get('_userId')
    ids = _normalize_ids(fn_args)
    wait_s = _coerce_wait_s(fn_args)
    mode = str(fn_args.get('mode') or 'any')
    scope = _own_command_scope(task)

    if not ids:
        tool_content = _dashboard(task, user_id, scope)
        meta = _build_simple_meta(
            fn_name, tool_content, source='Poll',
            title='Background handles', snippet=tool_content.split('\n')[0],
            badge='running' if tool_content.startswith('Background') else 'done',
        )
        _finalize_tool_round(task, rn, round_entry, [meta])
        return tc_id, tool_content, False

    refusal = _refuse_foreign_handles(task, fn_name, ids, user_id, scope, rn, round_entry)
    if refusal is not None:
        return tc_id, refusal, False

    deadline = time.monotonic() + wait_s
    interrupted = _wait_interruption(task, runtime_context)
    sections: list[str] = []
    unknown: list[str] = []
    swarm_wait: list[str] = []

    for handle_id in ids:
        kind, payload = _route(handle_id, task, user_id, scope)
        if kind == 'command':
            info, ended_by = _await_command(
                handle_id, user_id, deadline, interrupted, scope)
            from lib.tasks_pkg.conversation_delivery import completion_reaches
            sections.append(_format_command_section(
                info or payload, waited=wait_s > 0,
                automatic_delivery=completion_reaches(task, info or payload),
                ended_by=ended_by))
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
                + _task_status_text(handle_id, task, wait_s, deadline,
                                    _poll_wait_cap_s(), interrupted))
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
        sections.append(_await_swarm(swarm_wait, task, mode, budget,
                                     _poll_wait_cap_s(), interrupted))

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

    scope = _own_command_scope(task)
    refusal = _refuse_foreign_handles(task, fn_name, ids, user_id, scope, rn, round_entry)
    if refusal is not None:
        return tc_id, refusal, False

    sections: list[str] = []
    killed = 0
    for handle_id in ids:
        kind, payload = _route(handle_id, task, user_id, scope)
        if kind == 'command':
            if payload['status'] == 'completed':
                sections.append(f'{handle_id}: already finished.')
            elif kill_background_command(handle_id, user_id=user_id, task_id=scope):
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
