"""Application admission for manual compaction of owned conversations.

Entry points, called by DefaultConversationStore:
  * ``manual_compaction_rejection`` — refuse the destructive ``turn.compact``
    rewrite for conversations whose canonical Turns may be referenced by
    dispatch grants, reports or binding history.
  * ``schedule_working_view_compaction`` / ``scheduled_working_view_compaction``
    / ``clear_scheduled_working_view_compaction`` — the durable owner-scoped
    marker of a non-destructive working-view compaction for such protected
    conversations (docs/CONTEXT_COMPACTION.md, coordinator context lifecycle).
Dependencies are explicit conversation and project-binding repositories and a
storage client factory using the adapter's selected authority. No model summary
or request config is policy.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import time
import uuid

from lib.storage.errors import StorageError
from lib.tasks_pkg.compaction._context_lifecycle import (
    COORDINATOR_COMPACTION_UNSUPPORTED,
    PROJECT_COMPACTION_UNSUPPORTED,
)

SCHEDULED_COMPACTION_NAMESPACE = 'conversation.working_view_compaction.v1'


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


def _scheduled_key(conv_id, user_id):
    # Generic records carry no owner column: the key names the owner and the
    # value repeats both identities, which every read verifies.
    return f'{int(user_id)}:' + hashlib.sha256(str(conv_id).encode('utf-8')).hexdigest()


def _scheduled_row(client, conv_id, user_id):
    row = client().query('record.get', {
        'namespace': SCHEDULED_COMPACTION_NAMESPACE, 'key': _scheduled_key(conv_id, user_id)})
    if row is None:
        return None, 0
    value, version = row.get('value'), row.get('version')
    if (not isinstance(value, Mapping) or value.get('owner') != int(user_id)
            or value.get('conversationId') != conv_id
            or not isinstance(value.get('requestId'), str) or not value['requestId']
            or type(version) is not int or version < 1):
        raise StorageError('database_integrity', 'Invalid scheduled compaction marker')
    return dict(value), version


def scheduled_working_view_compaction(conv_id, *, user_id, client):
    """Return the pending marker ``{requestId, requestedAt, reason, ...}`` or None."""
    return _scheduled_row(client, conv_id, user_id)[0]


def schedule_working_view_compaction(conv_id, *, user_id, reason, client):
    """Record one pending request; a request while one is pending returns it unchanged."""
    marker, _ = _scheduled_row(client, conv_id, user_id)
    if marker is not None:
        return {**marker, 'created': False}
    value = {'owner': int(user_id), 'conversationId': conv_id,
             'requestId': uuid.uuid4().hex, 'requestedAt': int(time.time() * 1000),
             'reason': str(reason)}
    try:
        client(write=True).command('record.put', {
            'namespace': SCHEDULED_COMPACTION_NAMESPACE,
            'key': _scheduled_key(conv_id, user_id), 'value': value,
            'expected_version': 0,
        }, 'working-view-compaction:' + value['requestId'])
    except StorageError as exc:
        if exc.code != 'database_conflict':
            raise
        marker, _ = _scheduled_row(client, conv_id, user_id)
        if marker is None:
            raise
        return {**marker, 'created': False}  # A concurrent request won.
    return {**value, 'created': True}


def clear_scheduled_working_view_compaction(conv_id, *, user_id, request_id, client):
    """Retire exactly ``request_id``; a newer or absent marker is left untouched."""
    marker, version = _scheduled_row(client, conv_id, user_id)
    if marker is None or marker['requestId'] != request_id:
        return False
    try:
        result = client(write=True).command('record.delete', {
            'namespace': SCHEDULED_COMPACTION_NAMESPACE,
            'key': _scheduled_key(conv_id, user_id), 'expected_version': version,
        }, 'working-view-compaction-clear:' + request_id)
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return False
        raise
    return bool((result or {}).get('deleted'))
