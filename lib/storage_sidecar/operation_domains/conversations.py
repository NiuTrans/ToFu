"""Conversation transcript and revision-CAS operations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'conversation.get': ops.OperationSpec(
        'query', False, ops._conversation_get),
    'conversation.list': ops.OperationSpec(
        'query', False, ops._conversation_list),
    'conversation.count': ops.OperationSpec(
        'query', False, ops._conversation_count),
    'conversation.search': ops.OperationSpec(
        'query', False, ops._conversation_search_op),
    'conversation.create': ops.OperationSpec(
        'command', True, ops._conversation_create),
    'conversation.settings.update': ops.OperationSpec(
        'command', True, ops._conversation_settings_update),
    'conversation.metadata.update': ops.OperationSpec(
        'command', True, ops._conversation_metadata_update),
    'conversation.delete': ops.OperationSpec(
        'command', True, ops._conversation_delete),
    'conversation.restore': ops.OperationSpec(
        'command', True, ops._conversation_restore),
    'conversation.clone': ops.OperationSpec(
        'command', True, ops._conversation_clone),
    'conversation.purge': ops.OperationSpec(
        'command', True, ops._conversation_purge),
    'conversation.trash.prune': ops.OperationSpec(
        'command', False, ops._conversation_trash_prune),
}


__all__ = ['OPERATIONS']
