"""Owner-scoped desktop preference operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "desktop.egress_agent.get": ops.OperationSpec(
        "query", False, ops._desktop_egress_agent_get),
    # Both mutations are receipted: the preference is durable user state, and
    # callers retry an ambiguous acknowledgement with the same command id.
    "desktop.egress_agent.initialize": ops.OperationSpec(
        "command", True, ops._desktop_egress_agent_initialize),
    "desktop.egress_agent.set": ops.OperationSpec(
        "command", True, ops._desktop_egress_agent_set),
}

__all__ = ["OPERATIONS"]
