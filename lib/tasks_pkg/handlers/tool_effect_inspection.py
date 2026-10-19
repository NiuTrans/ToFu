"""Owner/conversation-bound read adapter for exact tool-effect evidence.

Entry: registered inspect_tool_effect handler. No journal, replay, or approval
policy lives here; the common service and normal tool settlement remain owners.
Storage imports stay lazy for the standalone agent package.
"""
import json

from lib.identity import require_user_id
from lib.tasks_pkg.executor import tool_registry, _build_simple_meta, _finalize_tool_round
from lib.tools.contracts import ToolContractError
from lib.tools.tool_effect_inspection import TOOL_EFFECT_INSPECTION
from lib.tools.result_envelope import typed_tool_error


def inspect_for_task(task, arguments):
    arguments = TOOL_EFFECT_INSPECTION.validate_arguments(arguments)
    user_id = require_user_id(task.get('_userId'), context='tool effect inspection')
    conversation_id = task.get('convId')
    invocation_id = arguments.get('invocation_id')
    if invocation_id:
        if arguments.get('turn_id') or not conversation_id:
            raise ToolContractError('effect_unavailable', 'Select one producing Turn or invocation')
        from lib.tasks_pkg.invocation_effects import inspect_child_effect
        return inspect_child_effect(user_id=user_id, conversation_id=conversation_id,
            invocation_id=invocation_id, operation_id=arguments['operation_id'])
    turn_id = arguments.get('turn_id') or task.get('_turnId')
    if not conversation_id or not turn_id:
        raise ToolContractError('effect_unavailable', 'A durable producing conversation and Turn are required')
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    return ConversationSyncService(SidecarConversationSyncRepository()).inspect_tool_effect(
        conversation_id, turn_id, user_id, operation_id=arguments['operation_id'])


@tool_registry.handler('inspect_tool_effect', category='general',
                       description='Inspect interrupted file publication')
def handle_tool_effect_inspection(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                                  cfg, project_path, project_enabled, all_tools=None):
    status = 'done'
    try:
        content = json.dumps(inspect_for_task(task, fn_args), ensure_ascii=False, separators=(',', ':'))
    except Exception as exc:
        # The adapter reports failed inspection as a tool error, never as an
        # outcome fact. Missing/foreign records are intentionally indistinguishable.
        if isinstance(exc, (ToolContractError, ValueError)):
            code, retryable = 'effect_unavailable', False
        else:
            from lib.conversation_sync.service import ConversationSyncNotFound
            from lib.storage.errors import StorageError
            if isinstance(exc, ConversationSyncNotFound):
                code, retryable = 'effect_unavailable', False
            elif isinstance(exc, StorageError):
                code, retryable = 'effect_store_unavailable', bool(exc.retryable)
            else:
                raise
        content = typed_tool_error(code, retryable=retryable,
            next_action='Retry only this inspection read if available; do not repeat the write.',
            message='File-publication evidence could not be inspected. The operation outcome remains unknown.'
        ).to_envelope_text()
        status = 'error'
    meta = _build_simple_meta(fn_name, content, source='Tool outcome', title='Inspect file publication')
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
