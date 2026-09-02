"""Conversation turn, attempt, sync, and migration operations."""

from lib.storage_sidecar import operations as ops

OPERATIONS = {
    # The lifecycle command is idempotent by (conversation_id, command_id)
    # and intentionally replays even if a lost-ACK retry carries a mutated
    # body; this mirrors the legacy lifecycle contract.
    'turn.create_pair': ops.OperationSpec(
        'command', False, ops._turn_create_pair, ops._turn_change_capture),
    'turn.append_settled': ops.OperationSpec(
        'command', True, ops._turn_append_settled, ops._turn_change_capture),
    'turn.attempt.claim': ops.OperationSpec('command', False, ops._turn_attempt_claim),
    'turn.attempt.create': ops.OperationSpec(
        'command', False, ops._turn_attempt_create, ops._turn_change_capture),
    # Semantic idempotency is owned by the deterministic attempt/job binding;
    # a lost-ACK retry must inspect current authority instead of replaying a
    # cached response that may predate worker settlement.
    'turn.attempt.dispatch_worker': ops.OperationSpec(
        'command', False, ops._turn_attempt_dispatch_worker,
        ops._turn_change_capture),
    'turn.projection.update': ops.OperationSpec(
        'command', False, ops._turn_projection_update, ops._turn_change_capture),
    'turn.related.announce': ops.OperationSpec(
        'command', False, ops._turn_related_announce, ops._turn_change_capture),
    'turn.branch.create': ops.OperationSpec(
        'command', False, ops._turn_branch_create, ops._turn_change_capture),
    'turn.branch.delete': ops.OperationSpec(
        'command', False, ops._turn_branch_delete, ops._turn_change_capture),
    'turn.compact': ops.OperationSpec(
        'command', True, ops._turn_compact, ops._turn_change_capture),
    'turn.delete': ops.OperationSpec(
        'command', False, ops._turn_delete, ops._turn_change_capture),
    'turn.recover': ops.OperationSpec(
        'command', False, ops._turn_recover, ops._turn_change_capture),
    'turn.cleanup': ops.OperationSpec('command', False, ops._turn_cleanup),
    'turn.search.backfill': ops.OperationSpec(
        'command', False, ops._turn_search_backfill),
    'turn.visible.sync': ops.OperationSpec(
        'command', False, ops._turn_visible_sync, ops._turn_change_capture),
    'turn.attempt.bind': ops.OperationSpec(
        'command', False, ops._turn_attempt_bind, ops._turn_change_capture),
    'turn.event.record': ops.OperationSpec(
        'command', False, ops._turn_event_record, ops._turn_change_capture),
    'turn.get': ops.OperationSpec('query', False, ops._turn_get),
    'turn.exists': ops.OperationSpec('query', False, ops._turn_exists),
    'turn.list': ops.OperationSpec('query', False, ops._turn_list),
    'turn.list_delta': ops.OperationSpec('query', False, ops._turn_list_delta),
    'turn.sync.snapshot': ops.OperationSpec('query', False, ops._turn_sync_snapshot),
    'turn.sync.changes': ops.OperationSpec('query', False, ops._turn_sync_changes),
    'turn.sync.prune': ops.OperationSpec('command', False, ops._turn_sync_prune),
    'turn.attempt.get': ops.OperationSpec('query', False, ops._attempt_get),
    'turn.revision': ops.OperationSpec('query', False, ops._turn_revision),
    'turn.events.list': ops.OperationSpec('query', False, ops._turn_events),
    # Idempotent by construction (age-bounded DELETE), so no receipt lane.
    'turn.events.prune': ops.OperationSpec('command', False, ops._turn_events_prune),
}

# Architectural ratchet: every new turn command is replay-visible by default.
# These maintenance/plumbing operations intentionally do not mutate a
# browser-observable projection.  Adding another exception requires an
# explicit review here instead of silently omitting transactional capture.
_NON_SYNC_COMMANDS = {
    'turn.attempt.claim',
    'turn.cleanup',
    'turn.events.prune',
    'turn.search.backfill',
    'turn.sync.prune',
}
for _name, _spec in OPERATIONS.items():
    if (_spec.kind == 'command' and _name not in _NON_SYNC_COMMANDS
            and _spec.after is None):
        raise RuntimeError(
            f'{_name} must declare transactional conversation-sync capture')

_registered_capture_operations = {
    name for name, spec in OPERATIONS.items()
    if spec.after is ops._turn_change_capture
}
if _registered_capture_operations != set(ops._TURN_CHANGE_CAPTURE_OPERATIONS):
    raise RuntimeError(
        'Turn conversation-sync capture mapping/catalog drift: '
        f'registered={sorted(_registered_capture_operations)!r}, '
        f'mapped={sorted(ops._TURN_CHANGE_CAPTURE_OPERATIONS)!r}'
    )

__all__ = ['OPERATIONS']
