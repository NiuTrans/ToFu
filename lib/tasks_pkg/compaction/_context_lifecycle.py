"""Manual compaction admission through the operation's selected store.

The core validates the admission result without importing application storage.
ConversationStore implementations own policy reads and must recheck before a
destructive commit. This port does not authorize a working-context reset.
"""
from __future__ import annotations


COORDINATOR_COMPACTION_UNSUPPORTED = 'coordinator_context_compaction_unsupported'
PROJECT_COMPACTION_UNSUPPORTED = 'project_context_compaction_unsupported'


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
