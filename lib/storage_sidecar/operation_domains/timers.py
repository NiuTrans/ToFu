"""Durable scheduler timer operations."""

from lib.storage_sidecar import operations as ops

OPERATIONS = {
    'timer.create': ops.OperationSpec('command', True, ops._timer_create),
    'timer.cancel': ops.OperationSpec('command', True, ops._timer_cancel),
    'timer.get': ops.OperationSpec('query', False, ops._timer_get),
    'timer.list': ops.OperationSpec('query', False, ops._timer_list),
    'timer.active.list_all': ops.OperationSpec(
        'query', False, ops._timer_active_list_all),
    'timer.active.count': ops.OperationSpec(
        'query', False, ops._timer_active_count),
    'timer.history': ops.OperationSpec('query', False, ops._timer_history),
    'timer.update': ops.OperationSpec('command', True, ops._timer_update),
    'timer.poll.append': ops.OperationSpec('command', False, ops._timer_poll_append),
    'timer.poll.commit': ops.OperationSpec('command', True, ops._timer_poll_commit),
    'timer.progress': ops.OperationSpec('command', True, ops._timer_progress),
    'timer.poll.log': ops.OperationSpec('query', False, ops._timer_poll_log),
}

__all__ = ['OPERATIONS']
