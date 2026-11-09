"""Portable record CAS double and a settable clock for My Day goal specs."""
from __future__ import annotations

from copy import deepcopy

from lib.storage.errors import StorageError

OWNER = 7
DAY = 24 * 60 * 60


class Records:
    """record.get, versioned record.put and prefix record.list pages.

    ``before_put`` runs once at the next put, before its CAS check: it lets a
    spec commit a concurrent write between a command's read and its write.
    """

    def __init__(self):
        self.rows = {}
        self.puts = 0
        self.before_put = None

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            row = self.rows.get((payload['namespace'], payload['key']))
            return deepcopy({'key': payload['key'], **row}) if row else None
        assert operation == 'record.list'
        keys = sorted(key for namespace, key in self.rows if namespace == payload['namespace']
                      and key.startswith(payload['prefix']) and key > payload.get('after_key', ''))
        return [deepcopy({'key': key, **self.rows[(payload['namespace'], key)]})
                for key in keys[:payload['limit']]]

    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook()
        identity = (payload['namespace'], payload['key'])
        current = self.rows.get(identity)
        if (current['version'] if current else 0) != payload['expected_version']:
            raise StorageError('database_conflict', 'CAS conflict')
        self.rows[identity] = {'value': deepcopy(payload['value']),
                               'version': payload['expected_version'] + 1}
        self.puts += 1
        return {'version': payload['expected_version'] + 1}


class Clock:
    def __init__(self, start=1_794_000_000.0):
        self.now = start

    def __call__(self):
        return self.now
