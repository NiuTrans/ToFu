"""Bounded source Turn bodies and private native snapshot transfers.

Metadata uses ordinary row admission. Exact-PK BLOB reads stream at 256 KiB;
only one explicitly bounded source/snapshot document is materialized at a time.
"""
import hashlib
import json

from lib.log import get_logger

from lib.storage_sidecar.generated_tofudb_migration import (
    MAX_SOURCE_ROW_BYTES, MAX_SOURCE_CHUNK_BYTES, MAX_TURN_SNAPSHOT_SOURCE_BYTES,
    MAX_TURN_SNAPSHOT_BYTES,
)

logger = get_logger(__name__)

TABLES = frozenset({'storage_conversation_turns', 'storage_conversation_trash_turns'})
BODY_COLUMNS = frozenset({'projection_json', 'settlement_json'})


def metadata_columns(table):
    return [column for column in table['columns'] if column not in BODY_COLUMNS]


def body_fingerprints(source, table, row):
    key = [row[column] for column in table['keys']]
    result = {}
    remaining = MAX_TURN_SNAPSHOT_SOURCE_BYTES
    for column in sorted(BODY_COLUMNS.intersection(table['columns'])):
        digest = hashlib.sha256()
        length = 0
        for chunk in source.blob_chunks(table['name'], column, table['keys'], key,
                                        maximum_bytes=remaining):
            digest.update(chunk)
            length += len(chunk)
        remaining -= length
        result[column] = {'bytes': length, 'sha256': digest.hexdigest()}
    return result


def hydrate_row(source, table, row):
    key = [row[column] for column in table['keys']]
    result = dict(row)
    remaining = MAX_TURN_SNAPSHOT_SOURCE_BYTES
    for column in sorted(BODY_COLUMNS.intersection(table['columns'])):
        raw = b''.join(source.blob_chunks(table['name'], column, table['keys'], key,
                                         maximum_bytes=remaining))
        remaining -= len(raw)
        result[column] = raw
    return result


def rows(source, table, **kwargs):
    for row in source.rows(table['name'], metadata_columns(table), table['keys'], **kwargs):
        yield hydrate_row(source, table, row)


def point(source, table, key):
    row = source.point(table['name'], metadata_columns(table), table['keys'], key)
    return None if row is None else hydrate_row(source, table, row)


def encoded_snapshot(value):
    from lib.storage_sidecar.tofudb_migration_import import MigrationImportError
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    if len(raw) > MAX_TURN_SNAPSHOT_BYTES:
        raise MigrationImportError('Turn snapshot exceeds its domain byte budget')
    return raw


def upload(session, raw):
    from lib.storage_sidecar import tofudb_migration_import as m
    begin = m._command(session, 'migration.turn.transfer.begin', {
        'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    try:
        start = begin.get('offset')
        if type(start) is not int or not 0 <= start <= len(raw):
            raise m.MigrationImportError('invalid Turn upload resume offset')
        for offset in range(start, len(raw), MAX_SOURCE_CHUNK_BYTES):
            chunk = raw[offset:offset+MAX_SOURCE_CHUNK_BYTES]
            result = m._command(session, 'migration.turn.transfer.append', {'offset': offset, 'hex': chunk.hex()})
            if result.get('offset') != offset+len(chunk):
                raise m.MigrationImportError('Turn upload offset differs')
        m._command(session, 'migration.turn.transfer.finish', {})
    except BaseException:
        try:
            m._command(session, 'migration.turn.transfer.abort', {})
        except Exception as exc:
            # Best-effort abort; the original failure re-raises below.
            logger.debug('Turn upload abort failed: %s', exc)
        raise


def read(session, identity):
    from lib.storage_sidecar import tofudb_migration_import as m
    manifest = m._command(session, 'migration.turn.transfer.read.begin', identity)
    raw = bytearray()
    try:
        size = manifest.get('bytes')
        if type(size) is not int or not 0 < size <= MAX_TURN_SNAPSHOT_BYTES:
            raise m.MigrationImportError('invalid Turn read snapshot size')
        while len(raw) < size:
            part = m._command(session, 'migration.turn.transfer.read.chunk', {'offset': len(raw)})
            if part.get('offset') != len(raw) or not isinstance(part.get('hex'), str):
                raise m.MigrationImportError('invalid Turn read chunk')
            if len(part['hex']) > MAX_SOURCE_CHUNK_BYTES * 2:
                raise m.MigrationImportError('Turn read chunk exceeds its bound')
            chunk = bytes.fromhex(part['hex'])
            if not chunk or len(raw)+len(chunk) > size:
                raise m.MigrationImportError('Turn read size differs')
            raw.extend(chunk)
            if part.get('final') is not (len(raw) == size):
                raise m.MigrationImportError('Turn read final marker differs')
        if hashlib.sha256(raw).hexdigest() != manifest.get('sha256'):
            raise m.MigrationImportError('Turn read digest differs')
        return json.loads(raw)
    finally:
        try:
            m._command(session, 'migration.turn.transfer.read.abort', {})
        except Exception as exc:
            # Preserve the transfer error; EOF still releases the native buffer.
            logger.debug('Turn read abort failed: %s', exc)


def is_large(raw):
    # Reserve room for the ordinary operator JSON envelope and escaping.
    return len(raw) > MAX_SOURCE_ROW_BYTES // 4
