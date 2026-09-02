"""Optimizer and aggregate-log operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'optimizer.proposal.create': ops.OperationSpec(
        'command', True, ops._optimizer_proposal_create),
    'optimizer.proposal.update': ops.OperationSpec(
        'command', True, ops._optimizer_proposal_update),
    'optimizer.proposal.get': ops.OperationSpec(
        'query', False, ops._optimizer_proposal_get),
    'optimizer.proposal.list': ops.OperationSpec(
        'query', False, ops._optimizer_proposal_list),
    'optimizer.action.record': ops.OperationSpec(
        'command', True, ops._optimizer_action_record),
    'optimizer.action.outcome': ops.OperationSpec(
        'command', True, ops._optimizer_action_outcome),
    'optimizer.action.revert': ops.OperationSpec(
        'command', True, ops._optimizer_action_revert),
    'optimizer.action.list': ops.OperationSpec(
        'query', False, ops._optimizer_action_list),
    'optimizer.action.expired': ops.OperationSpec(
        'query', False, ops._optimizer_action_expired),
    'optimizer.action.for_proposal': ops.OperationSpec(
        'query', False, ops._optimizer_action_for_proposal),
    'log_aggregate.flush': ops.OperationSpec(
        'command', False, ops._log_aggregate_flush),
    'log_aggregate.query': ops.OperationSpec(
        'query', False, ops._log_aggregate_query),
}

__all__ = ['OPERATIONS']
