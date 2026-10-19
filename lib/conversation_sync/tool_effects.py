"""Read one exact effect from an already owner-scoped authoritative Turn.

No arguments, paths or proof supplied by the HTTP caller are accepted here.
Publication evidence belongs to the domain journal; this query owns no state.
"""
from collections.abc import Mapping

from .validation import ContractViolation, decode


def inspect_tool_effect(stored, *, conversation_id, operation_id, producer_task_id=None):
    projection = stored.get('projection') or {}
    return inspect_effect_rows(projection.get('toolRounds', []),
        conversation_id=conversation_id, operation_id=operation_id, expected_scope='attempt',
        expected_task_id=producer_task_id)


def inspect_effect_rows(rows, *, conversation_id, operation_id, expected_scope, expected_task_id=None):
    """Shared domain query after the caller has proven its producing owner."""
    matches = [row for row in rows
               if isinstance(row, Mapping) and isinstance(row.get('executionEffect'), Mapping)
               and row['executionEffect'].get('operationId') == operation_id]
    if not matches:
        from .service import ConversationSyncNotFound
        raise ConversationSyncNotFound('Tool occurrence not found')
    if len(matches) != 1:
        return 'unknown', 'evidence_conflict'
    row = matches[0]
    effect = row['executionEffect']
    reference = effect.get('reconciliation')
    if (effect.get('scope') != expected_scope or row.get('toolName') != 'write_file'
            or not isinstance(reference, Mapping)):
        return 'unknown', 'unsupported'
    try:
        reference = decode('ProjectFileWriteReconciliation', reference)
    except ContractViolation:
        return 'unknown', 'evidence_conflict'
    if (row.get('taskId') != reference['taskId']
            or expected_task_id is not None and row.get('taskId') != expected_task_id):
        return 'unknown', 'evidence_conflict'
    from lib.project_mod.effect_reconciliation import inspect_prepared_write
    return inspect_prepared_write(reference, conversation_id=conversation_id)
