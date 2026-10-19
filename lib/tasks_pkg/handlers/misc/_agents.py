# HOT_PATH
"""Automation handlers for scheduler, desktop and swarm tool families."""

from __future__ import annotations

from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler.executor import execute_scheduler_tool
from lib.scheduler.tool_defs import SCHEDULER_TOOL_NAMES
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _scheduler_post_build(meta, tool_content, _fn_args):
    receipt = getattr(tool_content, 'timer_receipt', None)
    if isinstance(receipt, dict):
        meta['timerReceipt'] = dict(receipt)


@tool_registry.tool_set(SCHEDULER_TOOL_NAMES, category='scheduler',
                        description='Schedule reminders and recurring tasks')
def _handle_scheduler_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.tasks_pkg.manager._registry import task_user_id

    fn_args['_source_conv_id'] = task.get('convId', '')
    fn_args['_source_task_id'] = task.get('id', '')
    fn_args['_user_id'] = task_user_id(task)
    fn_args['_tool_call_id'] = tc_id
    if fn_name == 'timer_create':
        # Creation returns immediately, but the completed tool round keeps one
        # durable hand-off marker that links to the background watcher.
        fn_args['_parent_task'] = task
        fn_args['_tool_round_num'] = rn
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=execute_scheduler_tool,
        source='Scheduler', module_tag='Scheduler',
        post_build=_scheduler_post_build,
    )


def _run_desktop(fn_name, fn_args, user_id='', cancellation=None):
    """Desktop tool executor — wraps send_desktop_command + format_desktop_result.

    The wire cmd_type is the full tool name verbatim: the agent's dispatch
    table (lib/desktop_agent/_dispatch.COMMANDS) is keyed by it, prefix and
    all — never strip or rewrite it here. ``user_id`` (RWA P4a) scopes the
    command to the caller's own agents on relay deployments.
    """
    from lib.desktop import format_desktop_result, send_desktop_command
    wait_options = {} if cancellation is None else {'cancellation': cancellation}
    result, error = send_desktop_command(
        fn_name, fn_args, timeout=None, user_id=user_id, **wait_options)
    if error:
        return f'Desktop Agent Error: {error}'
    return format_desktop_result(fn_name, result)


def _desktop_post_build(meta, tool_content, _fn_args):
    """Screenshot results arrive as a __screenshot__ dict; keep the UI snippet
    human-readable instead of str()-ing the base64 carrier."""
    if isinstance(tool_content, dict) and tool_content.get('__screenshot__'):
        meta['snippet'] = (
            tool_content.get('_text_fallback') or 'Screenshot captured')[:120]


@tool_registry.tool_set(DESKTOP_TOOL_NAMES, category='desktop',
                        description='Interact with the desktop agent')
def _handle_desktop_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # Closure (same pattern as _run_swarm) so the task's bridge user rides
    # into the command — RWA P4a user scoping on relay deployments.
    from lib.project_mod.tool_admission import Cancellation

    def _exec_desktop(_fn_name, _fn_args):
        return _run_desktop(
            _fn_name, _fn_args, user_id=task.get('_userId', '') or '',
            cancellation=Cancellation(task))
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_exec_desktop,
        source='Desktop Agent', module_tag='Desktop',
        post_build=_desktop_post_build,
    )


@tool_registry.tool_set({'set_approval_delegate'}, category='swarm',
                        description='Register a task-scoped approval-delegation policy')
def _handle_set_approval_delegate(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.tasks_pkg.approval_delegate import register_approval_delegate

    def _exec(_fn_name, _fn_args):
        return register_approval_delegate(task, _fn_args)

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_exec,
        source='Approval Delegate', badge='policy', module_tag='ApprovalDelegate',
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
        from lib.agent_inbox import has_pending
        conv_id = task.get('convId', '')
        return execute_swarm_tool(
            _fn_name, _fn_args, task,
            abort_check=lambda: bool(task.get('aborted')),
            pending_input_check=(
                lambda: has_pending(conv_id, modes=['user-steer', 'background-command'])
            ) if conv_id else None,
            on_event=lambda ev: append_event(task, ev),
            project_path=project_path,
            project_enabled=project_enabled,
            model=cfg.get('model'),
            thinking_enabled=cfg.get('thinking_enabled'),
            search_mode=cfg.get('search_mode', 'multi'),
            cfg=cfg,
            # Wire visibility is a prompt projection. Delegation starts from
            # the same task-owned authority used by direct/gateway execution;
            # an explicit empty catalog must remain empty.
            all_tools=(task['_executable_tool_catalog']
                       if '_executable_tool_catalog' in task else all_tools or []),
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
        agent_ids = fn_args.get('agent_ids')
        if isinstance(agent_ids, list) and len(agent_ids) > 1:
            badge = f'{len(agent_ids)} results'
        elif isinstance(agent_ids, list) and agent_ids:
            badge = f'{str(agent_ids[0])[:8]}'
        else:
            badge = f'{(fn_args.get("agent_id") or "?")[:8]}'

    post_build = (_build_await_post_build() if fn_name == 'await_agents'
                  else _build_spawn_post_build() if fn_name == 'spawn_agents'
                  else None)

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_swarm,
        source='Swarm', badge=badge, module_tag='Swarm',
        post_build=post_build,
    )


def _build_spawn_post_build():
    """Keep launch objectives on the existing frontend-only result metadata lane."""
    import json

    def _post_build(meta, tool_content, _fn_args):
        if not isinstance(tool_content, str):
            return
        try:
            receipt = json.loads(tool_content)
        except (ValueError, RecursionError):
            return
        if (isinstance(receipt, dict) and receipt.get('status') == 'async_launched'
                and isinstance(receipt.get('agents'), list)):
            meta['swarmLaunchReceipt'] = {
                'status': 'async_launched', 'agents': receipt['agents'],
            }
    return _post_build


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
        timed_out = bool(data.get('timed_out'))
        # Count against the FULL swarm roster when the backend reports it
        # (``swarm_status``). A narrow ids=[...] await used to render
        # ``done 0/1`` — 0 of the one AWAITED agent — while 2 more ran, which
        # read as the whole swarm state. Prefer the roster's done/total; fall
        # back to the awaited-only count for older payloads.
        roster = data.get('swarm_status') or []
        if isinstance(roster, list) and roster:
            n_total = len(roster)
            n_done = sum(
                1 for e in roster
                if isinstance(e, dict) and e.get('status') not in (None, 'running', 'pending'))
        else:
            n_done = len(completed)
            n_total = n_done + len(still_running)
        if data.get('yield_reason') == 'pending_input':
            meta['badge'] = 'yielded · pending input'
            meta['awaitYieldReason'] = 'pending_input'
            if still_running:
                meta['awaitStillRunning'] = still_running
            return
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
