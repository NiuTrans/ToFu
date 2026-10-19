"""Logical paper documents, durable build generations and reading-position CAS.

Source bindings locate an opaque document ID without reading its current filesystem.
Build publication and the retained artifact pin share one transaction. Browser
reading state has a separate revision so stale tabs cannot overwrite a newer save.
"""
from __future__ import annotations

import time
import secrets
import hashlib
import json
from collections.abc import Mapping

from lib.paper.builds.contract import limit, validate
from lib.paper.builds.document_identity import source_binding_key
from lib.paper.builds.errors import BuildError
from lib.storage.errors import StorageError
from ._common import _integer, _required_text
from ._paper_artifacts import latest_retained_receipt, read_retained_receipt, retain_build
from ._records import _record_get, _record_put

_DOCUMENT = 'paper.document.v1'
_SOURCE = 'paper.document.source.v1'
_CURRENT = 'paper.document.current.v1'
_BUDGET = 'paper.document.budget.v1'


def _get(session, namespace, key):
    row = _record_get(session, {'namespace': namespace, 'key': key})
    return row['value'] if row else None


def _put(session, namespace, key, value):
    _record_put(session, {'namespace': namespace, 'key': key, 'value': value})


def _validated(value, definition):
    try:
        return validate(value, definition)
    except BuildError as exc:
        raise StorageError('database_protocol_error', str(exc)) from exc


def _owner(payload):
    return _integer(payload, 'user_id', minimum=1)


def _document_key(owner, document_id):
    import re
    if not isinstance(document_id, str) or not re.fullmatch(r'[0-9a-f]{64}', document_id):
        raise StorageError('database_protocol_error', 'Invalid logical paper document identity')
    return f'{owner}:{document_id}'


def _document(session, owner, document_id, *, lock=False):
    key = _document_key(owner, document_id)
    if lock:
        session.lock_key(_DOCUMENT, key)
    row = _get(session, _DOCUMENT, key)
    if not isinstance(row, Mapping):
        raise StorageError('database_not_found', 'Paper document is unavailable')
    return key, dict(row)


def _projection(session, owner, row):
    receipt = latest_retained_receipt(session, user_id=owner, document_id=row['document_id'])
    status = row['status']
    if status == 'pending' and row['request_expires_at_ms'] <= int(time.time() * 1000):
        status = 'interrupted'
    return {'document_id': row['document_id'], 'source': row['source'], 'generation': row['generation'],
            'status': status, 'receipt': receipt, 'reading': row['reading'],
            'reading_revision': row['reading_revision']}


def _paper_document_register(session, payload):
    owner = _owner(payload)
    source = dict(_validated(payload.get('source'), 'PaperStoredSourceV1'))
    if source['source'] == 'research':
        source = _research_source(session, owner, source)
    supplied = source.pop('document_id', None)
    binding = source_binding_key(source)
    source_key = f'{owner}:{binding}'
    session.lock_key(_BUDGET, 'capacity')
    existing = _get(session, _SOURCE, source_key)
    if existing:
        if supplied and existing['document_id'] != supplied:
            raise StorageError('database_conflict', 'Paper document does not match this source binding')
        return _document(session, owner, existing['document_id'])[1]
    if supplied and source['source'] != 'research':
        raise StorageError('database_conflict', 'Paper document source binding changed; select the source again')
    budget = _get(session, _BUDGET, 'capacity') or {'count': 0}
    document_id = supplied if source['source'] == 'research' else secrets.token_hex(32)
    # An existing retained Research ID cannot capture another source's catalog row.
    if _get(session, _DOCUMENT, _document_key(owner, document_id)) is not None:
        raise StorageError('database_conflict', 'Paper document already belongs to another source')
    source['document_id'] = document_id
    row = {'document_id': document_id, 'source': source, 'generation': 0, 'request_key': '',
           'request_expires_at_ms': 0, 'status': 'ready' if source['source'] == 'research' else 'idle',
           'reading': None, 'reading_revision': 0,
           'completed_build_id': None, 'completed_receipt_digest': None, 'completed_published': False}
    _put(session, _DOCUMENT, _document_key(owner, document_id), row)
    _put(session, _SOURCE, source_key, {'document_id': document_id})
    _put(session, _BUDGET, 'capacity', {**budget, 'count': budget['count'] + 1})
    return row


def _research_source(session, owner, source):
    """Only an owned workspace's retained publication can bind its existing ID.

    This lazy registration also adopts historical retained manuscripts without
    copying their bytes or changing the workspace publication authority.
    """
    from ._research_workspace import _research_workspace_get
    workspace = _research_workspace_get(session, {'user_id': owner,
        'paper_hash': source['paper_hash'], 'lang': source['lang']})
    compilation = ((workspace or {}).get('workspace') or {}).get('compilation') or {}
    reference = compilation.get('build')
    if not isinstance(reference, Mapping):
        raise StorageError('database_not_found', 'Research manuscript is unavailable')
    if source.get('document_id') and source['document_id'] != reference.get('document_id'):
        raise StorageError('database_conflict', 'Research manuscript identity changed')
    receipt = read_retained_receipt(session, user_id=owner,
        document_id=reference.get('document_id'), build_id=reference.get('build_id'))
    main = receipt['main_file']
    return {**source, 'document_id': receipt['document_id'], 'main_file': main, 'label': main}


def _require_build_authority(row):
    if row['source']['source'] == 'research':
        raise StorageError('database_conflict', 'Research workspace owns manuscript publication')


def _paper_document_resolve(session, payload):
    owner = _owner(payload)
    sources = payload.get('sources')
    if not isinstance(sources, list):
        raise StorageError('database_protocol_error', 'Paper source lookup requires an array')
    result = []
    for source in sources:
        _validated(source, 'PaperDocumentV1')
        row = _get(session, _SOURCE, f'{owner}:{source_binding_key(source)}')
        result.append(row['document_id'] if row else None)
    return result


def _paper_document_get(session, payload):
    owner = _owner(payload)
    _, row = _document(session, owner, payload.get('document_id'))
    return _projection(session, owner, row)


def _paper_document_current(session, payload):
    owner = _owner(payload)
    conversation_id = _required_text(payload, 'conversation_id', 160)
    pointer = _get(session, _CURRENT, f'{owner}:{conversation_id}')
    if pointer is None:
        return None
    _, row = _document(session, owner, pointer['document_id'])
    return _projection(session, owner, row)


def _paper_document_request(session, payload):
    owner = _owner(payload)
    conversation_id = _required_text(payload, 'conversation_id', 160)
    request_key = _required_text(payload, 'request_key', 64)
    session.lock_key(_BUDGET, 'capacity')
    current_key = f'{owner}:{conversation_id}'
    if _get(session, _CURRENT, current_key) is None:
        budget = _get(session, _BUDGET, 'capacity') or {'count': 0}
        _put(session, _BUDGET, 'capacity', {**budget, 'associations': budget.get('associations', 0) + 1})
    key, row = _document(session, owner, payload.get('document_id'), lock=True)
    _require_build_authority(row)
    if (row['status'] != 'pending' or row['request_key'] != request_key
            or row['request_expires_at_ms'] <= int(time.time() * 1000)):
        row.update(generation=row['generation'] + 1, request_key=request_key, status='pending',
                   request_expires_at_ms=int(time.time() * 1000) + limit('document_request_seconds') * 1000,
                   completed_build_id=None, completed_receipt_digest=None, completed_published=False)
        _put(session, _DOCUMENT, key, row)
    _put(session, _CURRENT, current_key, {'document_id': row['document_id']})
    return {'generation': row['generation'], 'request_key': row['request_key']}


def _paper_document_finish(session, payload):
    owner = _owner(payload)
    generation = _integer(payload, 'generation', minimum=1)
    receipt = _validated(payload.get('receipt'), 'PaperBuildReceiptV1')
    key, row = _document(session, owner, payload.get('document_id'), lock=True)
    _require_build_authority(row)
    if receipt['document_id'] != row['document_id'] or receipt['owner_user_id'] != owner:
        raise StorageError('database_protocol_error', 'Build receipt belongs to a different paper document')
    if (generation != row['generation'] or row['status'] == 'interrupted'
            or row['request_expires_at_ms'] <= int(time.time() * 1000)):
        return {'current': False, 'published': False}
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if row['completed_build_id'] is not None:
        if row['completed_build_id'] != receipt['build_id']:
            return {'current': False, 'published': False}
        if row['completed_receipt_digest'] != digest:
            raise StorageError('database_conflict', 'Completed paper build receipt changed')
        return {'current': True, 'published': row['completed_published']}
    if receipt['status'] == 'success':
        proof = payload.get('proof')
        if not isinstance(proof, Mapping) or proof.get('document_id') != row['document_id'] or proof.get('build_id') != receipt['build_id']:
            raise StorageError('database_protocol_error', 'A successful paper build requires retained artifact evidence')
        published = retain_build(session, user_id=owner, proof=proof)
        # Compare all receipt fields, not just the caller-supplied build ID.
        if published and read_retained_receipt(session, user_id=owner, document_id=row['document_id'],
                                              build_id=receipt['build_id']) != receipt:
            raise StorageError('database_conflict', 'Retained receipt differs from the completed build')
        row['status'] = 'ready'
    else:
        published = False
        row['status'] = 'failed'
    row.update(completed_build_id=receipt['build_id'], completed_receipt_digest=digest,
               completed_published=published)
    _put(session, _DOCUMENT, key, row)
    return {'current': True, 'published': published}


def _paper_document_abandon(session, payload):
    owner = _owner(payload)
    generation = _integer(payload, 'generation', minimum=1)
    key, row = _document(session, owner, payload.get('document_id'), lock=True)
    _require_build_authority(row)
    if generation == row['generation'] and row['status'] == 'pending':
        row['status'] = 'interrupted'
        _put(session, _DOCUMENT, key, row)
    return {'abandoned': row['status'] == 'interrupted'}


def _paper_document_reading(session, payload):
    owner = _owner(payload)
    expected = _integer(payload, 'expected_revision', minimum=0)
    request = _validated({name: payload.get(name) for name in (
        'expected_revision', 'state', 'writer_id', 'sequence')}, 'PaperReadingStateRequestV1')
    state = _validated(payload.get('state'), 'PaperReadingStateV1')
    try:
        encoded = json.dumps(state, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise StorageError('database_protocol_error', 'Reading position must contain finite JSON values') from exc
    if len(encoded) > limit('reading_state_bytes'):
        raise StorageError('database_protocol_error', 'Reading position exceeds its byte budget')
    key, row = _document(session, owner, payload.get('document_id'), lock=True)
    same_writer = (row.get('reading_writer_id') == request['writer_id']
                   and row.get('reading_writer_base_revision') == expected)
    if same_writer and request['sequence'] <= row['reading_writer_sequence']:
        if request['sequence'] == row['reading_writer_sequence'] and state != row['reading']:
            raise StorageError('database_conflict', 'Reading update sequence was reused with different content')
        return {'revision': row['reading_revision'], 'state': row['reading']}
    if not same_writer and row['reading_revision'] != expected:
        raise StorageError('database_conflict', 'Paper reading position changed in another viewer')
    read_retained_receipt(session, user_id=owner, document_id=row['document_id'], build_id=state['build_id'])
    row['reading'] = state
    row['reading_revision'] += 1
    row.update(reading_writer_id=request['writer_id'], reading_writer_base_revision=expected,
               reading_writer_sequence=request['sequence'])
    _put(session, _DOCUMENT, key, row)
    return {'revision': row['reading_revision'], 'state': state}
