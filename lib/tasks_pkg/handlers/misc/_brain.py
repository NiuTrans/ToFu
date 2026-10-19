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

    def _search_meta(meta, content, _args):
        receipt = getattr(content, 'conversation_search', None)
        if fn_name == 'list_conversations' and isinstance(receipt, dict):
            meta['conversationSearch'] = receipt

    detail = (fn_args.get('keyword', 'all') if fn_name == 'list_conversations'
              else str(fn_args.get('conversation_id') or '?')[:8])
    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run, source='Conversations', module_tag='ConvRef',
        title=f'{fn_name}: {detail}',

        post_build=_search_meta if fn_name == 'list_conversations' else None,
    )


@tool_registry.tool_set(
    frozenset({'create_conversation', 'dispatch_project_assignment', 'get_project_assignment', 'report_to_user',
               'delegate_project_work', 'await_project_workers', 'review_project_work', 'get_project_workers', 'cancel_project_work', 'finish_project_request'}),
    category='coordination', description='Independent worker conversations and authorized dispatch')
def _handle_coordinator_tools(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                              project_path, project_enabled, all_tools=None):
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    return simple_call(task, fn_name, fn_args, rn, round_entry, tc_id,
                       executor=lambda name, args: execute_coordinator_tool(task, name, args),
                       source='Coordinator', module_tag='Coordinator')


from lib.conversations.project_messages import NAMES as PROJECT_MESSAGE_NAMES


@tool_registry.tool_set(PROJECT_MESSAGE_NAMES, category='conversations',
                        description='Scoped project message submission, input and consideration receipts')
def _handle_project_message(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                            project_path, project_enabled, all_tools=None):
    from lib.conversations.project_messages import execute, ProjectMessageError
    from lib.storage.errors import StorageError
    import json
    def call(name, args):
        try:
            return json.dumps(execute(name, args, task), ensure_ascii=False)
        except (ProjectMessageError, StorageError) as error:
            return json.dumps({'error': str(error), 'code': getattr(error, 'code', 'project_message_rejected'),
                'execution_started': False, 'next_action': 'Read the exact scoped mailbox before retrying the same message identity.'}, ensure_ascii=False)
    return simple_call(task, fn_name, fn_args, rn, round_entry, tc_id,
                       executor=call, source='Project messages', module_tag='ProjectMessages')


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
        from lib.tasks_pkg.workspace_delivery import canonical_work_path
        canonical_path = canonical_work_path(task, project_path)
        work_id = ensure_work_item(
            task, canonical_path, trigger='isolated_workspace')
        if not work_id:
            return 'Error: this execution has no automatic Project work ID.'
        call_args = dict(_fn_args)
        # Checkers/Feed address logical Project work; workspace registrations
        # address the physical execution task under its canonical repository.
        call_args['task_id'] = str(task['id'])
        from lib import integration_state_repository
        workspace = integration_state_repository.find_workspace(
            canonical_path, str(task['id']), user_id=owner_user_id)
        automatic = bool(workspace and (workspace.get('origin') or {}).get('executionMode') == 'isolated-task')
        if _fn_name == 'integration_submit' and not automatic:
            results = run_all_enabled_checkers(
                canonical_path, user_id=owner_user_id, work_id=work_id,
                reason='integration')
            failed = [result for result in results if not result.get('ok')]
            if failed:
                labels = ', '.join(str(item.get('label') or 'checker')
                                   for item in failed)
                return (f'Error: integration submission rejected because '
                        f'checker(s) failed: {labels}. See Project Feed.')
        from lib.integration_control import execute_integration_tool
        return execute_integration_tool(
            _fn_name,
            call_args,
            project_path=canonical_path,
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
