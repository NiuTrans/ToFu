"""Owner-scoped BYO provider operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "provider.list": ops.OperationSpec("query", False, ops._provider_list),
    "provider.get": ops.OperationSpec("query", False, ops._provider_get),
    "provider.create": ops.OperationSpec("command", True, ops._provider_create),
    "provider.update": ops.OperationSpec("command", True, ops._provider_update),
    "provider.delete": ops.OperationSpec("command", True, ops._provider_delete),
    "provider.touch": ops.OperationSpec("command", False, ops._provider_touch),
}

__all__ = ["OPERATIONS"]
