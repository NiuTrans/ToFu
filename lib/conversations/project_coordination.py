"""Explicit project coordinator binding, not an execution state machine.

Entry points: ProjectCoordinationRepository.read/bind. Dependencies: the
owner-scoped ConversationRepository and portable record CAS storage. No
transcripts, task state, worker loops or tool grants are stored here.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from lib.conversations.project_identity import normalize_project_path
from lib.conversations.repository import ConversationRepository
from lib.identity import require_user_id
from lib.storage.errors import StorageError

NAMESPACE = 'project.coordination.binding.v1'


class ProjectCoordinationRepository:
    """One small binding per owner/project; validate the target on every read."""

    def __init__(self, user_id: int, *, client=None, conversations=None):
        self.user_id = require_user_id(user_id, context='project coordination')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.conversations = conversations or ConversationRepository(client)

    def _identity(self, path: str) -> tuple[str, str]:
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise ValueError('path must contain 1–4096 characters')
        path = normalize_project_path(path.strip())
        if not path:
            raise ValueError('path is required')
        return path, f'{self.user_id}:{hashlib.sha256(path.encode()).hexdigest()}'

    def _record(self, path: str, key: str):
        row = self.client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if row is None:
            return None
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(value, Mapping) or value.get('user_id') != self.user_id
                or value.get('projectPath') != path
                or not isinstance(value.get('conversationId'), str)):
            raise StorageError('database_integrity', 'Invalid coordination binding')
        return row

    def _target_available(self, path: str, conversation_id: str) -> bool:
        target = self.conversations.get(conversation_id, user_id=self.user_id,
                                        include_messages=False)
        if target is None:
            return False
        settings = target.metadata.get('settings') or {}
        if not isinstance(settings, Mapping):
            return False
        # Exact project identity only: sibling paths and matching cwd do not grant access.
        return (normalize_project_path(settings.get('projectPath') or '') == path
                and not settings.get('archived') and not settings.get('readOnly'))

    def _public(self, path: str, row) -> dict:
        conversation_id = row['value']['conversationId'] if row else ''
        available = bool(conversation_id and self._target_available(path, conversation_id))
        return {
            'projectPath': path, 'conversationId': conversation_id,
            'revision': int(row['version']) if row else 0,
            'availability': ('available' if available else 'unavailable')
                            if conversation_id else 'unbound',
            'capabilities': {'assign': False, 'sendMessage': False, 'steerSubagent': True},
        }

    def read(self, path: str) -> dict:
        path, key = self._identity(path)
        return self._public(path, self._record(path, key))

    def bind(self, path: str, conversation_id: str, *, expected_revision: int,
             command_id: str) -> dict:
        path, key = self._identity(path)
        if (not isinstance(conversation_id, str) or len(conversation_id) > 256
                or conversation_id != conversation_id.strip()):
            raise ValueError('Invalid conversationId')
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 0):
            raise ValueError('expectedRevision must be a non-negative integer')
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 256:
            raise ValueError('Idempotency-Key is required')
        if conversation_id and not self._target_available(path, conversation_id):
            raise ValueError('Conversation is not available in this owned project')
        value = {'user_id': self.user_id, 'projectPath': path,
                 'conversationId': conversation_id}
        # Include scope in the receipt identity; the supplied token is never an authority.
        receipt_key = hashlib.sha256(f'{key}\0{command_id}'.encode()).hexdigest()
        self.client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value,
            'expected_version': expected_revision,
        }, 'project-coordination:' + receipt_key)
        # Re-read current authority: a concurrent newer binding must not be overwritten
        # in the UI by the result of this request or an idempotent replay.
        return self.read(path)


__all__ = ['ProjectCoordinationRepository']
