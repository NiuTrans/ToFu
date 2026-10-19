"""Durable, owner-scoped identities for reusable Swarm workers.

This repository is the only place that maps an owner/project/conversation to
the portable record store. A retained worker is idle between ordinary child
invocations; no model loop or thread is held on its behalf.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

NAMESPACE = 'swarm.retained_agent.v1'
INDEX_NAMESPACE = 'swarm.retained_agent_roster.v1'
# TODO(enterprise): the portable record operation has no physical owner
# column. This repository enforces owner/tenant/scope through opaque keys and
# read validation; a future shared-administration API needs a dedicated
# semantic storage operation with predicates in every backend.
_ROSTER_PAGE_ROWS = 1000  # physical metadata window, never a roster quota


class RetainedAgentError(RuntimeError):
    """A reusable worker cannot be safely read or changed."""



class _RosterProjectionConflict(RetainedAgentError):
    """Only the reconstructible roster exhausted its bounded CAS attempts."""

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

    def __init__(self, user_id: int, *, tenant_id: str = '', client=None,
                 task: dict | None = None, cancellable: bool = True):
        self.user_id = require_user_id(user_id, context='retained agent owner')
        self.tenant_id = str(tenant_id or '')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self._client = client

        self._task = task
        self._cancellable = cancellable

    def _query(self, operation, payload):
        if self._task is None:
            return self._client().query(operation, payload)
        from lib.tasks_pkg.persistence_wait import wait_for_persistence
        return wait_for_persistence(self._task,
            lambda: self._client().query(operation, payload), label=operation,
            cancellable=self._cancellable)

    def _command(self, operation, payload, command_id):
        if self._task is None:
            return self._client(write=True).command(operation, payload, command_id)
        from lib.tasks_pkg.persistence_wait import wait_for_persistence
        return wait_for_persistence(self._task,
            lambda: self._client(write=True).command(operation, payload, command_id),
            label=operation, cancellable=self._cancellable)

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
        row = self._query('record.get', {
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

    def _rows(self, scope: str, namespace: str, *, include_value=True):
        """Stream ordered metadata pages; never enumerate authority dialogue."""
        after = ''
        prefix = self._prefix(scope)
        while True:
            payload = {'namespace': namespace, 'prefix': prefix,
                       'limit': _ROSTER_PAGE_ROWS, 'include_value': include_value}
            if after:
                payload['after_key'] = after
            rows = self._query('record.list', payload)
            if not isinstance(rows, list) or len(rows) > _ROSTER_PAGE_ROWS:
                raise RetainedAgentError('Retained agent roster is unavailable')
            if not rows:
                return
            for row in rows:
                key = row.get('key') if isinstance(row, dict) else None
                if (not isinstance(key, str) or not key.startswith(prefix) or key <= after
                        or type(row.get('version')) is not int or row['version'] < 1):
                    raise RetainedAgentError('Retained agent roster cursor/version is invalid')
                after = key
                yield row

    def _validate_index(self, scope: str, row: dict) -> dict:
        value = row.get('value')
        if (not isinstance(value, dict) or value.get('user_id') != self.user_id
                or value.get('tenant_id') != self.tenant_id or value.get('scope') != scope
                or row['key'] != self._key(scope, value.get('id'))
                or type(value.get('source_version')) is not int or value['source_version'] < 1):
            raise RetainedAgentError('Retained agent roster contains invalid ownership/version')
        return value

    @staticmethod
    def _roster_value(value: dict, source_version: int) -> dict:
        indexed = {field: value.get(field) for field in (
            'id', 'user_id', 'tenant_id', 'scope', 'role', 'name', 'state',
            'generation', 'last_result', 'last_status', 'created_at', 'updated_at')}
        indexed['message_count'] = len(value.get('messages') or [])
        indexed['source_version'] = source_version
        return indexed

    def list(self, scope: str) -> list[dict]:
        """Join authority headers to the cache; repair only mismatched identities.

        This is a paged read, not a transaction-wide snapshot. Concurrent updates
        may appear at their observed version, but a stale index never substitutes
        for an authoritative version or invents an identity after deletion.
        """
        from lib.storage.errors import StorageError
        result = []
        cached_rows = iter(self._rows(scope, INDEX_NAMESPACE))
        cached = next(cached_rows, None)
        for header in self._rows(scope, NAMESPACE, include_value=False):
            while cached is not None and cached['key'] < header['key']:
                self._validate_index(scope, cached)
                cached = next(cached_rows, None)
            indexed = None
            if cached is not None and cached['key'] == header['key']:
                indexed = self._validate_index(scope, cached)
                cached = next(cached_rows, None)
            if indexed is not None and indexed['source_version'] == header['version']:
                result.append(indexed)
                continue
            agent_id = header['key'][len(self._prefix(scope)):]
            found = self.get(scope, agent_id)
            if found is None:
                continue  # Concurrent deletion is authoritative, not a phantom row.
            value, version = found
            if version < header['version']:
                raise RetainedAgentError('Retained agent authority version regressed')
            result.append(self._roster_value(value, version))
            try:
                self._sync_index(scope, agent_id, value, version)
            except (StorageError, _RosterProjectionConflict):
                # Projection repair is optional: exact authority supplied this
                # response, and the next listing retries the same version.
                logger.warning('Retained roster projection repair unavailable')
        return result

    def _sync_index(self, scope: str, agent_id: str, value: dict,
                    source_version: int) -> None:
        """Publish a small monotonic roster projection after authority."""
        from lib.storage.errors import StorageError
        key = self._key(scope, agent_id)
        indexed = self._roster_value(value, source_version)
        for _ in range(4):
            row = self._query('record.get', {
                'namespace': INDEX_NAMESPACE, 'key': key,
            })
            current = row.get('value') if isinstance(row, dict) else None
            if (isinstance(current, dict)
                    and int(current.get('source_version') or 0) >= source_version):
                return
            expected = int(row['version']) if isinstance(row, dict) else 0
            try:
                self._command('record.put', {
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
        raise _RosterProjectionConflict('Agent roster changed concurrently')

    def create(self, scope: str, *, role: str, name: str,
               messages: list | None = None, last_result: str = '',
               last_status: str = '') -> dict:
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
        payload = {
            'namespace': NAMESPACE, 'key': self._key(scope, agent_id),
            'value': value, 'expected_version': expected,
        }
        try:
            return self._command(
                'record.put', payload, f'retained-agent:{uuid.uuid4().hex}')
        except StorageError as error:
            if error.code == 'database_conflict' or error.request_not_dispatched:
                raise
            # An ambiguous ACK may follow a successful commit. Never issue a
            # second write with a fresh receipt before checking authority.
            row = self._query('record.get', {
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
        index_row = self._query('record.get', {
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
]
