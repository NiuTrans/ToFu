"""Offline recoverable-trash import, before any active-domain verification.

A private owner-scoped stage survives interruption; native finish seals committed
history before publishing its range pin. Source rows use exact primary-key prefix
reads, and checkpoint progress follows independent pinned header/Turn/page checks.
"""
from __future__ import annotations

import hashlib

from lib.storage_sidecar import tofudb_migration_import as m
from lib.storage_sidecar.tofudb_migration_transcript import frozen_snapshot
from lib.storage_sidecar.generated_tofudb_migration import MAX_MIGRATION_TRASH_TURNS


def _header_table():
    table = m._plan_table('trash', 'storage_conversation_trash')
    return {**table, 'columns': [c for c in table['columns'] if c != 'messages_json']}


def _turn_table():
    return m._plan_table('trash', 'storage_conversation_trash_turns')


def _turn(row):
    document = m._turn_document({**row, 'presentation_id': '', 'current_attempt_id': None})
    if document['status'] in {'pending', 'running'}:
        raise m.MigrationImportError('trash source contains a live Turn')
    return document


def _turns(source, conversation_id, owner):
    table = _turn_table()
    if table['name'] not in source.tables:
        return
    count = 0
    for row in m.turn_payload.rows(source, table,
                           key_prefix=(conversation_id, owner), batch_rows=64):
        count += 1
        if count > MAX_MIGRATION_TRASH_TURNS:
            raise m.MigrationImportError('trash source exceeds the Turn budget')
        yield _turn(row)


def _snapshot(source, row):
    header_row = {**row, 'id': row['conversation_id']}
    document = {'header': m._conversation_document(header_row),
                'deleted_at_ms': m._nonneg_int(row['deleted_at_ms']),
                'msg_count': m._nonneg_int(row['msg_count']),
                'turn_count': 0, 'frozen': frozen_snapshot(source, header_row, trashed=True)}
    if not 0 < document['deleted_at_ms'] < 2**64 or document['msg_count'] >= 2**64:
        raise m.MigrationImportError('trash source timestamp or count is invalid')
    chain = hashlib.sha256(b'tofu-db.trash-turns.v1').hexdigest()
    for turn in _turns(source, row['conversation_id'], row['user_id']):
        chain = m._record_chain(chain, m._turn_restore_payload(turn))
        document['turn_count'] += 1
    return document, m._record_chain(chain, document)


def _verify(source, session, document):
    identity = document['header']['id']
    observed = m._query(session, 'migration.trash.get', {'conversation_id': identity})
    if (not isinstance(observed, dict) or observed.get('complete') is not True
            or observed.get('deleted_at_ms') != document['deleted_at_ms']
            or observed.get('turn_count') != document['turn_count']
            or observed.get('frozen') != document['frozen']
            or not isinstance(observed.get('header'), dict)
            or observed['header'].get('msg_count') != document['msg_count']
            or m._conversation_from_target({'id': identity, 'metadata': observed['header']}) != document['header']):
        raise m.MigrationImportError('trash source and pinned target headers differ')
    for turn in _turns(source, identity, session.owner_id):
        lookup = {'conversation_id': identity, 'id': turn['turn_id']}
        raw = m.turn_payload.encoded_snapshot(m._turn_restore_payload(turn))
        large = m.turn_payload.is_large(raw)
        del raw
        actual = (m.turn_payload.read(session, {**lookup, 'trash': True}) if large else
                  m._query(session, 'migration.trash.turn.get', lookup))
        if actual != m._turn_restore_payload(turn):
            raise m.MigrationImportError('trash source and pinned target Turns differ')
    table = _turn_table()
    cursor = None
    seen = set()
    while True:
        page = m._query(session, 'migration.trash.turn.scan', {'conversation_id': identity, 'cursor': cursor})
        if not isinstance(page, dict) or not isinstance(page.get('rows'), list) or len(page['rows']) > 128:
            raise m.MigrationImportError('invalid trash Turn catalog')
        for item in page['rows']:
            if not isinstance(item, dict) or set(item) != {'id'} or not isinstance(item['id'], str) or item['id'] in seen:
                raise m.MigrationImportError('invalid trash Turn catalog identity')
            seen.add(item['id'])
            if len(seen) > document['turn_count']:
                raise m.MigrationImportError('extra pinned trash Turn')
            row = m.turn_payload.point(source, table, (identity, session.owner_id, item['id']))
            if row is None:
                raise m.MigrationImportError('extra pinned trash Turn')
        following = page.get('cursor')
        if following is None:
            break
        if not isinstance(following, str) or following == cursor or not page['rows']:
            raise m.MigrationImportError('trash Turn catalog failed to advance')
        cursor = following
    if len(seen) != document['turn_count']:
        raise m.MigrationImportError('trash Turn catalog is incomplete')


def _catalog(source, session, expected_count=None):
    table = _header_table()
    cursor = None
    count = 0
    previous = b''
    while True:
        page = m._query(session, 'migration.trash.scan', {'cursor': cursor})
        if not isinstance(page, dict) or not isinstance(page.get('rows'), list) or len(page['rows']) > 128:
            raise m.MigrationImportError('invalid trash catalog')
        if 'pending_conversation_id' not in page:
            raise m.MigrationImportError('trash catalog omits pending state')
        pending = page['pending_conversation_id']
        if pending is not None:
            if (not isinstance(pending, str) or expected_count is not None
                    or table['name'] not in source.tables
                    or source.point(table['name'], table['keys'], table['keys'], (pending, session.owner_id)) is None):
                raise m.MigrationImportError('pending trash has no source or remains at completion')
        for item in page['rows']:
            if not isinstance(item, dict) or set(item) != {'conversation_id'}:
                raise m.MigrationImportError('invalid trash catalog identity')
            identity = item['conversation_id']
            if not isinstance(identity, str) or identity.encode('utf-8') <= previous:
                raise m.MigrationImportError('invalid trash catalog identity')
            previous = identity.encode('utf-8')
            count += 1
            row = source.point(table['name'], table['columns'], table['keys'], (identity, session.owner_id)) if table['name'] in source.tables else None
            if row is None:
                raise m.MigrationImportError('target contains extra trash')
            document, _ = _snapshot(source, row)
            _verify(source, session, document)
        following = page.get('cursor')
        if following is None:
            break
        if not isinstance(following, str) or following == cursor or not page['rows']:
            raise m.MigrationImportError('trash catalog failed to advance')
        cursor = following
    if expected_count is not None and count != expected_count:
        raise m.MigrationImportError('trash source and target catalogs differ')


def _verify_prefix(source, session, until=None, complete=False):
    count = 0
    digest = hashlib.sha256(b'tofu-db.trash-domain.v1').hexdigest()
    for row in m._scan(source, _header_table(), until=until, complete=complete):
        document, source_digest = _snapshot(source, row)
        _verify(source, session, document)
        digest = m._record_chain(digest, {'source': source_digest})
        count += 1
    if complete:
        _catalog(source, session, count)
    return count, digest


def import_trash(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    table = _header_table()
    # Reject owner/identity conflicts before this domain writes its first stage.
    for row in m._scan(source, table):
        if row['user_id'] != session.owner_id:
            raise m.MigrationImportError('trash source owner is outside the operator scope')
        if 'storage_conversations' in source.tables and source.point('storage_conversations', ['id'], ['id', 'user_id'], (row['conversation_id'], row['user_id'])) is not None:
            raise m.MigrationImportError('trash source identity is also active')
    turns = _turn_table()
    if turns['name'] in source.tables:
        for row in source.rows(turns['name'], turns['keys'], turns['keys'], batch_rows=64):
            if (row['user_id'] != session.owner_id or table['name'] not in source.tables
                    or source.point(table['name'], table['keys'], table['keys'], (row['conversation_id'], row['user_id'])) is None):
                raise m.MigrationImportError('trash Turn source owner or parent is missing')
            if 'storage_conversation_turns' in source.tables and source.point('storage_conversation_turns', ['turn_id'], ['turn_id'], (row['turn_id'],)) is not None:
                raise m.MigrationImportError('trash Turn source identity is also active')
    progress = m._resume_or_empty(checkpoint, 'trash')
    after = None
    if progress is not None:
        count, digest = _verify_prefix(source, session, None if progress['complete'] else progress['cursor'], progress['complete'])
        if count != progress['rows'] or digest != progress['sha256']:
            raise m.MigrationImportError('resumed trash target differs from progress')
        if progress['complete']:
            return
        after = progress['cursor']
    _catalog(source, session)
    last = after or []
    pending = 0
    rows = source.rows(table['name'], table['columns'], table['keys'], after=after, batch_rows=batch_rows) if table['name'] in source.tables else ()
    for row in rows:
        document, _ = _snapshot(source, row)
        state = m._command(session, 'migration.trash.begin', document)
        if state.get('complete') is not True:
            for turn in _turns(source, row['conversation_id'], row['user_id']):
                m._replay_turn(session, turn)
            m._replay_frozen_transcript(source, session, document['frozen'], trashed=True)
            m._command(session, 'migration.trash.finish', {'conversation_id': row['conversation_id']})
        _verify(source, session, document)
        last = list(m._pk(table, row))
        pending += 1
        if pending >= batch_rows:
            count, digest = _verify_prefix(source, session, last)
            m._publish(checkpoint, 'trash', cursor=last, rows=count, digest=digest, complete=False, after_verified_prefix=after_verified_prefix)
            pending = 0
    count, digest = _verify_prefix(source, session, complete=True)
    m._publish(checkpoint, 'trash', cursor=last, rows=count, digest=digest, complete=True, after_verified_prefix=after_verified_prefix)
