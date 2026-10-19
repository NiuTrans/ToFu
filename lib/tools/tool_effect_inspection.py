"""Read-only operation inspection contract; storage loads only on invocation.

Execution delegates to ConversationSyncService. Its generated
ConversationToolEffectInspection schema owns the result, not a second tool DTO.
"""
from lib.tools.contracts import ToolContractV2, ToolErrorContract

TOOL_EFFECT_INSPECTION = ToolContractV2(
    name='inspect_tool_effect',
    model_description=(
        'Inspect an earlier interrupted write_file occurrence in this conversation. '
        'Use the exact operation_id and producing turn_id or invocation_id from its recovery instruction. '
        'The result is file_published (historical publication, not current content), '
        'file_not_published (proved at inspection), or unknown. This read never repeats '
        'a write, authorizes retry, resumes an invocation, or accepts a task.'),
    detailed_help=('Outcome schemas: ConversationToolEffectInspection and InvocationToolEffectInspection '
                   'in conversation_sync_v3.yaml. '
                   'An unknown outcome requires further evidence or explicit human takeover; '
                   'never turn missing evidence into success or permission to replay. '
                   'After publication, read current content before planning an authorized change.'),
    parameters={'type': 'object', 'additionalProperties': False,
                'required': ['operation_id'], 'properties': {
                    'operation_id': {'type': 'string', 'minLength': 32, 'maxLength': 32, 'pattern': '^[a-f0-9]{32}$'},
                    'invocation_id': {'type': 'string', 'minLength': 1, 'maxLength': 512,
                                      'description': 'Registered producing child invocation; mutually exclusive with turn_id.'},
                    'turn_id': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                                'description': 'Producing Turn ID; defaults to this task’s Turn.'}}},
    search_metadata=('unknown effect inspection recovery', '核实写入 恢复未知操作'),
    permission='read', idempotency='read_only', effect_recovery='read_only',
    ptc_eligible=True,
    errors=(ToolErrorContract('effect_unavailable', False,
                             'Retain unknown; obtain the exact producing occurrence or human guidance.'),
            ToolErrorContract('effect_store_unavailable', True,
                             'Retry this inspection read, never the original write.')),
)
