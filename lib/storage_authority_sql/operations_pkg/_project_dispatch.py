"""Atomic segmented project dispatch persistence on the selected SQL authority.

Entry points are the four project.dispatch operations. Reuse versioned document
CAS/codec and the enclosing operation transaction. The project head lock always
precedes child locks. Domain authorization and index contents belong to the
project dispatch repository; this owner enforces scope, bounds and atomicity.
"""
from __future__ import annotations

import hashlib
import re

from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC
from ._common import _dump, _integer, _required_text
from ._records import _record_get_owned, _record_list_owned, _record_put_owned, _record_delete_owned

HEADS = SPEC['namespaces']['heads']
DOCUMENTS = SPEC['namespaces']['documents']
LIMITS = SPEC['limits']
_ID = re.compile(r'[A-Za-z0-9._:-]*\Z')


def _invalid(message='Invalid project dispatch storage request'):
    raise StorageError('database_protocol_error', message)


def _owner(payload):
    return _integer(payload, 'user_id', minimum=1, maximum=2**63 - 1)


def _scope(payload):
    owner = _owner(payload)
    project = _required_text(payload, 'project_path', LIMITS['projectPathCharacters'])
    if not project.strip() or '\x00' in project:
        _invalid()
    return owner, project, f'{owner}:{hashlib.sha256(project.encode("utf-8")).hexdigest()}'


def _identity(value, *, empty=False):
    if (not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= LIMITS['identityCharacters']
            or not _ID.fullmatch(value)):
        _invalid()
    return value


def _collection(payload):
    value = payload.get('collection')
    if value not in SPEC['collections']:
        _invalid()
    return value


def _revision(payload, field):
    return _integer(payload, field, minimum=0, maximum=LIMITS['maximumRevision'])


def _document(value, maximum):
    if not isinstance(value, dict):
        _invalid('Dispatch document must be an object')
    size = len(_dump(value))
    if size > maximum:
        _invalid('Dispatch document exceeds transport bound')
    return size


def get(session, payload):
    _, _, project_key = _scope(payload)
    identity = _identity(payload.get('id'))
    if payload.get('collection') == 'head':
        if identity != 'root':
            _invalid()
        namespace, key = HEADS, project_key
    else:
        namespace, key = DOCUMENTS, f'{project_key}:{_collection(payload)}:{identity}'
    return _record_get_owned(session, {'namespace': namespace, 'key': key})


def _page(session, payload, namespace, prefix, include_value):
    limit = _integer(payload, 'limit', default=LIMITS['pageRows'], minimum=1, maximum=LIMITS['pageRows'])
    reverse = payload.get('reverse', False)
    if type(reverse) is not bool:
        _invalid('Dispatch ordering must be boolean')
    after = payload.get('after_key', '')
    if (not isinstance(after, str) or len(after) > 512 or not _ID.fullmatch(after)
            or (after and not after.startswith(prefix))):
        _invalid('Dispatch cursor differs from owned prefix')
    return _record_list_owned(session, {'namespace': namespace, 'prefix': prefix,
        'before_key' if reverse else 'after_key': after, 'limit': limit,
        'include_value': include_value}, binary_order=True, reverse=reverse)


def list_documents(session, payload):
    _, _, key = _scope(payload)
    collection = _collection(payload)
    prefix = _identity(payload.get('prefix', ''), empty=True)
    return _page(session, payload, DOCUMENTS, f'{key}:{collection}:{prefix}', collection == 'indexes')


def projects(session, payload):
    return _page(session, payload, HEADS, f'{_owner(payload)}:', True)


def commit(session, payload):
    owner, project, key = _scope(payload)
    expected = _revision(payload, 'expected_revision')
    head = payload.get('head')
    size = _document(head, LIMITS['headBytes'])
    changes = payload.get('changes')
    if not isinstance(changes, list) or len(changes) > LIMITS['commitChanges']:
        _invalid()
    prepared, seen = [], set()
    for change in changes:
        if not isinstance(change, dict):
            _invalid()
        collection = _collection(change)
        identity = _identity(change.get('id'))
        child_key = f'{key}:{collection}:{identity}'
        if child_key in seen:
            _invalid('Duplicate dispatch document identity')
        seen.add(child_key)
        deleting = change.get('delete') is True
        if deleting:
            if collection != 'indexes' or 'value' in change:
                _invalid('Only reconstructible dispatch indexes can be deleted')
        elif 'delete' in change:
            _invalid()
        else:
            size += _document(change.get('value'), LIMITS['indexBytes'] if collection == 'indexes' else LIMITS['documentBytes'])
        if size > LIMITS['commitBytes']:
            _invalid('Dispatch commit exceeds transport bound')
        prepared.append((deleting, {'namespace': DOCUMENTS, 'key': child_key,
            'expected_version': _revision(change, 'expected_version'), 'value': change.get('value')}))

    # All validation precedes mutations. The shared operation transaction rolls
    # back the head too when any later child CAS fails or the receipt fails.
    result = _record_put_owned(session, {'namespace': HEADS, 'key': key,
        'expected_version': expected, 'value': {'format': SPEC['format'],
        'owner': owner, 'projectPath': project, 'state': head}})
    for deleting, row in sorted(prepared, key=lambda item: item[1]['key']):
        if deleting:
            # Generic delete permits absent rows at every expected version;
            # this semantic CAS requires the exact witnessed absence/version.
            current = _record_get_owned(session, row)
            if (current['version'] if current else 0) != row['expected_version']:
                raise StorageError('database_conflict', 'Dispatch index version conflict')
            _record_delete_owned(session, row)
        else:
            _record_put_owned(session, row)
    return result
