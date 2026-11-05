"""Manual compaction admission through the operation's selected store.

The core validates the admission result without importing application storage.
ConversationStore implementations own policy reads and must recheck before a
destructive commit. Protected conversations never take that rewrite: their
compaction is a scheduled working-view operation whose durable owner-scoped
marker lives behind the same store port (docs/CONTEXT_COMPACTION.md,
coordinator context lifecycle). Stores without the port refuse scheduling.
"""
from __future__ import annotations


COORDINATOR_COMPACTION_UNSUPPORTED = 'coordinator_context_compaction_unsupported'
PROJECT_COMPACTION_UNSUPPORTED = 'project_context_compaction_unsupported'
# Destructive-rewrite refusals that a scheduled working-view compaction serves.
PROTECTED_COMPACTION_SCOPES = {
    COORDINATOR_COMPACTION_UNSUPPORTED: 'coordinator',
    PROJECT_COMPACTION_UNSUPPORTED: 'project',
}
# Marker reason of an ordinary conversation scheduled while its task runs.
BUSY_COMPACTION_REASON = 'task_active'


class ManualCompactionRefused(ValueError):
    """A canonical transcript cannot safely undergo this persistent rewrite."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def manual_compaction_rejection(conv_id, *, store, user_id, messages=()):
    """Use the same store as the transcript read; unknown policy never admits."""
    admit = getattr(store, 'manual_compaction_rejection', None)
    if not callable(admit):
        return 'turn_protocol_unsupported'
    rejection = admit(conv_id, user_id=user_id, messages=messages)
    if rejection is not None and (
            not isinstance(rejection, str) or not rejection.strip()):
        raise TypeError(
            'ConversationStore.manual_compaction_rejection must return '
            'None or a non-empty rejection reason')
    return rejection


def _marker(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get('requestId'), str)
            or not value['requestId'] or type(value.get('requestedAt')) is not int):
        raise TypeError('ConversationStore returned a malformed scheduled compaction marker')
    return value


def schedule_working_view_compaction(conv_id, *, store, user_id, reason):
    """Record the pending request; None when the store cannot schedule."""
    schedule = getattr(store, 'schedule_working_view_compaction', None)
    if not callable(schedule):
        return None
    marker = _marker(schedule(conv_id, user_id=user_id, reason=reason))
    if marker is None or type(marker.get('created')) is not bool:
        raise TypeError('ConversationStore must report whether it created the marker')
    return marker


def scheduled_working_view_compaction(conv_id, *, store, user_id):
    """Return the pending marker, or None (also for stores without the port)."""
    read = getattr(store, 'scheduled_working_view_compaction', None)
    return _marker(read(conv_id, user_id=user_id)) if callable(read) else None


def clear_scheduled_working_view_compaction(conv_id, *, store, user_id, request_id):
    clear = getattr(store, 'clear_scheduled_working_view_compaction', None)
    return bool(clear(conv_id, user_id=user_id, request_id=request_id)) if callable(clear) else False
