"""Durable paper-build bytes, upload witnesses, read leases and document pins.

Entry points are registered in operation_domains.paper_artifacts. Research CAS
calls retain_research_build inside its transaction. Bounded immutable chunks use
storage_records so SQLite/Postgres backup and logical replay carry the bytes as
well as their references. No filesystem or application cache is an authority.
All mutations take the same resource-ledger lock; natural keys replace receipts
for chunk retries, avoiding a second durable copy of every upload request.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping

import orjson

from lib.paper.builds.contract import limit, validate
from lib.paper.builds.errors import BuildError
from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg._common import _integer, _required_text

_META = 'paper.build.artifact.v1'
_CHUNK = 'paper.build.chunk.v1'
_DOCUMENT = 'paper.build.document.v1'
_LEDGER = 'paper.build.budget.v1'
_NAMES = {'document.pdf', 'document.synctex.gz', 'sources.json', 'build.log'}


def _now_ms():
    return int(time.time() * 1000)


def _error(kind, message):
    raise StorageError(kind, message)


def _identity(payload):
    owner = _integer(payload, 'user_id', minimum=1)
    document = _required_text(payload, 'document_id', 64)
    build = _required_text(payload, 'build_id', 32)
    if not re.fullmatch(r'[0-9a-f]{32,64}', document) or not re.fullmatch(r'[0-9a-f]{32}', build):
        _error('database_protocol_error', 'Invalid paper build identity')
    return owner, document, build, f'{owner}:{document}:{build}'


def _token(payload, name):
    value = _required_text(payload, name, 32)
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        _error('database_protocol_error', 'Invalid paper artifact witness')
    return value


def _invalid_chunk_constant(value):
    raise ValueError('Invalid retained chunk JSON constant')


def _get(session, namespace, key):
    row = session.fetch_one('SELECT value_json FROM storage_records WHERE namespace = ? AND record_key = ?',
                            (namespace, key))
    if row is None:
        return None
    try:
        encoded = row['value_json']
        # orjson's scratch allocation for a 256 KiB base64 chunk is over 4 MiB.
        # The fixed two-string chunk shape uses the bounded stdlib decoder.
        result = dict(encoded) if isinstance(encoded, Mapping) else (
            json.loads(encoded, parse_constant=_invalid_chunk_constant)
            if namespace == _CHUNK else orjson.loads(encoded))
        if not isinstance(result, dict):
            raise ValueError('not a document')
        if namespace == _CHUNK and (set(result) != {'data', 'sha256'}
                or not all(isinstance(value, str) for value in result.values())):
            raise ValueError('invalid chunk shape')
        return result
    except (ValueError, TypeError) as exc:
        raise StorageError('database_integrity', 'Invalid retained paper artifact') from exc


def _put(session, namespace, key, value):
    encoded = orjson.dumps(value).decode()
    changed = session.execute('UPDATE storage_records SET value_json = ?, version = version + 1, '
                              'updated_at_ms = ? WHERE namespace = ? AND record_key = ?',
                              (encoded, _now_ms(), namespace, key))
    if not changed:
        session.execute('INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) '
                        'VALUES (?, ?, ?, ?, ?)', (namespace, key, encoded, 1, _now_ms()))


def _delete(session, namespace, key):
    session.execute('DELETE FROM storage_records WHERE namespace = ? AND record_key = ?', (namespace, key))


def _lock(session):
    session.lock_key(_LEDGER, 'capacity')


def _parts(receipt):
    size = limit('retained_chunk_bytes')
    # JSON Schema integers include integral floats; validation precedes this
    # arithmetic boundary, so range() receives the exact bounded byte count.
    return [(artifact['name'], offset, min(size, int(artifact['size_bytes']) - offset))
            for artifact in receipt['artifacts'] for offset in range(0, int(artifact['size_bytes']), size)]


def _metadata(session, key):
    value = _get(session, _META, key)
    if value is None:
        _error('database_not_found', 'Retained paper build is unavailable')
    return value


def _upload(session, payload):
    *_, key = _identity(payload)
    token = _token(payload, 'upload_id')
    value = _metadata(session, key)
    if value['upload_id'] != token:
        _error('database_conflict', 'Paper artifact upload witness differs')
    if value['state'] == 'reclaiming' or (value['state'] in {'uploading', 'sealed'}
                                         and value['expires_at_ms'] <= _now_ms()):
        _error('database_conflict', 'Paper artifact upload expired')
    return key, value


def _readers(value):
    now = _now_ms()
    return {key: expiry for key, expiry in value['readers'].items() if expiry > now}


def _paper_artifact_prepare(session, payload):
    owner, document, build, key = _identity(payload)
    upload_id = _token(payload, 'upload_id')
    receipt = payload.get('receipt')
    try:
        validate(receipt)
    except (BuildError, TypeError, ValueError) as exc:
        raise StorageError('database_protocol_error', 'Invalid retained build receipt') from exc
    if (receipt['owner_user_id'] != owner or receipt['document_id'] != document
            or receipt['build_id'] != build or receipt['status'] != 'success'
            or any(row['name'] not in _NAMES for row in receipt['artifacts'])):
        _error('database_protocol_error', 'Retained receipt does not match the successful build')
    receipt_bytes = len(orjson.dumps(receipt))
    payload_bytes = sum(row['size_bytes'] for row in receipt['artifacts'])
    if receipt_bytes > limit('retained_receipt_bytes'):
        _error('database_protocol_error', 'Retained paper build exceeds its byte limit')
    _lock(session)
    existing = _get(session, _META, key)
    if existing is not None:
        _, existing = _upload(session, payload)
        if existing['receipt'] != receipt:
            _error('database_conflict', 'An immutable paper build cannot be replaced')
        return {'next_sequence': existing['next_sequence'], 'state': existing['state']}
    ledger = _get(session, _LEDGER, 'capacity') or {'records': {}}
    reserved = payload_bytes + receipt_bytes
    ledger['records'][key] = reserved
    _put(session, _LEDGER, 'capacity', ledger)
    _put(session, _META, key, {'receipt': receipt, 'upload_id': upload_id, 'state': 'uploading',
         'next_sequence': 0, 'readers': {},
         'expires_at_ms': _now_ms() + limit('retained_upload_seconds') * 1000})
    return {'next_sequence': 0, 'state': 'uploading'}


def _paper_artifact_append(session, payload):
    sequence = _integer(payload, 'sequence', minimum=0)
    encoded = _required_text(payload, 'data', 4 * ((limit('retained_chunk_bytes') + 2) // 3))
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise StorageError('database_protocol_error', 'Invalid paper artifact chunk encoding') from exc
    _lock(session)
    key, value = _upload(session, payload)
    parts = _parts(value['receipt'])
    if sequence >= len(parts) or len(data) != parts[sequence][2]:
        _error('database_protocol_error', 'Paper artifact chunk has an invalid position or length')
    chunk = {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
    if sequence < value['next_sequence']:
        if _get(session, _CHUNK, f'{key}:{sequence}') != chunk:
            _error('database_conflict', 'Paper artifact chunk retry differs from its committed bytes')
        return {'next_sequence': value['next_sequence']}
    if sequence != value['next_sequence'] or value['state'] != 'uploading':
        _error('database_conflict', 'Paper artifact chunks must arrive in sequence')
    _put(session, _CHUNK, f'{key}:{sequence}', chunk)
    value['next_sequence'] += 1
    _put(session, _META, key, value)
    return {'next_sequence': value['next_sequence']}


def _chunk_bytes(session, key, sequence):
    chunk = _get(session, _CHUNK, f'{key}:{sequence}')
    try:
        data = base64.b64decode(chunk['data'], validate=True)
        if hashlib.sha256(data).hexdigest() != chunk['sha256']:
            raise ValueError('digest mismatch')
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise StorageError('database_integrity', 'Retained paper artifact chunk is missing or corrupt') from exc


def _paper_artifact_seal(session, payload):
    _lock(session)
    key, value = _upload(session, payload)
    if value['state'] != 'uploading':
        return {'sealed': True}
    parts = _parts(value['receipt'])
    if value['next_sequence'] != len(parts):
        _error('database_conflict', 'Paper artifact upload is incomplete')
    hashes = {row['name']: hashlib.sha256() for row in value['receipt']['artifacts']}
    for sequence, (name, _, size) in enumerate(parts):
        data = _chunk_bytes(session, key, sequence)
        if len(data) != size:
            _error('database_integrity', 'Retained artifact size changed')
        hashes[name].update(data)
    if any(hashes[row['name']].hexdigest() != row['sha256'] for row in value['receipt']['artifacts']):
        _error('database_integrity', 'Retained artifact digest differs from the build receipt')
    value['state'] = 'sealed'
    _put(session, _META, key, value)
    return {'sealed': True}


def retain_research_build(session, *, user_id, reference, proof):
    """Pin an exact verified receipt in the same transaction as workspace CAS."""
    if not isinstance(reference, Mapping) or not isinstance(proof, Mapping):
        _error('database_protocol_error', 'A retained build requires its upload witness')
    payload = {**proof, 'user_id': user_id}
    owner, document, build, key = _identity(payload)
    _lock(session)
    _, value = _upload(session, payload)
    receipt = value['receipt']
    pdf = next((row for row in receipt['artifacts'] if row['name'] == 'document.pdf'), None)
    if (value['state'] not in {'sealed', 'pinned'} or not receipt['version_verified'] or pdf is None
            or reference.get('document_id') != document or reference.get('build_id') != build
            or reference.get('manifest_digest') != receipt['source_digest']
            or reference.get('pdf_sha256') != pdf['sha256']):
        _error('database_conflict', 'Research reference differs from its retained verified build')
    return retain_build(session, user_id=user_id, proof=proof)


def read_retained_receipt(session, *, user_id, document_id, build_id):
    """Read owned immutable metadata; no source path or compilation is involved."""
    *_, key = _identity({'user_id': user_id, 'document_id': document_id, 'build_id': build_id})
    value = _metadata(session, key)
    if value['state'] not in {'pinned', 'retired'}:
        _error('database_not_found', 'Retained paper build is unavailable')
    return value['receipt']


def latest_retained_receipt(session, *, user_id, document_id):
    pointer = _paper_artifact_latest(session, {'user_id': user_id, 'document_id': document_id})
    return (read_retained_receipt(session, user_id=user_id, document_id=document_id, build_id=pointer['build_id'])
            if pointer else None)


def retain_build(session, *, user_id, proof):
    """Publish an owned sealed build; an unverified result cannot displace proof.

The caller owns its domain revision fence and surrounding transaction. Research
adds its exact source/PDF witness check; chat uses its durable requested generation.
"""
    payload = {**proof, 'user_id': user_id}
    owner, document, build, key = _identity(payload)
    _lock(session)
    _, value = _upload(session, payload)
    if value['state'] not in {'sealed', 'pinned'}:
        _error('database_conflict', 'Only a sealed paper build can be retained')
    document_key = f'{owner}:{document}'
    prior = _get(session, _DOCUMENT, document_key)
    if prior is not None and prior['build_id'] != build:
        prior_key = f'{document_key}:{prior["build_id"]}'
        previous = _metadata(session, prior_key)
        if previous['receipt']['version_verified'] and not value['receipt']['version_verified']:
            return False
        previous['state'] = 'retired'
        previous['expires_at_ms'] = _now_ms() + limit('viewer_lease_seconds') * 1000
        _put(session, _META, prior_key, previous)
    value['state'] = 'pinned'
    value['expires_at_ms'] = 0
    _put(session, _META, key, value)
    _put(session, _DOCUMENT, document_key, {'build_id': build})
    return True


def _paper_artifact_discard(session, payload):
    _lock(session)
    *_, key = _identity(payload)
    token = _token(payload, 'upload_id')
    value = _get(session, _META, key)
    if value is not None and value['upload_id'] == token and value['state'] in {'uploading', 'sealed'}:
        value['expires_at_ms'] = 0
        _put(session, _META, key, value)
    # A lost workspace acknowledgement must never unpin its committed PDF.
    return {'discarded': True}


def _paper_artifact_lease(session, payload):
    *_, key = _identity(payload)
    lease_id = _token(payload, 'lease_id')
    operation = payload.get('operation')
    if operation not in {'retain', 'renew', 'release'}:
        _error('database_protocol_error', 'Invalid retained artifact lease operation')
    _lock(session)
    value = _get(session, _META, key)
    if value is None:
        if operation == 'release':
            return {'retained': False}
        _error('database_not_found', 'Retained paper build is unavailable')
    readers = _readers(value)
    if operation == 'release':
        readers.pop(lease_id, None)
    else:
        if value['state'] not in {'pinned', 'retired'} or (
                value['state'] == 'retired' and value['expires_at_ms'] <= _now_ms() and not readers):
            _error('database_not_found', 'Retained paper build is unavailable')
        if operation == 'renew' and lease_id not in readers:
            _error('database_not_found', 'Retained artifact reader expired')
        readers[lease_id] = _now_ms() + limit('viewer_lease_seconds') * 1000
    value['readers'] = readers
    _put(session, _META, key, value)
    return {'retained': lease_id in readers, 'receipt': value['receipt']}


def _paper_artifact_read(session, payload):
    *_, key = _identity(payload)
    lease_id = _token(payload, 'lease_id')
    sequence = _integer(payload, 'sequence', minimum=0)
    value = _metadata(session, key)
    if lease_id not in _readers(value):
        _error('database_not_found', 'Retained artifact reader expired')
    if sequence >= value['next_sequence']:
        _error('database_protocol_error', 'Retained artifact sequence is out of bounds')
    return {'data': base64.b64encode(_chunk_bytes(session, key, sequence)).decode()}


def _paper_artifact_latest(session, payload):
    owner = _integer(payload, 'user_id', minimum=1)
    document = _required_text(payload, 'document_id', 64)
    if not re.fullmatch(r'[0-9a-f]{32,64}', document):
        _error('database_protocol_error', 'Invalid paper document identity')
    return _get(session, _DOCUMENT, f'{owner}:{document}')


def _paper_artifact_prune(session, payload):
    owner = _integer(payload, 'user_id', minimum=1)
    _lock(session)
    ledger = _get(session, _LEDGER, 'capacity') or {'records': {}}
    cursor_key = f'gc:{owner}'
    cursor = _get(session, _LEDGER, cursor_key) or {'key': '', 'inclusive': False}
    if not isinstance(cursor, dict) or not isinstance(cursor.get('inclusive'), bool):
        _error('database_protocol_error', 'Invalid retained artifact reclamation cursor')
    prefix = f'{owner}:'
    saved = cursor.get('key', '')
    if saved and (not isinstance(saved, str) or not saved.startswith(prefix)):
        _error('database_protocol_error', 'Invalid retained artifact reclamation cursor')
    rows = [key for key in sorted(ledger['records'])
            if key.startswith(prefix) and (key > saved or
                (cursor.get('inclusive') and key == saved))]
    page = rows[:limit('retained_gc_scan_rows')]
    for key in page:
        value = _metadata(session, key)
        if value['state'] == 'pinned' or value['expires_at_ms'] > _now_ms() or _readers(value):
            continue
        value['state'] = 'reclaiming'
        begin = value.get('gc_sequence', 0)
        end = min(value['next_sequence'], begin + limit('retained_gc_chunks'))
        for sequence in range(begin, end):
            _delete(session, _CHUNK, f'{key}:{sequence}')
        if end == value['next_sequence']:
            _delete(session, _META, key)
            del ledger['records'][key]
            _put(session, _LEDGER, 'capacity', ledger)
        else:
            value['gc_sequence'] = end
            _put(session, _META, key, value)
        _put(session, _LEDGER, cursor_key,
             {'key': key, 'inclusive': end < value['next_sequence']})
        return {'progress': True, 'chunks_deleted': end - begin}
    full_page = len(page) == limit('retained_gc_scan_rows')
    if full_page:
        _put(session, _LEDGER, cursor_key, {'key': page[-1], 'inclusive': False})
    else:
        _delete(session, _LEDGER, cursor_key)
    return {'progress': full_page, 'chunks_deleted': 0}
