"""SQL checkpoint capture and passive replay inside the selected transaction.

The shared Rust authority still decides all transitions. This port records the
ordered SQL observations, locks and mutations needed to verify their prefix;
replay cannot manufacture an active checkpoint or overwrite an active actor.
"""
import hashlib
import time
import uuid

import orjson

from lib.storage.errors import StorageError
from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
from lib.storage_authority_sql.json_document import JsonBytes
from lib.storage_authority_sql.logical_outbox import LogicalMutationRecordingSession, _wire_value
from lib.storage_authority_sql.operations_pkg._common import _load
from lib.storage_authority_sql.adapters.text_size import json_utf8_length_expression, replay_length_expressions

REPLAY = SPEC['logical_replay']
HEADERS = SPEC['namespaces']['headers']
FENCES = SPEC['namespaces']['replay_fences']
METHODS = ('fetch_one', 'fetch_all', 'fetch_one_for_update', 'fetch_one_for_update_skip_locked')


def _refuse(message):
    raise StorageError('database_conflict', 'Native checkpoint replay ' + message)


def observation_digest(value, params=()):
    def document(column, item):
        decoded = _load(item)
        if (column in ('value_json', 'header_json') and HEADERS in params
                and isinstance(decoded, dict) and decoded.get('format') == REPLAY['passive_envelope_format']):
            if set(decoded) != {'format', 'tenant_id', 'owner_user_id', 'task_id', 'value'}:
                _refuse('passive envelope is invalid')
            key = hashlib.sha256(orjson.dumps([decoded['tenant_id'], decoded['owner_user_id'], decoded['task_id']])).hexdigest()
            if key in params:
                decoded = {**decoded, 'format': SPEC['callback_format']}
        return decoded
    def row(value):
        if value is None:
            return None
        return {key: (document(key, item) if key in REPLAY['json_columns'] and item is not None
                      else int(item) if isinstance(item, bool) else item)
                for key, item in value.items() if key != REPLAY['passive_column']}
    normalized = [row(item) for item in value] if isinstance(value, list) else row(value)
    return hashlib.sha256(orjson.dumps(_wire_value(normalized), option=orjson.OPT_SORT_KEYS)).hexdigest()


class CheckpointRecordingSession(LogicalMutationRecordingSession):
    """Record bounded SQL reads at their actual positions among mutations."""
    def __init__(self, session):
        super().__init__(session)
        self.steps = []

    def lock_key(self, namespace, key):
        super().lock_key(namespace, key)
        self.steps.append({'kind': 'lock', 'namespace': namespace, 'key': key})

    def execute(self, sql, params=()):
        result = super().execute(sql, params)
        self.steps.append({'kind': 'mutation', 'index': len(self.mutations)-1})
        return result

    def execute_many_exact(self, sql, params):
        start = len(self.mutations)
        result = super().execute_many_exact(sql, params)
        self.steps.extend({'kind': 'mutation', 'index': index} for index in range(start, len(self.mutations)))
        return result

    def _read(self, method, sql, params):
        result = getattr(self._session, method)(sql, params)
        self.steps.append({'kind': 'read', 'method': method, 'sql': sql,
                           'params': _wire_value(list(params)), 'digest': observation_digest(result, params)})
        return result

    def fetch_one(self, sql, params=()):
        return self._read('fetch_one', sql, params)

    def fetch_all(self, sql, params=()):
        return self._read('fetch_all', sql, params)

    def fetch_one_for_update(self, sql, params=()):
        return self._read('fetch_one_for_update', sql, params)

    def fetch_one_for_update_skip_locked(self, sql, params=()):
        return self._read('fetch_one_for_update_skip_locked', sql, params)


def command(rows, request, callback, deadline_at):
    operation = 'native_checkpoint.' + request['operation']
    pipeline = rows.logical_outbox
    if pipeline is None or not pipeline.policy.capture_enabled:
        return rows.backend.command(operation, '', None, 'user', callback, deadline_at,
                                    receipt_required=False, join_on_timeout=True)
    if not pipeline.capture_enabled:
        _refuse('capture is unavailable')
    request_id = uuid.uuid4().hex
    digest = hashlib.sha256(orjson.dumps(request, option=orjson.OPT_SORT_KEYS)).hexdigest()
    committed = None

    def capture(session):
        nonlocal committed
        committed = None
        recorder = CheckpointRecordingSession(session)
        response = callback(recorder)
        if any(mutation['rowcount'] > 0 for mutation in recorder.mutations):
            committed = pipeline.capture(session, operation=operation, request_id=request_id,
                request_digest=digest, command_id=None,
                payload={'tenant_id': str(rows.tenant), 'owner_user_id': rows.owner,
                         'native_checkpoint_replay': {'format': REPLAY['format'],
                             'backend': session.backend, 'steps': recorder.steps,
                             'header_bytes': request['header_bytes'], 'identity_bytes': rows.identity_bytes}},
                response=response, mutations=recorder.mutations)
        return response

    response = rows.backend.command(operation, digest, None, 'user', capture, deadline_at,
                                   receipt_required=False, join_on_timeout=True)
    pipeline.notify(committed)
    return response


def replay(session, record, document, mutations):
    """Apply an authenticated trace and its passive fences in one transaction."""
    from lib.storage_authority_sql.logical_replay import _unwire_value, apply_mutation
    request = document['request']
    proof = request.get('native_checkpoint_replay')
    if (record.get('operation') not in {'native_checkpoint.' + name for name in REPLAY['operations']}
            or request.get('tenant_id') != record.get('tenant_id')
            or request.get('owner_user_id') != record.get('owner_user_id')
            or not isinstance(proof, dict) or set(proof) != {'format', 'backend', 'steps', 'header_bytes', 'identity_bytes'}
            or proof['format'] != REPLAY['format'] or proof['backend'] not in ('sqlite', 'postgres')
            or any(type(proof[name]) is not int or not 0 < proof[name] < 1 << 60 for name in ('header_bytes', 'identity_bytes'))
            or not isinstance(proof['steps'], list)):
        _refuse('contract differs')
    # Validate the complete program before its first effect, including exact
    # once-only coverage of mutations. The enclosing Backend owns rollback.
    next_index = 0
    for step in proof['steps']:
        if not isinstance(step, dict):
            _refuse('step is invalid')
        kind = step.get('kind')
        if kind == 'mutation':
            if set(step) != {'kind', 'index'} or type(step['index']) is not int or step['index'] != next_index:
                _refuse('mutation order differs')
            next_index += 1
        elif kind == 'lock':
            if set(step) != {'kind', 'namespace', 'key'} or any(not isinstance(step[k], str) for k in ('namespace', 'key')):
                _refuse('lock is invalid')
        elif kind == 'read':
            if (set(step) != {'kind', 'method', 'sql', 'params', 'digest'} or step['method'] not in METHODS
                    or not isinstance(step['sql'], str) or not step['sql'].lstrip().lower().startswith('select ')
                    or not isinstance(step['params'], list) or not isinstance(step['digest'], str)):
                _refuse('observation is invalid')
        else:
            _refuse('step kind is unsupported')
    if next_index != len(mutations):
        _refuse('mutation coverage differs')
    private_keys = set()
    for step in proof['steps']:
        if step['kind'] == 'lock':
            session.lock_key(step['namespace'], step['key'])
            if step['namespace'] == HEADERS:
                key = step['key']
                if key not in private_keys:
                    _check_passive_prefix(session, key, record['stream_id'])
                    private_keys.add(key)
        elif step['kind'] == 'read':
            sql = replay_length_expressions(step['sql'], proof['backend'], session.backend)
            params = tuple(_unwire_value(step['params']))
            value = getattr(session, step['method'])(sql, params)
            if observation_digest(value, params) != step['digest']:
                _refuse('observation prefix differs')
        else:
            apply_mutation(session, mutations[step['index']])
    if not private_keys:
        _refuse('has no private actor fence')
    for key in sorted(private_keys):
        _mark_passive_header(session, key, proof)
        encoded = JsonBytes(orjson.dumps({'format': REPLAY['format'], 'stream_id': record['stream_id'],
                                         'sequence': record['sequence']}))
        session.execute('INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) '
            'VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET '
            'value_json=excluded.value_json, version=excluded.version, updated_at_ms=excluded.updated_at_ms',
            (FENCES, key, encoded, record['sequence'], max(1, time.time_ns()//1_000_000)))
    return len(mutations)


def _mark_passive_header(session, key, proof):
    # The envelope itself fences older controllers that do not know the extra
    # namespace. Replay hashes its logical source form, never grants a lease.
    bound = (proof['header_bytes'] + proof['identity_bytes']) * 6 + SPEC['envelope_bytes']
    length = json_utf8_length_expression(session.backend, 'value_json')
    row = session.fetch_one(f'SELECT CASE WHEN {length}<=? THEN value_json ELSE NULL END AS value_json '
                            'FROM storage_records WHERE namespace=? AND record_key=?', (bound, HEADERS, key))
    if row is None:
        return
    value = _load(row['value_json'])
    if not isinstance(value, dict) or value.get('format') not in (SPEC['callback_format'], REPLAY['passive_envelope_format']):
        _refuse('header envelope is invalid')
    value = {**value, 'format': REPLAY['passive_envelope_format']}
    if session.execute('UPDATE storage_records SET value_json=? WHERE namespace=? AND record_key=?',
                       (JsonBytes(orjson.dumps(value)), HEADERS, key)) != 1:
        _refuse('header fence was lost')


def _check_passive_prefix(session, key, stream_id):
    bound = json_utf8_length_expression(session.backend, 'value_json')
    rows = session.fetch_all('SELECT namespace, '
        f'CASE WHEN namespace=? AND {bound}<=4096 THEN value_json ELSE NULL END AS value_json FROM storage_records '
        'WHERE record_key=? AND namespace IN (?, ?, ?)',
        (FENCES, key, HEADERS, SPEC['namespaces']['snapshots'], FENCES))
    fence = next((_load(row['value_json']) for row in rows if row['namespace'] == FENCES), None)
    if fence is None:
        if rows:
            _refuse('target contains an unfenced actor')
    elif not isinstance(fence, dict) or fence.get('format') != REPLAY['format'] or fence.get('stream_id') != stream_id:
        _refuse('target belongs to another stream')
