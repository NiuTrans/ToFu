"""Lossless, inactive archives of explicitly retired SQLite domain rows.

No archived row is replayed as a live task or current project setting. The
operator owner is the archive custodian; rows retain their original fields,
including any historical identity. Typed cells preserve NULL, TEXT, BLOB,
integer and binary floating-point values. SQL remains in migration_source.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from lib.storage_authority_sql.migration.tofudb_migration_metadata import TABLE as METADATA_TABLE, validate_metadata

NAMESPACE = 'migration.retired_sqlite.v1'
MAX_ARCHIVE_ROWS = 50_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
TABLES = [{'name': 'error_resolutions',
  'keys': ['fingerprint'],
  'columns': ['fingerprint',
              'logger_name',
              'sample_message',
              'resolved_by',
              'ticket',
              'notes',
              'resolved_at',
              'updated_at']},
 {'name': 'project_charter',
  'keys': ['project_path'],
  'columns': ['project_path', 'content', 'decisions', 'updated_by_conv', 'updated_at', 'version']},
 {'name': 'project_status_snapshots',
  'keys': ['project_path', 'seq'],
  'columns': ['project_path', 'seq', 'snapshot_id', 'narrative', 'pillar_state', 'trigger', 'ts']},
 {'name': 'project_watch_items',
  'keys': ['item_id'],
  'columns': ['item_id',
              'project_path',
              'kind',
              'text',
              'status',
              'promoted',
              'response_fingerprint',
              'created_by_conv',
              'created_at',
              'updated_at',
              'promoted_text',
              'promoted_at']},
 {'name': 'project_watch_responses',
  'keys': ['item_id', 'seq'],
  'columns': ['item_id', 'seq', 'project_path', 'response', 'pillar_state', 'trigger', 'ts']},
 {'name': 'recent_projects', 'keys': ['path'], 'columns': ['path', 'count', 'last_used']},
 {'name': 'scheduled_tasks',
  'keys': ['id'],
  'columns': ['id',
              'name',
              'schedule',
              'task_type',
              'command',
              'description',
              'enabled',
              'notify_on_failure',
              'notify_on_success',
              'max_runtime',
              'last_run',
              'last_result',
              'last_status',
              'run_count',
              'fail_count',
              'created_at',
              'updated_at',
              'target_conv_id',
              'source_conv_id',
              'tools_config',
              'poll_count',
              'last_poll_at',
              'last_poll_decision',
              'last_poll_reason',
              'last_execution_at',
              'last_execution_task_id',
              'last_execution_status',
              'execution_count',
              'max_executions',
              'expires_at',
              'condition_kind',
              'condition_command',
              'condition_regex',
              'promotion_streak',
              'fallback_streak',
              'promoted_at']},
 {'name': 'timer_watchers',
  'keys': ['id'],
  'columns': ['id',
              'conv_id',
              'source_task_id',
              'check_instruction',
              'check_command',
              'continuation_message',
              'poll_interval',
              'max_polls',
              'poll_count',
              'status',
              'tools_config',
              'created_at',
              'updated_at',
              'triggered_at',
              'cancelled_at',
              'execution_task_id',
              'last_poll_at',
              'last_poll_decision',
              'last_poll_reason',
              'condition_kind',
              'condition_command',
              'condition_regex',
              'promotion_streak',
              'fallback_streak',
              'promoted_at',
              'origin']},
 {'name': 'timer_poll_log',
  'keys': ['id'],
  'columns': ['id',
              'timer_id',
              'poll_time',
              'decision',
              'reason',
              'check_output',
              'tokens_used',
              'model',
              'poll_id',
              'raw_output',
              'tier',
              'predicate_matched',
              'llm_agreed']},
 {'name': 'proactive_poll_log',
  'keys': ['id'],
  'columns': ['id',
              'task_id',
              'poll_time',
              'decision',
              'reason',
              'status_snapshot',
              'model',
              'tokens_used',
              'execution_task_id',
              'tier',
              'predicate_matched',
              'llm_agreed']},
 {'name': 'users', 'keys': ['id'],
  'columns': ['id', 'username', 'display_name', 'password_hash', 'created_at']},
 {'name': 'schema_meta', 'keys': ['key'], 'columns': ['key', 'value']},
 {'name': 'scoped_sequences', 'keys': ['namespace', 'scope_key'],
  'columns': ['namespace', 'scope_key', 'value']},
 {'name': 'conversations_fts_config', 'keys': ['k'], 'columns': ['k', 'v']},
 {'name': 'pricing_cache', 'keys': ['key'], 'columns': ['key', 'value', 'updated_at']},
 {'name': 'rate_limit_events', 'keys': ['id'], 'columns': ['id', 'endpoint', 'ip', 'ts_ms']},
 METADATA_TABLE]


def _cell(value: Any) -> list[Any]:
    if value is None:
        return ['null']
    if type(value) is int:
        return ['integer', str(value)]
    if type(value) is float:
        return ['real', value.hex()]
    if isinstance(value, str):
        return ['text', value]
    if isinstance(value, bytes):
        return ['blob', value.hex()]
    raise ValueError('unsupported retired SQLite cell')


def _value(cell: list[Any]) -> Any:
    if cell == ['null']:
        return None
    if not isinstance(cell, list) or len(cell) != 2 or not isinstance(cell[1], str):
        raise ValueError('invalid retired SQLite cell')
    kind, value = cell
    if kind == 'integer':
        return int(value)
    if kind == 'real':
        return float.fromhex(value)
    if kind == 'text':
        return value
    if kind == 'blob':
        return bytes.fromhex(value)
    raise ValueError('invalid retired SQLite cell type')


def _document(table, row):
    from lib.storage_authority_sql.migration import tofudb_migration_import as m
    if table['name'] == METADATA_TABLE['name']:
        try:
            validate_metadata(row)
        except ValueError as exc:
            raise m.MigrationImportError(str(exc)) from exc
    identity = [_cell(row[key]) for key in table['keys']]
    key = table['name'] + ':' + hashlib.sha256(m._canonical(identity)).hexdigest()
    payload = {'format': 1, 'table': table['name'], 'columns': table['columns'],
               'primaryKeyColumns': table['keys'],
               'cells': [_cell(row[column]) for column in table['columns']]}
    return {'namespace': NAMESPACE, 'key': key, 'version': 1, 'updated_at_ms': 1,
            'value_json': json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False)}


def preflight(source):
    """Admit complete source columns and a bounded total before target writes."""
    from lib.storage_authority_sql.migration import tofudb_migration_import as m
    count = 0
    total_bytes = 0
    for table in TABLES:
        if table['name'] not in source.tables:
            continue
        if set(source.table_columns(table['name'])) != set(table['columns']):
            raise m.MigrationImportError('retired source schema has unarchived columns')
        rows = source.rows(table['name'], table['columns'], table['keys'])
        try:
            for row in rows:
                count += 1
                if count > MAX_ARCHIVE_ROWS:
                    raise m.MigrationImportError('retired source archive row budget exceeded')
                # Typed BLOB/text expansion must also fit the existing record wire budget.
                document_bytes = len(m._canonical(_document(table, row)))
                total_bytes += document_bytes
                if document_bytes > 2 * 1024 * 1024:
                    raise m.MigrationImportError('retired source archive document budget exceeded')
                if total_bytes > MAX_ARCHIVE_BYTES:
                    raise m.MigrationImportError('retired source archive byte budget exceeded')
        finally:
            rows.close()


def _catalog(source, session, selected_table):
    from lib.storage_authority_sql.migration import tofudb_migration_import as m
    table_by_name = {table['name']: table for table in TABLES}
    cursor = None
    previous = b''
    seen = 0
    archived = 0
    while True:
        page = m._query(session, 'migration.record.scan', {'cursor': cursor})
        if (not isinstance(page, dict) or not isinstance(page.get('records'), list)
                or len(page['records']) > m.OPERATOR_SESSION['maximumCatalogPageRows']):
            raise m.MigrationImportError('invalid retired archive catalog page')
        for identity in page['records']:
            if (not isinstance(identity, dict) or set(identity) != {'namespace', 'key'}
                    or any(not isinstance(value, str) for value in identity.values())):
                raise m.MigrationImportError('invalid retired archive catalog identity')
            ns, key = identity['namespace'], identity['key']
            encoded_ns = ns.encode('utf-8')
            encoded = len(encoded_ns).to_bytes(2, 'big') + encoded_ns + key.encode('utf-8')
            if encoded <= previous:
                raise m.MigrationImportError('retired archive catalog did not advance')
            previous = encoded
            if ns != NAMESPACE:
                continue
            archived += 1
            if archived > MAX_ARCHIVE_ROWS:
                raise m.MigrationImportError('retired target archive row budget exceeded')
            name = key.split(':', 1)[0]
            if name not in table_by_name:
                raise m.MigrationImportError('retired target contains an unknown source table')
            if name != selected_table:
                continue
            table = table_by_name[name]
            observed = m._query(session, 'migration.record.get', identity)
            if not isinstance(observed, dict) or not isinstance(observed.get('value_json'), str):
                raise m.MigrationImportError('retired target archive document is missing')
            try:
                payload = json.loads(observed['value_json'])
            except ValueError:
                raise m.MigrationImportError('retired target archive document is malformed') from None
            if not isinstance(payload, dict):
                raise m.MigrationImportError('retired target archive document is malformed')
            if (payload.get('columns') != table['columns']
                    or payload.get('primaryKeyColumns') != table['keys']
                    or len(payload.get('cells', [])) != len(table['columns'])):
                raise m.MigrationImportError('retired target source schema differs')
            try:
                row = dict(zip(table['columns'], map(_value, payload['cells'])))
            except (ValueError, TypeError):
                raise m.MigrationImportError('retired target archive cells are malformed') from None
            source_row = (source.point(name, table['columns'], table['keys'],
                                       [row[key] for key in table['keys']])
                          if name in source.tables else None)
            if source_row is None or _document(table, source_row) != observed:
                raise m.MigrationImportError('retired archive source and target differ')
            seen += 1
        following = page.get('cursor')
        if following is None:
            return seen
        if (not page['records'] or not isinstance(following, str)
                or following != previous.hex() or following == cursor):
            raise m.MigrationImportError('retired archive catalog cursor did not advance')
        cursor = following


def import_retired(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    from lib.storage_authority_sql.migration import tofudb_migration_import as m
    for table in TABLES:
        domain = 'retired.' + table['name']
        progress = m._resume_or_empty(checkpoint, domain)
        _catalog(source, session, table['name'])
        digest = hashlib.sha256(domain.encode()).hexdigest()
        if progress and progress['rows'] == 0 and progress['sha256'] != digest:
            raise m.MigrationImportError('retired archive checkpoint differs')
        count = 0
        last = []
        rows = (source.rows(table['name'], table['columns'], table['keys'],
                            batch_rows=batch_rows) if table['name'] in source.tables else ())
        try:
            for row in rows:
                document = _document(table, row)
                # Insert-or-match remains read-only for a replayed committed prefix.
                if not progress or count >= progress['rows']:
                    m._command(session, 'migration.record.restore', document)
                m._verify_record(document, session)
                count += 1
                digest = m._record_chain(digest, document)
                last = list(m._pk(table, row))
                if progress and count == progress['rows']:
                    if digest != progress['sha256'] or (
                            not progress['complete'] and last != progress['cursor']):
                        raise m.MigrationImportError('retired archive checkpoint differs')
                if count % batch_rows == 0 and (not progress or count > progress['rows']):
                    m._publish(checkpoint, domain, cursor=last, rows=count, digest=digest,
                               complete=False, after_verified_prefix=after_verified_prefix)
        finally:
            if hasattr(rows, 'close'):
                rows.close()
        if progress and (count < progress['rows'] or (progress['complete'] and (
                count != progress['rows'] or digest != progress['sha256']))):
            raise m.MigrationImportError('retired archive checkpoint differs')
        if _catalog(source, session, table['name']) != count:
            raise m.MigrationImportError('retired archive catalog count differs')
        m._publish(checkpoint, domain, cursor=last, rows=count, digest=digest,
                   complete=True, after_verified_prefix=after_verified_prefix)
