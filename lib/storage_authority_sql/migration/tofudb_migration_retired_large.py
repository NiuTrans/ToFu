"""Bounded, lossless chunk archives of explicitly retired conversation/task rows.

Source types and byte strings stay intact. Parts publish before a row manifest;
verified prefixes never repair missing target state. These archives do not
reactivate old conversations, executors or queued requests. SQL belongs to the
pinned source reader, and generic native records own atomic part publication.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing

from lib.storage_authority_sql.migration.generated_tofudb_migration import RETIRED_LARGE_ARCHIVE as LIMITS
from lib.storage_authority_sql.migration.generated_tofudb_migration import MAX_SOURCE_CHUNK_BYTES
from lib.storage_authority_sql.migration.tofudb_migration_retired import _cell, _value

NAMESPACE = 'migration.retired_sqlite_parts.v1'
TABLES = [{'keys': ['id', 'user_id'],
  'columns': ['id',
              'user_id',
              'title',
              'messages',
              'created_at',
              'updated_at',
              'settings',
              'msg_count',
              'search_text',
              'rev',
              'messages_rows_rev'],
  'name': 'conversations',
  'retiredLarge': True},
 {'keys': ['conv_id', 'seq'],
  'columns': ['conv_id',
              'seq',
              'msg_id',
              'role',
              'content',
              'content_json',
              'thinking',
              'translated_content',
              'meta',
              'meta_light',
              'message_ts',
              'billing_meta',
              'created_at',
              'updated_at',
              'translation_state'],
  'name': 'conversation_messages',
  'retiredLarge': True},
 {'keys': ['turn_id'],
  'columns': ['turn_id',
              'conversation_id',
              'user_id',
              'lane_id',
              'parent_turn_id',
              'ordinal',
              'actor',
              'kind',
              'run_id',
              'status',
              'current_attempt_id',
              'projection',
              'projection_revision',
              'settlement',
              'created_at',
              'updated_at'],
  'name': 'conversation_turns',
  'retiredLarge': True},
 {'keys': ['attempt_id'],
  'columns': ['attempt_id',
              'conversation_id',
              'turn_id',
              'command_id',
              'task_id',
              'operation',
              'status',
              'base_projection_revision',
              'resume_anchor',
              'config',
              'error',
              'created_at',
              'started_at',
              'settled_at',
              'superseded_at'],
  'name': 'generation_attempts',
  'retiredLarge': True},
 {'keys': ['task_id'],
  'columns': ['task_id',
              'conv_id',
              'content',
              'thinking',
              'error',
              'status',
              'tool_rounds',
              'search_results',
              'metadata',
              'segments',
              'created_at',
              'completed_at',
              'abort_requested_at',
              'abort_source'],
  'name': 'task_results',
  'retiredLarge': True},
 {'keys': ['id'],
  'columns': ['id',
              'conv_id',
              'messages_json',
              'summary',
              'created_at',
              'trigger',
              'task_id',
              'round_num',
              'model',
              'tokens_before',
              'tokens_after',
              'msgs_before',
              'msgs_after',
              'reason'],
  'name': 'transcript_archive',
  'retiredLarge': True},
 {'keys': ['id'],
  'columns': ['id',
              'conv_id',
              'payload',
              'config',
              'position',
              'kind',
              'priority',
              'created_at',
              'leased_until',
              'lease_task_id'],
  'name': 'message_queue',
  'retiredLarge': True},
 {'name': 'conversations_fts_content', 'keys': ['id'], 'columns': ['id', 'c0'],
  'retiredLarge': True}]


def _m():
    from lib.storage_authority_sql.migration import tofudb_migration_import
    return tofudb_migration_import


def _key(table, key):
    return table['name'] + ':' + hashlib.sha256(_m()._canonical([_cell(v) for v in key])).hexdigest()


def _snapshot(key, payload):
    return {'namespace': NAMESPACE, 'key': key, 'version': 1, 'updated_at_ms': 1,
            'value_json': _m()._canonical(payload).decode('ascii')}


def _root(table, key, cells):
    if len(_m()._canonical(list(key))) > 8192:
        raise _m().MigrationImportError('retired large archive key budget exceeded')
    result = _snapshot(_key(table, key), {
        'format': 1, 'kind': 'row', 'table': table['name'],
        'columns': table['columns'], 'primaryKeyColumns': table['keys'],
        'primaryKeyValues': [_cell(v) for v in key], 'cells': cells,
    })
    if len(_m()._canonical(result)) > LIMITS['maximumRootBytes']:
        raise _m().MigrationImportError('retired large archive manifest budget exceeded')
    return result


def _describe(source, table, key, *, hashes=True):
    cells = []
    raw_bytes = parts = 0
    for column, meta in zip(table['columns'], source.cell_metadata(
            table['name'], table['columns'], table['keys'], key)):
        kind = meta['type']
        if kind not in ('text', 'blob'):
            cell = _cell(meta['value'])
            raw_bytes += len(_m()._canonical(cell))
            cells.append(cell)
            continue
        size = meta['bytes']
        raw_bytes += size
        if raw_bytes > LIMITS['maximumRowBytes']:
            raise _m().MigrationImportError('retired large archive row budget exceeded')
        if size <= LIMITS['inlineCellBytes']:
            value = source.blob_slice(table['name'], column, table['keys'], key,
                                      offset=0, size=size, maximum_bytes=LIMITS['maximumRowBytes'])
            cells.append([kind + '-hex', value.hex()])
        else:
            count = (size + MAX_SOURCE_CHUNK_BYTES - 1) // MAX_SOURCE_CHUNK_BYTES
            parts += count
            digest = hashlib.sha256()
            if hashes:
                with closing(source.blob_chunks(table['name'], column, table['keys'], key,
                                                 maximum_bytes=LIMITS['maximumRowBytes'])) as chunks:
                    for chunk in chunks:
                        digest.update(chunk)
            cells.append([kind + '-parts', size, digest.hexdigest(), count])
    if raw_bytes > LIMITS['maximumRowBytes']:
        raise _m().MigrationImportError('retired large archive row budget exceeded')
    return cells, raw_bytes, parts


def source_rows(source, table, *, batch_rows=128):
    if table['name'] not in source.tables:
        return
    with closing(source.rows(table['name'], table['keys'], table['keys'], batch_rows=batch_rows)) as rows:
        for row in rows:
            key = tuple(row[field] for field in table['keys'])
            cells, _, _ = _describe(source, table, key)
            yield {**row, '_archive_snapshot': _root(table, key, cells)}


def preflight(source):
    count = estimated_bytes = records = 0
    for table in TABLES:
        if table['name'] not in source.tables:
            continue
        if set(source.table_columns(table['name'])) != set(table['columns']):
            raise _m().MigrationImportError('retired large source has unarchived columns')
        with closing(source.rows(table['name'], table['keys'], table['keys'])) as rows:
            for row in rows:
                key = tuple(row[field] for field in table['keys'])
                cells, raw, parts = _describe(source, table, key, hashes=False)
                _root(table, key, cells)
                count += 1
                records += parts + 1
                # Hex payload plus a conservative allowance for metadata per row/part.
                estimated_bytes += 2 * raw + 65536 + parts * 32768
                if (count > LIMITS['maximumSourceRows'] or records > LIMITS['maximumTargetRecords']
                        or estimated_bytes > LIMITS['maximumEncodedBytes']):
                    raise _m().MigrationImportError('retired large archive aggregate budget exceeded')


def _part(table, key, column, index, data):
    return _snapshot(f'{_key(table, key)}:part:{column}:{index}', {
        'format': 1, 'kind': 'part', 'table': table['name'],
        'primaryKeyValues': [_cell(v) for v in key],
        'column': column, 'part': index, 'data_hex': data.hex(),
    })


def _parts(source, table, key, root):
    cells = json.loads(root['value_json'])['cells']
    for column, cell in enumerate(cells):
        if cell[0] not in ('text-parts', 'blob-parts'):
            continue
        with closing(source.blob_chunks(table['name'], table['columns'][column], table['keys'], key,
                                         maximum_bytes=LIMITS['maximumRowBytes'])) as chunks:
            for index, chunk in enumerate(chunks):
                yield _part(table, key, column, index, chunk)


def _verify_root(source, session, table, key, root):
    _m()._verify_record(root, session)
    for part in _parts(source, table, key, root):
        _m()._verify_record(part, session)


def _catalog(source, session, selected):
    by_name = {table['name']: table for table in TABLES}
    cursor = None
    previous = b''
    rows_seen = archive_records = 0
    while True:
        page = _m()._query(session, 'migration.record.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('records'), list)
                or len(page['records']) > _m().OPERATOR_SESSION['maximumCatalogPageRows']):
            raise _m().MigrationImportError('invalid retired large archive catalog')
        for identity in page['records']:
            if (not isinstance(identity, dict) or set(identity) != {'namespace', 'key'}
                    or any(not isinstance(v, str) for v in identity.values())):
                raise _m().MigrationImportError('invalid retired large archive identity')
            namespace = identity['namespace'].encode()
            if len(namespace) > 65535:
                raise _m().MigrationImportError('invalid retired large archive namespace')
            encoded = len(namespace).to_bytes(2, 'big') + namespace + identity['key'].encode()
            if encoded <= previous:
                raise _m().MigrationImportError('retired large archive catalog did not advance')
            previous = encoded
            if identity['namespace'] != NAMESPACE:
                continue
            archive_records += 1
            if archive_records > LIMITS['maximumTargetRecords']:
                raise _m().MigrationImportError('retired large target record budget exceeded')
            name = identity['key'].split(':', 1)[0]
            if name not in by_name:
                raise _m().MigrationImportError('retired large target has unknown source table')
            if name != selected:
                continue
            table = by_name[name]
            observed = _m()._query(session, 'migration.record.get', identity)
            try:
                payload = json.loads(observed['value_json'])
                key = tuple(_value(v) for v in payload['primaryKeyValues'])
            except (ValueError, TypeError, KeyError):
                raise _m().MigrationImportError('retired large target metadata is malformed') from None
            if (len(key) != len(table['keys'])
                    or any(not isinstance(v, str) and type(v) is not int for v in key)
                    or name not in source.tables
                    or source.point(name, table['keys'], table['keys'], key) is None):
                raise _m().MigrationImportError('retired large target has no source row')
            if payload.get('kind') == 'row':
                cells, _, _ = _describe(source, table, key)
                expected = _root(table, key, cells)
                if observed != expected:
                    raise _m().MigrationImportError('retired large manifest differs from source')
                _verify_root(source, session, table, key, expected)
                rows_seen += 1
            elif payload.get('kind') == 'part':
                column, index = payload.get('column'), payload.get('part')
                if (type(column) is not int or not 0 <= column < len(table['columns'])
                        or type(index) is not int or index < 0):
                    raise _m().MigrationImportError('retired large part identity is malformed')
                field = table['columns'][column]
                meta = source.cell_metadata(name, [field], table['keys'], key)[0]
                size = meta.get('bytes', 0)
                offset = index * MAX_SOURCE_CHUNK_BYTES
                if (meta['type'] not in ('text', 'blob') or size <= LIMITS['inlineCellBytes']
                        or offset >= size):
                    raise _m().MigrationImportError('retired large part has no source bytes')
                data = source.blob_slice(name, field, table['keys'], key, offset=offset,
                                         size=min(MAX_SOURCE_CHUNK_BYTES, size-offset),
                                         maximum_bytes=LIMITS['maximumRowBytes'])
                if observed != _part(table, key, column, index, data):
                    raise _m().MigrationImportError('retired large part differs from source')
            else:
                raise _m().MigrationImportError('retired large target node kind is invalid')
        following = page.get('cursor')
        if following is None:
            return rows_seen
        if (not page['records'] or not isinstance(following, str)
                or following != previous.hex() or following == cursor):
            raise _m().MigrationImportError('retired large archive cursor did not advance')
        cursor = following


def import_archives(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    m = _m()
    for table in TABLES:
        domain = 'retired_large.' + table['name']
        progress = m._resume_or_empty(checkpoint, domain)
        _catalog(source, session, table['name'])
        digest = hashlib.sha256(domain.encode()).hexdigest()
        if progress and progress['rows'] == 0 and progress['sha256'] != digest:
            raise m.MigrationImportError('retired large checkpoint differs')
        count = 0
        last = []
        iterator = source_rows(source, table, batch_rows=batch_rows)
        with closing(iterator):
            for row in iterator:
                root = row['_archive_snapshot']
                key = tuple(row[field] for field in table['keys'])
                if progress and count < progress['rows']:
                    _verify_root(source, session, table, key, root)
                else:
                    for part in _parts(source, table, key, root):
                        m._command(session, 'migration.record.restore', part)
                        m._verify_record(part, session)
                    m._command(session, 'migration.record.restore', root)
                    m._verify_record(root, session)
                count += 1
                digest = m._record_chain(digest, root)
                last = list(key)
                if progress and count == progress['rows'] and (
                        digest != progress['sha256'] or not progress['complete'] and last != progress['cursor']):
                    raise m.MigrationImportError('retired large checkpoint differs')
                if count % batch_rows == 0 and (not progress or count > progress['rows']):
                    m._publish(checkpoint, domain, cursor=last, rows=count, digest=digest,
                               complete=False, after_verified_prefix=after_verified_prefix)
        if progress and (count < progress['rows'] or progress['complete'] and (
                count != progress['rows'] or digest != progress['sha256'])):
            raise m.MigrationImportError('retired large checkpoint differs')
        if _catalog(source, session, table['name']) != count:
            raise m.MigrationImportError('retired large archive catalog count differs')
        m._publish(checkpoint, domain, cursor=last, rows=count, digest=digest,
                   complete=True, after_verified_prefix=after_verified_prefix)
