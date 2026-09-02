"""Scheduled task and proactive poll operations."""

from lib.storage_sidecar import operations as ops

OPERATIONS = {
    'scheduler.task.create': ops.OperationSpec('command', True, ops._scheduler_create),
    'scheduler.task.ensure': ops.OperationSpec('command', True, ops._scheduler_ensure),
    'scheduler.task.get': ops.OperationSpec('query', False, ops._scheduler_get),
    'scheduler.task.list': ops.OperationSpec('query', False, ops._scheduler_list),
    'scheduler.task.list_all': ops.OperationSpec(
        'query', False, ops._scheduler_list_all),
    'scheduler.task.update': ops.OperationSpec('command', True, ops._scheduler_update),
    'scheduler.task.delete': ops.OperationSpec('command', True, ops._scheduler_delete),
    'scheduler.task.record_result': ops.OperationSpec('command', True, ops._scheduler_record_result),
    'scheduler.task.claim_due': ops.OperationSpec('command', True, ops._scheduler_claim_due),
    'scheduler.poll.append': ops.OperationSpec('command', False, ops._scheduler_poll_append),
    'scheduler.poll.log': ops.OperationSpec('query', False, ops._scheduler_poll_log),
}

__all__ = ['OPERATIONS']
