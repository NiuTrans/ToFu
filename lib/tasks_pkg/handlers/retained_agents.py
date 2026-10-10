"""Tool adapter for owner-scoped reusable Swarm identities."""

from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tasks_pkg.manager import append_event


@tool_registry.tool_set({'manage_agents', 'list_agents'}, category='swarm',
                        description='Inspect or manage reusable agents')
def _handle_manage_agents(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                          cfg, project_path, project_enabled, all_tools=None):
    fn_args = dict(fn_args or {})
    if fn_name == 'list_agents':
        fn_args['action'] = 'inspect' if fn_args.get('agent_id') else 'list'
    def execute(_name, arguments):
        from lib.swarm.retained import execute_manage_agents
        return execute_manage_agents(
            arguments, task=task, cfg=cfg,
            all_tools=all_tools or [], project_path=project_path,
            abort_check=lambda: bool(task.get('aborted')),
            on_event=lambda event: append_event(task, event),
        )

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=execute, source='Team Agent', badge=str(fn_args.get('action') or ''),
        module_tag='Swarm',
    )
