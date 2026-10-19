"""Bounded global receipt-source audit before any native ownership assignment.

Both source formats share command identity. Duplicate identities are corruption
even when payloads match, as in the live dual-format resolver. Keep raw response
bytes and request digests; never infer an owner, rewrite a digest, or coalesce
formats. The pinned reader owns SQL, raw BLOB keys and cursor cleanup.
"""
from contextlib import closing
import hashlib
import time

from lib.storage_authority_sql.receipt_codec import command_receipt_key_v2, decode_receipt_response
from lib.storage_authority_sql.migration.tofudb_migration_source import MigrationSourceError
from lib.storage_authority_sql.migration.generated_tofudb_migration import LEGACY_RECEIPT_IMPORT

TABLE_KEYS = {'storage_command_receipts': 'command_id', 'storage_command_receipts_v2': 'command_key'}
MAX_ROWS = LEGACY_RECEIPT_IMPORT['maximumRows']


def audit_receipt_source(source, *, maximum_rows=MAX_ROWS, timeout_seconds=60.0):
    """Return complete fingerprints or fail; no partial result implies admission."""
    if (type(maximum_rows) is not int or not 1 <= maximum_rows <= MAX_ROWS
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300):
        raise MigrationSourceError('invalid receipt source audit budget')
    deadline = time.monotonic() + timeout_seconds
    total = 0
    result = {'scope': 'global-source-codec-and-identity-only;not-native-owner-or-cutover-proof',
              'tables': {}, 'duplicateIdentities': 0}
    for table, key in TABLE_KEYS.items():
        if table not in source.tables:
            continue
        columns = [key, 'operation', 'request_digest', 'response_json', 'committed_at_ms']
        if set(source.table_columns(table)) != set(columns):
            raise MigrationSourceError('receipt source has unaccounted columns')
        rows = (source.rows(table, columns, [key]) if key == 'command_id'
                else source.digest_key_rows(table, columns, key))
        count = stored = 0
        chain = hashlib.sha256()
        with closing(rows):
            for row in rows:
                total += 1
                if total > maximum_rows or time.monotonic() > deadline:
                    raise MigrationSourceError('receipt source audit budget exhausted')
                if key == 'command_id':
                    command_key = command_receipt_key_v2(row[key])
                    if ('storage_command_receipts_v2' in source.tables
                            and source.digest_key_point('storage_command_receipts_v2',
                                ['command_key'], 'command_key', command_key.hex()) is not None):
                        raise MigrationSourceError('command receipt exists in multiple storage formats')
                    value = row['request_digest']
                    if not isinstance(value, str) or len(value) != 64:
                        raise MigrationSourceError('legacy receipt request digest is invalid')
                    try:
                        request_digest = bytes.fromhex(value)
                    except ValueError:
                        raise MigrationSourceError('legacy receipt request digest is invalid') from None
                else:
                    command_key = bytes.fromhex(row[key])
                    request_digest = row['request_digest']
                if not isinstance(request_digest, bytes) or len(request_digest) != 32:
                    raise MigrationSourceError('receipt request digest is invalid')
                operation = row['operation']
                clock = row['committed_at_ms']
                if not isinstance(operation, str) or not operation or type(clock) is not int or not 0 < clock < 2**64:
                    raise MigrationSourceError('receipt operation or commit clock is invalid')
                response = row['response_json']
                if isinstance(response, str):
                    response = response.encode('utf-8')
                decode_receipt_response(response)
                if time.monotonic() > deadline:
                    raise MigrationSourceError('receipt source audit budget exhausted')
                for part in (command_key, operation.encode(), request_digest, response, str(clock).encode()):
                    chain.update(len(part).to_bytes(8, 'big'))
                    chain.update(part)
                count += 1
                stored += len(response)
        result['tables'][table] = {'rows': count, 'storedBytes': stored, 'sourceChainSha256': chain.hexdigest()}
    return result
