"""Application admission for destructive compaction of owned canonical Turns.

Entry point: manual_compaction_rejection, called by DefaultConversationStore.
Dependencies are explicit conversation and project-binding repositories using
the adapter's storage authority. No model summary or request config is policy.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.storage.errors import StorageError
from lib.tasks_pkg.compaction._context_lifecycle import (
    COORDINATOR_COMPACTION_UNSUPPORTED,
    PROJECT_COMPACTION_UNSUPPORTED,
)


def manual_compaction_rejection(conv_id, *, user_id, conversations, bindings,
                                messages=()):
    """Refuse loss of source Attempts, grants and canonical report identities.

    Former coordinator creation identities and reports survive unbinding/moves.
    Any binding epoch conservatively protects the project's conversations:
    without a complete reverse-reference index a partial history cannot prove
    that deleting Turns is safe. Unknown ownership/storage failures propagate.
    """
    document = conversations.get(conv_id, user_id=user_id, include_messages=False)
    if document is None:
        return 'not_found'
    settings = document.metadata.get('settings') or {}
    if not isinstance(settings, Mapping):
        raise StorageError('database_integrity', 'Invalid conversation settings')
    if str(conv_id).startswith('coordinator-') or any(
            isinstance(message, Mapping)
            and str(message.get('_turnKind') or '').startswith('coordinator_report.')
            for message in messages):
        return COORDINATOR_COMPACTION_UNSUPPORTED
    project = settings.get('projectPath')
    if not project:
        return None
    binding = bindings.read(project)
    # Unavailable still identifies the protected conversation. Archived/read-only
    # status or a temporarily invalid target must not restore mutation authority.
    if binding.get('conversationId') == conv_id:
        return COORDINATOR_COMPACTION_UNSUPPORTED
    if binding['revision'] > 0:
        return PROJECT_COMPACTION_UNSUPPORTED
    return None
