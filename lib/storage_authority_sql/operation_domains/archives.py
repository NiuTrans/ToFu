"""Compaction archive operation catalog."""

from lib.storage_authority_sql import operations as ops
from lib.storage_authority_sql.operations_pkg._model_continuation import (
    continuation_get, continuation_put, continuation_delete,
)


from lib.storage_authority_sql.operations_pkg._invocations import register_child, resolve_child

OPERATIONS = {

    'invocation.register_child': ops.OperationSpec('command', False, register_child),
    'invocation.resolve_child': ops.OperationSpec('query', False, resolve_child),
    # Live frontier CAS, not receipt replay: retain no historical payload copies.
    'model_continuation.get': ops.OperationSpec('query', False, continuation_get),
    'model_continuation.put': ops.OperationSpec('command', False, continuation_put),
    'model_continuation.delete': ops.OperationSpec('command', False, continuation_delete),
    "compaction_archive.create": ops.OperationSpec(
        "command", True, ops._archive_create
    ),
    "compaction_archive.list": ops.OperationSpec(
        "query", False, ops._archive_list
    ),
    "compaction_archive.get": ops.OperationSpec(
        "query", False, ops._archive_get
    ),
    "compaction_archive.update_summary": ops.OperationSpec(
        "command", True, ops._archive_update_summary
    ),
    "compaction_archive.delete_conversation": ops.OperationSpec(
        "command", True, ops._archive_delete_conversation
    ),
    "compaction_archive.prune": ops.OperationSpec(
        "command", True, ops._archive_prune
    ),
}


__all__ = ["OPERATIONS"]
