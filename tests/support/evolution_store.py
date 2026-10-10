"""Shared synthetic Sidecar record.* seam for self-evolution store tests."""
from __future__ import annotations

from copy import deepcopy

from lib.storage.errors import StorageError


class EvolutionRecordStore:
    def __init__(self):
        self.rows = {}
        self.versions = {}
        self.writes = 0
        self.failures_left = 0
        self.permanent_failure = False

    def command(self, operation, payload, command_id):
        assert operation in ('record.put','record.delete')
        self.writes += 1
        if self.permanent_failure:
            raise RuntimeError('write refused durably')
        if self.failures_left > 0:
            self.failures_left -= 1
            raise StorageError('database_locked', 'transient writer starvation',
                               retryable=True, retry_after_ms=0)
        if operation == 'record.delete':
            key=(payload['namespace'],payload['key'])
            deleted=self.rows.pop(key,None) is not None
            self.versions.pop(key,None)
            return {'deleted':deleted}
        key = (payload['namespace'], payload['key'])
        version = self.versions.get(key, 0)
        if payload.get('expected_version', version) != version:
            raise StorageError('database_conflict', 'version conflict')
        self.rows[key] = deepcopy(payload['value'])
        self.versions[key] = version + 1
        return {'version': version + 1}

    def query(self, operation, payload):
        if operation == 'record.get':
            value = self.rows.get((payload['namespace'], payload['key']))
            return {'value': deepcopy(value), 'version': self.versions.get((payload['namespace'], payload['key']), 1)} if value else None
        assert operation == 'record.list'
        return [{'key': key, 'value': deepcopy(value)} for (namespace, key), value in
                sorted(self.rows.items(), reverse=bool(payload.get('before_key'))) if namespace == payload['namespace']
                and key.startswith(payload['prefix'])
                and (not payload.get('after_key') or key > payload['after_key'])
                and (not payload.get('before_key') or key < payload['before_key'])][:payload['limit']]


def install_store(monkeypatch) -> EvolutionRecordStore:
    from lib.evolution import records

    value = EvolutionRecordStore()
    monkeypatch.setattr(records, '_storage', lambda **kwargs: value)
    monkeypatch.setattr(records, '_sleep', lambda delay: None)
    return value
