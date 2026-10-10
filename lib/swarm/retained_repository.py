"""Durable, owner-scoped identities for reusable Swarm workers.

This repository is the only place that maps an owner/project/conversation to
the portable record store. A retained worker is idle between ordinary child
invocations; no model loop or thread is held on its behalf.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id


NAMESPACE = 'swarm.retained_agent.v1'
INDEX_NAMESPACE = 'swarm.retained_agent_roster.v1'
# TODO(enterprise): the portable record operation has no physical owner
# column. This repository enforces owner/tenant/scope through opaque keys and
# read validation; a future shared-administration API needs a dedicated
# semantic storage operation with predicates in every backend.
MAX_MEMORY_BYTES = 8 * 1024 * 1024
MAX_ROSTER = 256
MAX_ROSTER_SCAN = 1000


class RetainedAgentError(RuntimeError):
    """A reusable worker cannot be safely read or changed."""


def scope_for(*, project_path: str = '', conversation_id: str = '',
              requested: str = '') -> str:
    """Return a stable, opaque scope without exposing project paths in keys."""
    choice = requested or ('project' if project_path else 'conversation')
    if choice == 'project':
        source = normalize_project_path(project_path)
        if not source:
            raise RetainedAgentError('A project-scoped agent requires a project.')
        kind = 'p'
    elif choice == 'conversation':
        source = str(conversation_id or '').strip()
        if not source:
            raise RetainedAgentError('A conversation-scoped agent requires a conversation.')
        kind = 'c'
    else:
        raise RetainedAgentError('scope must be project or conversation')
    return f'{kind}:{hashlib.sha256(source.encode("utf-8")).hexdigest()}'


class RetainedAgentRepository:
    """CAS repository; every method requires the explicit owner at creation."""

    def __init__(self, user_id: int, *, tenant_id: str = '', client=None):
        self.user_id = require_user_id(user_id, context='retained agent owner')
        self.tenant_id = str(tenant_id or '')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self._client = client

    def _prefix(self, scope: str) -> str:
        if not isinstance(scope, str) or not scope.startswith(('p:', 'c:')):
            raise RetainedAgentError('Invalid retained agent scope')
        tenant = hashlib.sha256(self.tenant_id.encode('utf-8')).hexdigest()[:24]
        return f'{self.user_id}:{tenant}:{scope}:'

    def _key(self, scope: str, agent_id: str) -> str:
        try:
            canonical = str(uuid.UUID(str(agent_id)))
        except (ValueError, AttributeError) as error:
            raise RetainedAgentError('Invalid agent ID') from error
        return self._prefix(scope) + canonical

    def get(self, scope: str, agent_id: str) -> tuple[dict, int] | None:
        row = self._client().query('record.get', {
            'namespace': NAMESPACE, 'key': self._key(scope, agent_id),
        })
        if not isinstance(row, dict):
            return None
        value = row.get('value')
        if (not isinstance(value, dict)
                or value.get('user_id') != self.user_id
                or value.get('tenant_id') != self.tenant_id
                or value.get('scope') != scope
                or value.get('id') != agent_id):
            raise RetainedAgentError('Retained agent ownership record is invalid')
        return value, int(row['version'])

    def list(self, scope: str) -> list[dict]:
        rows = self._client().query('record.list', {
            # The roster holds metadata only. Listing full 8 MiB working
            # transcripts would breach the storage frame at ordinary scale.
            'namespace': INDEX_NAMESPACE, 'prefix': self._prefix(scope),
            'limit': MAX_ROSTER_SCAN,
        })
        if not isinstance(rows, list):
            raise RetainedAgentError('Retained agent roster is unavailable')
        result = []
        for row in rows:
            value = row.get('value') if isinstance(row, dict) else None
            if (not isinstance(value, dict)
                    or value.get('user_id') != self.user_id
                    or value.get('tenant_id') != self.tenant_id
                    or value.get('scope') != scope):
                raise RetainedAgentError('Retained agent roster contains invalid ownership')
            result.append(value)
        return result

    def _sync_index(self, scope: str, agent_id: str, value: dict,
                    source_version: int) -> None:
        """Publish a small monotonic roster projection after authority."""
        from lib.storage.errors import StorageError
        key = self._key(scope, agent_id)
        indexed = {field: value.get(field) for field in (
            'id', 'user_id', 'tenant_id', 'scope', 'role', 'name', 'state',
            'generation', 'last_result', 'last_status', 'created_at', 'updated_at')}
        indexed['message_count'] = len(value.get('messages') or [])
        indexed['source_version'] = source_version
        for _ in range(4):
            row = self._client().query('record.get', {
                'namespace': INDEX_NAMESPACE, 'key': key,
            })
            current = row.get('value') if isinstance(row, dict) else None
            if (isinstance(current, dict)
                    and int(current.get('source_version') or 0) >= source_version):
                return
            expected = int(row['version']) if isinstance(row, dict) else 0
            try:
                self._client(write=True).command('record.put', {
                    'namespace': INDEX_NAMESPACE, 'key': key,
                    'value': indexed, 'expected_version': expected,
                }, f'retained-agent-roster:{uuid.uuid4().hex}')
                return
            except StorageError as error:
                # The roster is a projection. A lost ACK may already have
                # published this version; the next read checks it before
                # issuing any new write.
                if (error.code != 'database_conflict'
                        and (error.request_not_dispatched or not error.retryable)):
                    raise
        raise RetainedAgentError('Agent roster changed concurrently')

    def create(self, scope: str, *, role: str, name: str,
               messages: list | None = None, last_result: str = '',
               last_status: str = '') -> dict:
        if len(self.list(scope)) >= MAX_ROSTER:
            raise RetainedAgentError('Retained agent roster is full; archive or delete an agent')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise RetainedAgentError('Agent name must contain 1–80 characters')
        if not isinstance(role, str) or not role or len(role) > 64:
            raise RetainedAgentError('Invalid agent role')
        from lib.timeutil import now_ms
        agent_id = str(uuid.uuid4())
        value = {
            'id': agent_id, 'user_id': self.user_id,
            'tenant_id': self.tenant_id, 'scope': scope,
            'role': role, 'name': name.strip(), 'state': 'idle',
            'generation': 1, 'run_id': '', 'swarm_key': '',
            'messages': list(messages or []),
            'last_result': str(last_result or '')[:4000],
            'last_status': str(last_status or '')[:32],
            'created_at': now_ms(), 'updated_at': now_ms(),
        }
        receipt = self._put(scope, agent_id, value, expected=0)
        self._sync_index(scope, agent_id, value, int(receipt['version']))
        return value

    def _put(self, scope: str, agent_id: str, value: dict, *, expected: int) -> dict:
        from lib.storage.errors import StorageError
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
        if len(encoded) > MAX_MEMORY_BYTES:
            raise RetainedAgentError('Agent memory exceeds its durable size limit')
        payload = {
            'namespace': NAMESPACE, 'key': self._key(scope, agent_id),
            'value': value, 'expected_version': expected,
        }
        try:
            return self._client(write=True).command(
                'record.put', payload, f'retained-agent:{uuid.uuid4().hex}')
        except StorageError as error:
            if error.code == 'database_conflict' or error.request_not_dispatched:
                raise
            # An ambiguous ACK may follow a successful commit. Never issue a
            # second write with a fresh receipt before checking authority.
            row = self._client().query('record.get', {
                'namespace': NAMESPACE, 'key': payload['key'],
            })
            if (isinstance(row, dict) and row.get('value') == value
                    and row.get('version') == expected + 1):
                return {'version': expected + 1}
            raise

    def change(self, scope: str, agent_id: str, transform) -> dict:
        from lib.storage.errors import StorageError
        for _ in range(4):
            found = self.get(scope, agent_id)
            if found is None:
                raise RetainedAgentError('Agent not found in this scope')
            value, version = found
            updated = transform(dict(value))
            if not isinstance(updated, Mapping):
                raise RetainedAgentError('Agent state transition is invalid')
            updated = dict(updated)
            from lib.timeutil import now_ms
            updated['updated_at'] = now_ms()
            try:
                receipt = self._put(scope, agent_id, updated, expected=version)
                self._sync_index(scope, agent_id, updated, int(receipt['version']))
                return updated
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise
        raise RetainedAgentError('Agent changed concurrently; retry the operation')

    def delete(self, scope: str, agent_id: str) -> None:
        from lib.storage.errors import StorageError
        found = self.get(scope, agent_id)
        if found is None:
            raise RetainedAgentError('Agent not found in this scope')
        value, version = found
        if value['state'] == 'busy':
            raise RetainedAgentError('Wait for the agent to finish before deleting it')
        key = self._key(scope, agent_id)
        try:
            self._client(write=True).command('record.delete', {
                'namespace': NAMESPACE, 'key': key,
                'expected_version': version,
            }, f'retained-agent-delete:{uuid.uuid4().hex}')
        except StorageError as error:
            if error.request_not_dispatched or self.get(scope, agent_id) is not None:
                raise
        index_row = self._client().query('record.get', {
            'namespace': INDEX_NAMESPACE, 'key': key,
        })
        if not isinstance(index_row, dict):
            return
        try:
            self._client(write=True).command('record.delete', {
                'namespace': INDEX_NAMESPACE, 'key': key,
                'expected_version': int(index_row['version']),
            }, f'retained-agent-roster-delete:{uuid.uuid4().hex}')
        except StorageError as error:
            if error.request_not_dispatched:
                raise
            remaining = self._client().query('record.get', {
                'namespace': INDEX_NAMESPACE, 'key': key,
            })
            if isinstance(remaining, dict):
                raise


__all__ = [
    'RetainedAgentError', 'RetainedAgentRepository', 'scope_for',
    'MAX_MEMORY_BYTES', 'MAX_ROSTER', 'MAX_ROSTER_SCAN',
]
