"""Owned learned extraction rules over the selected storage authority's record CAS.

Entry point: SiteKnowledgeRepository. The caller supplies an explicit principal
and a captured browser/profile identity. This repository grants no browser,
model or mutation authority; application composition must admit those effects.
The schema owns persisted shape. Native/SQL selection, transport and receipts
belong to lib.storage; unscoped legacy JSON never enters this repository.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import cache
import hashlib
import json
from pathlib import Path

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError


class KnowledgeConflict(RuntimeError):
    """A different revision won; re-read and verify before another publication."""


def _encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@cache
def knowledge_contract():
    return json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/site_knowledge_v1.schema.json').read_text())


@cache
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(knowledge_contract())


def _storage(*, write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


@dataclass(frozen=True)
class KnowledgeRevision:
    version: int
    entry_json: str

    @property
    def entry(self) -> dict | None:
        return json.loads(self.entry_json)


class SiteKnowledgeRepository:
    def __init__(self, principal: PrincipalContext, *, client_id: str, profile: str,
                 client_factory=_storage):
        if not isinstance(principal, PrincipalContext):
            raise TypeError('Site knowledge requires an explicit principal')
        self.owner_id = principal.require_owner(context='site knowledge repository')
        if not isinstance(client_id, str) or not client_id.strip() or client_id != client_id.strip():
            raise ValueError('Site knowledge requires a captured browser client')
        if not isinstance(profile, str):
            raise ValueError('Site knowledge requires a captured browser profile')
        self._scope_json = _encode({'owner_user_id': self.owner_id,
            'tenant_id': principal.tenant_id, 'client_id': client_id, 'profile': profile})
        self._factory = client_factory

    def _identity(self, domain, operation):
        if (not isinstance(domain, str) or not domain or domain != domain.strip().lower()
                or not isinstance(operation, str) or not operation or operation != operation.strip()):
            raise ValueError('Site knowledge requires canonical domain and operation identity')
        return {'schema_version': 1, 'scope': json.loads(self._scope_json),
                'domain': domain, 'operation': operation}

    def _payload(self, identity):
        return {'namespace': knowledge_contract()['x-storage']['namespace'],
                'key': hashlib.sha256(_encode(identity).encode()).hexdigest(),
                'user_id': self.owner_id}

    def _decode(self, row, identity):
        if row is None:
            return KnowledgeRevision(0, 'null')
        if not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] < 1:
            raise ValueError('Invalid site knowledge revision')
        value = row.get('value')
        if (not isinstance(value, dict)
                or _encode({key: value.get(key) for key in identity}) != _encode(identity)):
            raise ValueError('Invalid site knowledge identity')
        error = next(_validator().iter_errors(value), None)
        if error is not None:
            raise ValueError('Invalid site knowledge record: ' + error.message)
        return KnowledgeRevision(row['version'], _encode(value['entry']))

    def read(self, domain: str, operation: str = 'search') -> KnowledgeRevision:
        identity = self._identity(domain, operation)
        return self._decode(self._factory().query('record.get', self._payload(identity)), identity)

    def _commit(self, domain, entry, expected_version, operation):
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('A nonnegative expected site knowledge revision is required')
        identity = self._identity(domain, operation)
        document = {**identity, 'entry': deepcopy(entry)}
        error = next(_validator().iter_errors(document), None)
        if error is not None:
            raise ValueError('Invalid site knowledge record: ' + error.message)
        encoded = _encode(document)
        entry_json = _encode(document['entry'])
        payload = self._payload(identity)
        digest = hashlib.sha256(_encode([payload, expected_version, document]).encode()).hexdigest()
        try:
            result = self._factory(write=True).command('record.put', {
                **payload, 'value': json.loads(encoded), 'expected_version': expected_version,
            }, 'site-knowledge:' + digest)
        except StorageError as error:
            if error.code == 'database_conflict':
                raise KnowledgeConflict('Site knowledge changed; verify the new revision') from error
            if error.code not in {'database_timeout', 'database_unavailable', 'database_busy'}:
                raise
            observed = self.read(domain, operation)
            if observed.version == expected_version + 1 and observed.entry_json == entry_json:
                return observed
            raise
        if (not isinstance(result, dict) or type(result.get('version')) is not int
                or result['version'] != expected_version + 1):
            raise ValueError('Site knowledge publication revision was not acknowledged')
        return KnowledgeRevision(result['version'], entry_json)

    def publish(self, domain: str, entry: dict, *, expected_version: int,
                operation: str = 'search') -> KnowledgeRevision:
        if not isinstance(entry, dict) or not str(entry.get('extractor_js') or '').strip():
            raise ValueError('A verified nonempty extractor is required')
        return self._commit(domain, entry, expected_version, operation)

    def retire(self, domain: str, *, expected_version: int,
               operation: str = 'search') -> KnowledgeRevision:
        return self._commit(domain, None, expected_version, operation)
