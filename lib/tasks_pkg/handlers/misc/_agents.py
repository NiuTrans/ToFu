# HOT_PATH
"""Automation / sub-agent misc handlers: ``scheduler``, ``desktop`` and
``swarm`` tool families (plus their small executor + badge helpers).

``append_event`` is resolved THROUGH the package facade
(``lib.tasks_pkg.handlers.misc``) at call time so a test patching
``lib.tasks_pkg.handlers.misc.append_event`` steers these handlers exactly as
before the package split.
"""

from __future__ import annotations

from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.log import get_logger
from lib.scheduler import SCHEDULER_TOOL_NAMES, execute_scheduler_tool
from lib.swarm.tools import SWARM_TOOL_NAMES
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call

logger = get_logger(__name__)


def _append_event(task, ev):
    """Route append_event through the package facade so monkeypatching
    ``lib.tasks_pkg.handlers.misc.append_event`` remains effective."""
    from lib.tasks_pkg.handlers import misc as _facade
    return _facade.append_event(task, ev)


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


def _run_desktop(fn_name, fn_args, user_id=''):
    """Desktop tool executor — wraps send_desktop_command + format_desktop_result.

    The wire cmd_type is the full tool name verbatim: the agent's dispatch
    table (lib/desktop_agent/_dispatch.COMMANDS) is keyed by it, prefix and
    all — never strip or rewrite it here. ``user_id`` (RWA P4a) scopes the
    command to the caller's own agents on relay deployments.
    """
    from lib.desktop import format_desktop_result, send_desktop_command
    result, error = send_desktop_command(fn_name, fn_args, timeout=30,
                                         user_id=user_id)
    if error:
        return f'Desktop Agent Error: {error}'
    return format_desktop_result(fn_name, result)


@tool_registry.tool_set(DESKTOP_TOOL_NAMES, category='desktop',
                        description='Interact with the desktop agent')
def _handle_desktop_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    # Closure (same pattern as _run_swarm) so the task's bridge user rides
    # into the command — RWA P4a user scoping on relay deployments.
    def _exec_desktop(_fn_name, _fn_args):
        return _run_desktop(_fn_name, _fn_args,
                            user_id=task.get('_userId', '') or '')
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_exec_desktop,
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
            on_event=lambda ev: _append_event(task, ev),
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
        agent_ids = fn_args.get('agent_ids')
        if isinstance(agent_ids, list) and len(agent_ids) > 1:
            badge = f'{len(agent_ids)} results'
        elif isinstance(agent_ids, list) and agent_ids:
            badge = f'{str(agent_ids[0])[:8]}'
        else:
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
