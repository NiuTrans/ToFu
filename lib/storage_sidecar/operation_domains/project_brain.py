"""Project Brain event/projection semantic operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'project_brain.get': ops.OperationSpec(
        'query', False, ops._project_brain_get),
    'project_brain.active.list': ops.OperationSpec(
        'query', False, ops._project_brain_list_active),
    'project_brain.work.start': ops.OperationSpec(
        'command', True, ops._project_brain_work_start),
    'project_brain.work.refine': ops.OperationSpec(
        'command', True, ops._project_brain_work_refine),
    'project_brain.work.change': ops.OperationSpec(
        'command', True, ops._project_brain_work_change),
    'project_brain.work.finish': ops.OperationSpec(
        'command', True, ops._project_brain_work_finish),
    'project_brain.narrative.add': ops.OperationSpec(
        'command', True, ops._project_brain_narrative_add),
    'project_brain.attention.add': ops.OperationSpec(
        'command', True, ops._project_brain_attention_add),
    'project_brain.checker.register': ops.OperationSpec(
        'command', True, ops._project_brain_checker_register),
    'project_brain.checker.result': ops.OperationSpec(
        'command', True, ops._project_brain_checker_result),
    'project_brain.decision.promote': ops.OperationSpec(
        'command', True, ops._project_brain_decision_promote),
    'project_brain.watch.add': ops.OperationSpec(
        'command', True, ops._project_brain_watch_add),
    'project_brain.watch.update': ops.OperationSpec(
        'command', True, ops._project_brain_watch_update),
    'project_brain.watch.delete': ops.OperationSpec(
        'command', True, ops._project_brain_watch_delete),
    # Prepare mutates only once (cursor initialization) and must remain fresh
    # thereafter, so it intentionally has no permanent command receipt.
    'project_brain.cursor.prepare': ops.OperationSpec(
        'command', False, ops._project_brain_cursor_prepare),
    'project_brain.cursor.confirm': ops.OperationSpec(
        'command', True, ops._project_brain_cursor_confirm),
    'project_brain.cutover.status': ops.OperationSpec(
        'query', False, ops._project_brain_cutover_status),
    'project_brain.cutover': ops.OperationSpec(
        'command', True, ops._project_brain_cutover),
    'project_brain.recovery.snapshot': ops.OperationSpec(
        'maintenance', False, ops._project_brain_recovery_snapshot),
    'project_brain.rebuild': ops.OperationSpec(
        'maintenance', False, ops._project_brain_rebuild),
}


__all__ = ['OPERATIONS']
