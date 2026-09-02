"""Conversation artifact operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'artifact.create': ops.OperationSpec('command', True, ops._artifact_create),
    'artifact.get': ops.OperationSpec('query', False, ops._artifact_get),
    'artifact.list': ops.OperationSpec('query', False, ops._artifact_list),
    'artifact.delete': ops.OperationSpec('command', True, ops._artifact_delete),
    'artifact.versions': ops.OperationSpec('query', False, ops._artifact_versions),
    'artifact.library': ops.OperationSpec('query', False, ops._artifact_library),
    'artifact.pin': ops.OperationSpec('command', True, ops._artifact_pin),
    'tool_result_artifact.put': ops.OperationSpec(
        'command', True, ops._tool_result_artifact_put),
    'tool_result_artifact.read': ops.OperationSpec(
        'query', False, ops._tool_result_artifact_read),
    'tool_result_artifact.search': ops.OperationSpec(
        'query', False, ops._tool_result_artifact_search),
    'tool_result_artifact.prune': ops.OperationSpec(
        'maintenance', False, ops._tool_result_artifact_prune),
}

__all__ = ['OPERATIONS']
