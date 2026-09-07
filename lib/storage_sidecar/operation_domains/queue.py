"""Durable turn-source queue operations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'queue.enqueue': ops.OperationSpec('command', True, ops._queue_enqueue),
    'queue.list': ops.OperationSpec('query', False, ops._queue_list),
    'queue.remove': ops.OperationSpec('command', True, ops._queue_remove),
    'queue.clear': ops.OperationSpec('command', True, ops._queue_clear),
    'queue.kind.clear': ops.OperationSpec(
        'command', True, ops._queue_kind_clear),
    'queue.dequeue': ops.OperationSpec('command', False, ops._queue_dequeue),
    'queue.lease.release': ops.OperationSpec(
        'command', True, ops._queue_lease_release),
    'queue.reap': ops.OperationSpec('command', True, ops._queue_reap),
    'queue.lease.bind': ops.OperationSpec(
        'command', True, ops._queue_lease_bind),
    'queue.finalize': ops.OperationSpec('command', True, ops._queue_finalize),
    'queue.depth': ops.OperationSpec('query', False, ops._queue_depth),
    'queue.conversations.list_all': ops.OperationSpec(
        'query', False, ops._queue_conversations_list_all),
    'queue.autopilot.get': ops.OperationSpec(
        'query', False, ops._queue_autopilot_get),
    'queue.autopilot.list_all': ops.OperationSpec(
        'query', False, ops._queue_autopilot_list_all),
    'queue.autopilot.arm': ops.OperationSpec(
        'command', False, ops._queue_autopilot_arm),
    'queue.autopilot.clear': ops.OperationSpec(
        'command', True, ops._queue_autopilot_clear),
}


__all__ = ['OPERATIONS']
