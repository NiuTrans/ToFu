"""Owner/tenant-scoped automation records over the selected storage authority.

Entry points: AutomationRepository and VersionedEntity. Each mutation uses the
existing backend-neutral record CAS; an occurrence is one atomic aggregate
containing its immutable dispatch intent. No database/path/backend selection
or executor policy lives here. Bulky output belongs in existing artifact stores.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Callable

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError

from .conditions import check_json_bounds
from .contract import LIMITS, STORAGE_NAMESPACES
from .definitions import PreparedDefinition
from .errors import AutomationConflict, AutomationError

NAMESPACES = {kind:STORAGE_NAMESPACES[kind] for kind in ('definition','run','cursor','target','active')}

MAX_RECORD_BYTES = LIMITS['recordBytes']
MAX_PAGE_ROWS = LIMITS['pageRows']
_ENTITY_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


@dataclass(frozen=True, slots=True)
class VersionedEntity:
    id: str
    version: int
    document_json: str

    @property
    def value(self) -> dict:
        return json.loads(self.document_json)


class AutomationRepository:
    """An explicitly bound repository; cross-owner keys never enter its API."""

    def __init__(self, principal: PrincipalContext, *, client_factory: Callable = _storage):
        if not isinstance(principal, PrincipalContext):
            raise TypeError('Automation repository requires a principal')
        self.principal = principal
        self.owner_id = principal.require_owner(context='automation repository')
        self._factory = client_factory
        tenant = principal.tenant_id or ''
        tenant_digest = hashlib.sha256(tenant.encode('utf-8')).hexdigest()[:32]
        self._prefix = f'{self.owner_id}:{tenant_digest}:'

    def _key(self, entity_id: str) -> str:
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
            raise AutomationError('invalid_entity_id', 'Invalid automation entity id')
        return self._prefix + entity_id

    def _decode(self, entity_id: str, row: dict | None) -> VersionedEntity | None:
        if row is None:
            return None
        if not isinstance(row, dict):
            raise AutomationError('automation_storage_integrity', 'Invalid automation record envelope')
        value = row.get('value')
        if (not isinstance(value, dict) or type(row.get('version')) is not int
                or row['version'] < 1 or type(value.get('ownerId')) is not int
                or value.get('ownerId') != self.owner_id
                or value.get('tenantId') != self.principal.tenant_id
                or value.get('id') != entity_id):
            raise AutomationError('automation_storage_integrity', 'Invalid automation record identity')
        return VersionedEntity(entity_id, row['version'],
                               json.dumps(value, allow_nan=False, sort_keys=True))

    def get(self, kind: str, entity_id: str) -> VersionedEntity | None:
        self.principal.require_scope('automations:read')
        row = self._factory().query('record.get', {
            'namespace': NAMESPACES[kind], 'key': self._key(entity_id), 'user_id': self.owner_id,
        })
        return self._decode(entity_id, row)

    def page(self, kind: str, *, limit: int = 32, after: str = '') -> tuple[list[VersionedEntity], str]:
        self.principal.require_scope('automations:read')
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS:
            raise AutomationError('invalid_page_limit', 'Automation page limit must be 1..64')
        payload = {'namespace': NAMESPACES[kind], 'prefix': self._prefix,
                   'limit': limit, 'user_id': self.owner_id}
        if after:
            payload['after_key'] = self._key(after)
        rows = self._factory().query('record.list', payload)
        if not isinstance(rows, list) or len(rows) > limit:
            raise AutomationError('automation_storage_integrity', 'Invalid automation page')
        result = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get('key', '')).startswith(self._prefix):
                raise AutomationError('automation_storage_integrity', 'Automation page escaped its owner')
            entity_id = row['key'][len(self._prefix):]
            self._key(entity_id)
            result.append(self._decode(entity_id, row))
        return result, result[-1].id if len(result) == limit else ''

    def run_history(self, definition_id: str, *, limit: int = 8,
                    before: str = '') -> tuple[list[VersionedEntity], str]:
        """Read newest indexed runs with bounded point hydration, never a scan."""
        self.principal.require_scope('automations:read')
        self._key(definition_id)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS:
            raise AutomationError('invalid_page_limit', 'Invalid automation history page limit')
        if before and (not isinstance(before, str) or
                       not re.fullmatch(r'[0-9]{16}:[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', before)):
            raise AutomationError('invalid_history_cursor', 'Invalid automation history cursor')
        prefix = self._prefix + definition_id + ':'
        rows = self._factory().query('record.list', {
            'namespace': STORAGE_NAMESPACES['history'], 'prefix': prefix,
            'before_key': prefix + (before or '~'), 'limit': limit, 'user_id': self.owner_id,
        })
        if not isinstance(rows, list) or len(rows) > limit:
            raise AutomationError('automation_storage_integrity', 'Invalid automation history page')
        result, cursors = [], []
        for row in rows:
            pointer = row.get('value') if isinstance(row, dict) else None
            if (not isinstance(pointer, dict) or pointer.get('definitionId') != definition_id
                    or type(pointer.get('ownerId')) is not int or pointer['ownerId'] != self.owner_id
                    or pointer.get('tenantId') != self.principal.tenant_id
                    or type(pointer.get('createdAt')) not in {int, float}
                    or not math.isfinite(pointer['createdAt']) or pointer['createdAt'] <= 0):
                raise AutomationError('automation_storage_integrity', 'Invalid owned history pointer')
            run = self.get('run', pointer.get('id'))
            if (run is None or run.value['definitionId'] != definition_id
                    or run.value['createdAt'] != pointer['createdAt']):
                raise AutomationError('automation_storage_integrity', 'History has no matching run')
            cursor = f'{int(run.value["createdAt"] * 1000):016d}:{run.id}'
            if row.get('key') != prefix + cursor:
                raise AutomationError('automation_storage_integrity', 'Invalid history index key')
            result.append(run)
            cursors.append(cursor)
        if cursors != sorted(cursors, reverse=True) or len(set(cursors)) != len(cursors):
            raise AutomationError('automation_storage_integrity', 'Invalid history ordering')
        return result, cursors[-1] if len(result) == limit else ''

    def put(self, kind: str, entity_id: str, value: dict, *, expected_version: int) -> VersionedEntity:
        """CAS one aggregate; classify a lost acknowledgement by exact readback."""
        if kind in {'active', 'run'}:
            raise AutomationError('automation_index_owned', 'Runs and active pointers are owned by atomic execution operations')
        self.principal.require_scope('automations:manage' if kind == 'definition' else 'automations:execute')
        if type(expected_version) is not int or expected_version < 0:
            raise AutomationError('revision_required', 'A nonnegative expected revision is required')
        check_json_bounds(value, max_depth=LIMITS['recordDepth'], max_nodes=LIMITS['recordNodes'])
        document = deepcopy(value)
        document.update(id=entity_id, ownerId=self.owner_id, tenantId=self.principal.tenant_id)
        document.pop('_commitDigest', None)
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > MAX_RECORD_BYTES - 128:
            raise AutomationError('automation_record_too_large', 'Automation evidence must use artifact references')
        digest = hashlib.sha256((str(expected_version) + '\n' + encoded).encode('utf-8')).hexdigest()
        document['_commitDigest'] = digest
        payload = {'namespace': NAMESPACES[kind], 'key': self._key(entity_id),
                   'value': document, 'expected_version': expected_version, 'user_id': self.owner_id}
        command_id = 'automation:' + hashlib.sha256(
            (kind + '\n' + self._key(entity_id) + '\n' + digest).encode('utf-8')).hexdigest()
        try:
            result = self._factory(write=True).command('record.put', payload, command_id)
        except StorageError as exc:
            if exc.code == 'database_conflict':
                raise AutomationConflict('Automation revision changed') from exc
            if exc.code not in {'database_timeout', 'database_unavailable', 'database_busy'}:
                raise
            # No blind command replay: exact committed bytes prove success.
            row = self._factory().query('record.get', {
                'namespace': NAMESPACES[kind], 'key': self._key(entity_id), 'user_id': self.owner_id,
            })
            committed = self._decode(entity_id, row)
            if (committed is not None and committed.version == expected_version + 1
                    and committed.value.get('_commitDigest') == digest):
                return committed
            raise
        if not isinstance(result, dict) or result.get('version') != expected_version + 1:
            raise AutomationError('automation_storage_integrity', 'Automation write was not acknowledged')
        return VersionedEntity(entity_id, result['version'], json.dumps(document, allow_nan=False, sort_keys=True))

    def save_definition(self, prepared: PreparedDefinition, *, expected_version: int, now: float) -> VersionedEntity:
        definition = prepared.definition
        return self.put('definition', definition['id'], {
            'schemaVersion': 1, 'definition': definition,
            'compiled': {'digest': prepared.digest, 'stepOrder': list(prepared.ordered_step_ids),
                         'actions': prepared.action_contracts},
            'updatedAt': now, 'deleted': False,
        }, expected_version=expected_version)

    def retire_definition(self, entity: VersionedEntity, *, now: float) -> VersionedEntity:
        value = entity.value
        if value.get('ownerId') != self.owner_id or value.get('tenantId') != self.principal.tenant_id:
            raise AutomationError('automation_owner_mismatch', 'Definition is outside this repository')
        value['deleted'] = True
        value['definition']['enabled'] = False
        value['updatedAt'] = now
        return self.put('definition', entity.id, value, expected_version=entity.version)
