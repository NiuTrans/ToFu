"""Compaction archive operation catalog."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
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
