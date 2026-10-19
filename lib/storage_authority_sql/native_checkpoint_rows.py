"""Private SQL bytes for the shared Rust checkpoint Store.

The caller supplies the already selected Backend and authenticated tenant/owner.
Each CAS is one receipt-free durable Backend.command; reads are one statement.
No journal transitions, recovery decisions, backend open, or implicit retry.
"""
import hashlib
import time

import orjson

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.text_size import json_utf8_length_expression
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
from lib.storage_authority_sql.json_document import JsonBytes

HEADERS = SPEC['namespaces']['headers']
SNAPSHOTS = SPEC['namespaces']['snapshots']
FENCES = SPEC['namespaces']['replay_fences']
RESERVED_NAMESPACES = frozenset(SPEC['namespaces'].values())


def _refuse(code='database_protocol_error'):
    raise StorageError(code, 'Native checkpoint storage request refused')


class NativeCheckpointRows:
    """Trusted storage callback; all state transitions remain in Rust."""

    def __init__(self, backend, *, tenant_id, owner_user_id, document_bytes, identity_bytes, logical_outbox=None):
        self.backend = backend
        self.tenant = tenant_id
        self.owner = owner_user_id
        self.document_bytes = document_bytes
        self.identity_bytes = identity_bytes
        self.validate_handoff = None
        self.goal_transaction = None
        self.logical_outbox = logical_outbox

    def command(self, request, callback, deadline_at):
        from lib.storage_authority_sql.native_checkpoint_outbox import command
        return command(self, request, callback, deadline_at)

    def key(self, task):
        return hashlib.sha256(orjson.dumps([self.tenant, self.owner, task])).hexdigest()

    def _envelope(self, task, value):
        return {'format': SPEC['callback_format'], 'tenant_id': self.tenant,
                'owner_user_id': self.owner, 'task_id': task, 'value': value}

    def _decode(self, raw, task, maximum):
        if isinstance(raw, (bytes, str)):
            raw = orjson.loads(raw)
        if (not isinstance(raw, dict) or set(raw) != {'format', 'tenant_id', 'owner_user_id', 'task_id', 'value'}
                or type(raw.get('tenant_id')) is not int or type(raw.get('owner_user_id')) is not int
                or raw != self._envelope(task, raw.get('value'))):
            _refuse('database_integrity')
        value = raw['value']
        self._value(value, task, maximum)
        return value

    def _value(self, value, task, maximum):
        if (not isinstance(value, dict) or type(value.get('owner_user_id')) is not int
                or value['owner_user_id'] != self.owner or value.get('task_id') != task):
            _refuse('database_integrity')
        if len(orjson.dumps(value)) > maximum:
            _refuse('database_integrity')

    def _projection(self, session, alias, maximum):
        # jsonb text can expand whitespace/escapes. Bound materialization, then
        # recheck the compact document's actual budget after decoding.
        limit = (maximum + self.identity_bytes) * 6 + SPEC['envelope_bytes']
        length = json_utf8_length_expression(session.backend, 'value_json')
        length = length.replace('value_json', alias + '.value_json')
        return f'CASE WHEN {length} <= {limit} THEN {alias}.value_json ELSE NULL END'

    def _header(self, session, task, key, maximum):
        row = session.fetch_one(
            f'SELECT {self._projection(session, "h", maximum)} AS value_json, h.version, '
            'f.record_key IS NOT NULL AS native_replay_passive FROM (SELECT ? AS record_key) k '
            'LEFT JOIN storage_records h ON h.namespace=? AND h.record_key=k.record_key '
            'LEFT JOIN storage_records f ON f.namespace=? AND f.record_key=k.record_key', (key, HEADERS, FENCES))
        if row['native_replay_passive']:
            _refuse('database_conflict')
        if row['version'] is None:
            return None
        value = self._decode(row['value_json'], task, maximum)
        version = row['version']
        if type(version) is not int or not 0 < version <= (1 << 63) - 1:
            _refuse('database_integrity')
        return {'ok': True, 'version': version, 'value': value}

    def _capture_write_prefix(self, session, namespace, key):
        from lib.storage_authority_sql.native_checkpoint_outbox import CheckpointRecordingSession
        if isinstance(session, CheckpointRecordingSession):
            row = session.fetch_one(
                f'SELECT {self._projection(session, "r", self.document_bytes)} AS value_json, '
                'r.version, r.updated_at_ms FROM storage_records r WHERE r.namespace=? AND r.record_key=?',
                (namespace, key))
            if row is not None and row['value_json'] is None:
                _refuse('database_integrity')

    def delete_snapshot(self, session, key):
        self._capture_write_prefix(session, SNAPSHOTS, key)
        session.execute('DELETE FROM storage_records WHERE namespace=? AND record_key=?', (SNAPSHOTS, key))

    def _write(self, session, namespace, key, encoded, version, now):
        self._capture_write_prefix(session, namespace, key)
        affected = session.execute(
            'INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) '
            'VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET '
            'value_json=excluded.value_json, version=excluded.version, updated_at_ms=excluded.updated_at_ms',
            (namespace, key, encoded, version, now))
        if affected != 1:
            _refuse('database_integrity')

    def dispatch(self, request, deadline_at):
        if isinstance(request, dict) and request.get('operation') in ('goal_claim', 'goal_handoff'):
            from lib.storage_authority_sql.native_checkpoint_goal import execute
            return execute(self, request, deadline_at)
        fields = {'format', 'operation', 'tenant_id', 'owner_user_id', 'task_id',
                  'header_bytes', 'snapshot_bytes', 'expected_version'}
        operation = request.get('operation') if isinstance(request, dict) else None
        allowed = fields | ({'header', 'snapshot'} if operation in ('cas', 'handoff') else set())
        if operation == 'handoff':
            allowed |= {'source', 'resume'}
        if (not isinstance(request, dict) or not fields <= request.keys() or not request.keys() <= allowed
                or request['format'] != SPEC['callback_format']
                or operation not in ('header', 'snapshot', 'cas', 'handoff')
                or type(request['tenant_id']) is not int or request['tenant_id'] != self.tenant
                or type(request['owner_user_id']) is not int or request['owner_user_id'] != self.owner):
            _refuse()
        task = request['task_id']
        if not isinstance(task, str) or not task or len(task.encode()) > self.identity_bytes:
            _refuse()
        header_bytes, snapshot_bytes, expected = (request[name] for name in
                                                  ('header_bytes', 'snapshot_bytes', 'expected_version'))
        if (any(type(n) is not int or not 0 < n <= self.document_bytes for n in (header_bytes, snapshot_bytes))
                or type(expected) is not int or not 0 <= expected <= (1 << 63) - 1):
            _refuse()
        key = self.key(task)
        if operation == 'handoff':
            from lib.storage_authority_sql.native_checkpoint_handoff import handoff
            return handoff(self, request, deadline_at)
        if operation == 'header':
            if expected != 0:
                _refuse()
            return self.backend.query('native_checkpoint.header', lambda session:
                self._header(session, task, key, header_bytes) or {'ok': True, 'version': 0}, deadline_at)
        if operation == 'snapshot':
            def snapshot(session):
                # One SQL snapshot covers the header version and saved body, even
                # at PostgreSQL READ COMMITTED isolation.
                row = session.fetch_one(
                    f'SELECT {self._projection(session, "h", header_bytes)} AS header_json, '
                    f'{self._projection(session, "s", snapshot_bytes)} AS snapshot_json, '
                    'h.version AS header_version, s.version AS snapshot_version '
                    ', f.record_key IS NOT NULL AS native_replay_passive '
                    'FROM storage_records h LEFT JOIN storage_records s '
                    'ON s.namespace=? AND s.record_key=h.record_key '
                    'LEFT JOIN storage_records f ON f.namespace=? AND f.record_key=h.record_key '
                    'WHERE h.namespace=? AND h.record_key=?', (SNAPSHOTS, FENCES, HEADERS, key))
                if row is not None and row['native_replay_passive']:
                    _refuse('database_conflict')
                if row is None or row['header_version'] != expected:
                    return {'ok': False, 'error': 'conflict'}
                self._decode(row['header_json'], task, header_bytes)
                if row['snapshot_version'] is None:
                    return {'ok': True, 'version': expected}
                if not 0 < row['snapshot_version'] <= expected:
                    _refuse('database_integrity')
                return {'ok': True, 'version': expected,
                        'value': self._decode(row['snapshot_json'], task, snapshot_bytes)}
            return self.backend.query('native_checkpoint.snapshot', snapshot, deadline_at)
        if expected == (1 << 63) - 1 or 'header' not in request:
            _refuse()
        self._value(request['header'], task, header_bytes)
        replacements = [(HEADERS, JsonBytes(orjson.dumps(self._envelope(task, request['header']))))]
        if 'snapshot' in request:
            self._value(request['snapshot'], task, snapshot_bytes)
            replacements.append((SNAPSHOTS, JsonBytes(orjson.dumps(self._envelope(task, request['snapshot'])))))

        def compare_exchange(session):
            session.lock_key(HEADERS, key)
            actual = self._header(session, task, key, header_bytes)
            if (actual['version'] if actual else 0) != expected:
                return {'ok': False, 'error': 'conflict'}
            now = max(1, time.time_ns() // 1_000_000)
            for namespace, value in replacements:
                self._write(session, namespace, key, value, expected + 1, now)
            return {'ok': True, 'version': expected + 1}

        return self.command(request, compare_exchange, deadline_at)
