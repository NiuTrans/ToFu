"""One owner/tenant/conversation aggregate in the selected store's record CAS.

The explicit conversation check precedes every read/write. No SQL, filesystem
storage, module-global owner, or model-controlled namespace crosses this seam.
"""
from copy import deepcopy
import hashlib
import json

from lib.identity import PrincipalContext
from lib.storage import StorageError, get_storage_client
from .contract import limit


class FactConflict(Exception):
    """The caller must reload the aggregate before editing."""


class FactsUnavailable(Exception):
    """Missing and foreign conversation/source identities are indistinguishable."""


class FactNotFound(FactsUnavailable):
    """An authorized conversation has no fact with the requested identity."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class FactRepository:
    def __init__(self, conversation_id: str, principal: PrincipalContext):
        from lib.response_assets import owned_conversation
        self.owner = principal.require_owner(context='working facts')
        self.tenant = principal.tenant_id
        self.conversation_id = str(conversation_id)
        if not self.conversation_id or len(self.conversation_id) > 128:
            raise FactsUnavailable('Working facts unavailable')
        try:
            owned_conversation(self.conversation_id, self.owner)
        except PermissionError as exc:
            raise FactsUnavailable('Working facts unavailable') from exc
        except StorageError as exc:
            if exc.code in {'database_forbidden', 'database_not_found'}:
                raise FactsUnavailable('Working facts unavailable') from exc
            raise
        self.key = digest([self.owner, self.tenant, self.conversation_id])

    def payload(self):
        return {'namespace': 'working_facts_v1', 'key': self.key, 'user_id': self.owner}

    def read(self):
        row = get_storage_client().query('record.get', self.payload())
        if not row:
            return {'facts': {}}, 0
        value = row['value']
        if (value.get('owner') != self.owner or value.get('tenant') != self.tenant
                or value.get('conversationId') != self.conversation_id):
            raise FactsUnavailable('Working facts unavailable')
        return deepcopy(value), row['version']

    def put(self, value, revision):
        value = deepcopy(value)
        value.update(owner=self.owner, tenant=self.tenant, conversationId=self.conversation_id)
        value.pop('_commitDigest', None)
        if len(json.dumps(value, ensure_ascii=False).encode()) > limit('maxRecordBytes'):
            raise ValueError('Working facts record is full; existing evidence was preserved')
        commit = digest([self.key, revision, value])
        value['_commitDigest'] = commit
        try:
            result = get_storage_client(write=True).command('record.put', {
                **self.payload(), 'value': value, 'expected_version': revision,
            }, 'working-facts:' + commit)
        except StorageError as exc:
            if exc.code == 'database_conflict':
                raise FactConflict('Working facts changed; reload before editing') from exc
            if exc.code not in {'database_timeout', 'database_busy', 'database_unavailable'}:
                raise
            observed, version = self.read()
            if version == revision + 1 and observed.get('_commitDigest') == commit:
                return
            raise
        if not isinstance(result, dict) or result.get('version') != revision + 1:
            raise ValueError('Working facts commit was not acknowledged')
