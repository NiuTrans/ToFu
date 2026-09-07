"""Owner-scoped model-routing v2 semantic operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "model_routing.get": ops.OperationSpec(
        "query", False, ops._model_routing_get),
    "model_routing.commit": ops.OperationSpec(
        "command", True, ops._model_routing_commit),
    "model_routing.migration_receipt": ops.OperationSpec(
        "query", False, ops._model_routing_migration_receipt),
    "model_routing.migration_receipt.put": ops.OperationSpec(
        "command", True, ops._model_routing_migration_receipt_put),
    "model_routing.secret.put": ops.OperationSpec(
        "command", True, ops._model_routing_secret_put),
    "model_routing.secret.get": ops.OperationSpec(
        "query", False, ops._model_routing_secret_get),
    "model_routing.secret.list": ops.OperationSpec(
        "query", False, ops._model_routing_secret_list),
    "model_routing.secret.delete": ops.OperationSpec(
        "command", True, ops._model_routing_secret_delete),
    "model_routing.secret.prune": ops.OperationSpec(
        "command", False, ops._model_routing_secret_prune),
}

__all__ = ["OPERATIONS"]
