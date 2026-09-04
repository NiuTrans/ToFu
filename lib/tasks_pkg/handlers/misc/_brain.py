# HOT_PATH
"""Conversation reference and retained Integration tool handlers."""

from __future__ import annotations

from lib.conv_ref import execute_conv_ref_tool
from lib.log import get_logger
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tools.conversation import CONV_REF_TOOL_NAMES, INTEGRATION_TOOL_NAMES


logger = get_logger(__name__)


@tool_registry.tool_set(
    CONV_REF_TOOL_NAMES,
    category='conversations',
    description='List and retrieve past conversations',
)
def _handle_conv_ref_tool(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    del tc, cfg, project_enabled, all_tools
    from lib.tasks_pkg.manager import task_user_id
    current_conv_id = task.get('convId')
    owner_user_id = int(task_user_id(task))

    def _run(_fn_name, _fn_args):
        return execute_conv_ref_tool(
            _fn_name,
            _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path,
            user_id=owner_user_id,
        )

    detail = (fn_args.get('keyword', 'all') if fn_name == 'list_conversations'
              else str(fn_args.get('conversation_id') or '?')[:8])
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run, source='Conversations', module_tag='ConvRef',
        title=f'{fn_name}: {detail}',
    )


@tool_registry.tool_set(
    INTEGRATION_TOOL_NAMES,
    category='conversations',
    description='Checkpoint or submit this execution\'s isolated workspace',
)
def _handle_integration_tool(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    del tc, cfg, all_tools
    current_conv_id = str(task.get('convId') or '')
    args = dict(fn_args or {})
    from lib.tasks_pkg.manager import task_user_id
    owner_user_id = int(task_user_id(task))

    def _run(_fn_name, _fn_args):
        if not (project_enabled and project_path):
            return 'Error: integration tools require project mode.'
        from lib.conversations.project_brain import (
            ensure_work_item,
            run_all_enabled_checkers,
        )
        work_id = ensure_work_item(
            task, project_path, trigger='isolated_workspace')
        if not work_id:
            return 'Error: this execution has no automatic Project work ID.'
        call_args = dict(_fn_args)
        call_args['task_id'] = work_id
        if _fn_name == 'integration_submit':
            results = run_all_enabled_checkers(
                project_path, user_id=owner_user_id, work_id=work_id,
                reason='integration')
            failed = [result for result in results if not result.get('ok')]
            if failed:
                labels = ', '.join(str(item.get('label') or 'checker')
                                   for item in failed)
                return (f'Error: integration submission rejected because '
                        f'checker(s) failed: {labels}. See Project Attention.')
        from lib.integration_control import execute_integration_tool
        return execute_integration_tool(
            _fn_name,
            call_args,
            project_path=project_path,
            user_id=owner_user_id,
            conv_id=current_conv_id,
        )

    verb = fn_name.replace('integration_', '', 1)
    return simple_call(
        task, fn_name, args, rn, round_entry, tc_id,
        executor=_run, source='Integration', module_tag='Integration',
        badge=verb,
    )


__all__ = ['_handle_conv_ref_tool', '_handle_integration_tool']
