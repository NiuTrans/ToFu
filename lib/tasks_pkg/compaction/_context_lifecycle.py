"""Admission for destructive manual compaction, not a working-context reset.

The existing manual command deletes/folds canonical Turns. Coordinator grants,
source Attempts and report pointers require those exact identities/projections.
Until continuation storage supports an atomic same-frontier working-view update,
refuse here rather than rebind, delete a checkpoint or invent another transcript.
The application adapter rechecks this policy immediately before turn.compact.
"""
from __future__ import annotations

from collections.abc import Mapping


COORDINATOR_COMPACTION_UNSUPPORTED = 'coordinator_context_compaction_unsupported'
PROJECT_COMPACTION_UNSUPPORTED = 'project_context_compaction_unsupported'


class ManualCompactionRefused(ValueError):
    """A canonical transcript cannot safely undergo this persistent rewrite."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def manual_compaction_rejection(conv_id, *, user_id, messages=(),
                                conversations=None, bindings=None):
    """Read owned authority; request config/model summaries never select policy.

    A former coordinator's creation identity and canonical reports remain
    protected even after unbinding or a project move. For legacy identities,
    any binding epoch in the owned project conservatively protects all its
    conversations: there is no complete reverse index of source/report refs.
    This bounded refusal is safer than scanning or trusting a history page.
    Storage/identity failures propagate, never become permission to compact.
    """
    from lib.conversations.repository import ConversationRepository
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.storage.errors import StorageError

    conversations = conversations if conversations is not None else ConversationRepository()
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
    bindings = bindings if bindings is not None else ProjectCoordinationRepository(
        user_id, conversations=conversations)
    binding = bindings.read(project)
    # Unavailable still identifies the protected conversation. Archived/read-only
    # status or a temporarily invalid target must not restore mutation authority.
    if binding.get('conversationId') == conv_id:
        return COORDINATOR_COMPACTION_UNSUPPORTED
    if binding['revision'] > 0:
        return PROJECT_COMPACTION_UNSUPPORTED
    return None
