"""Isolated record.* authority for artifact-alias contract tests.

ArtifactRecordStore implements the existing version-CAS behavior in memory;
callers inject it at get_storage_client so tests never depend on a neighboring
API test having started a storage authority. No application runtime dependency.
"""

class ArtifactRecordStore:
    """In-memory stand-in for record.* with the authority's version CAS."""

    def __init__(self):
        self.records = {}
        self.calls = []
        self.before_put = None

    def command(self, op, payload, command_id, deadline=None):
        assert op == 'record.put'
        self.calls.append((op, payload.get('expected_version')))
        if self.before_put is not None:
            self.before_put(payload)
        ns, key = payload['namespace'], payload['key']
        current = self.records.get((ns, key))
        actual = current['version'] if current else 0
        expected = payload.get('expected_version')
        if expected is not None and expected != actual:
            from lib.agent_core.storage_errors import StorageError
            raise StorageError('database_conflict', 'Storage record version conflict')
        self.records[(ns, key)] = {'value': payload['value'],
                                   'version': actual + 1}
        return {'key': key, 'version': actual + 1}

    def query(self, op, payload, deadline=None):
        assert op == 'record.get'
        self.calls.append((op, None))
        return self.records.get((payload['namespace'], payload['key']))
