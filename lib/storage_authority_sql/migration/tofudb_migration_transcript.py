"""Bounded frozen-transcript source decoding for offline native migration.

The source owns SQLite handles and exact owner/primary-key lookup. This module
streams the outer JSON array, retaining only one encoded message plus one input
chunk, then uses the application's authoritative archive/projection decoder.
It neither creates Turns nor publishes a migration completion receipt.
"""

from collections.abc import Iterable, Iterator
import json
import math
import re
from typing import Any

from lib.storage_authority_sql.archived_message_codec import (
    decode_archived_message_sequence_from_storage,
)
from lib.storage_authority_sql.migration.generated_tofudb_migration import MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES
from lib.storage_authority_sql.projection_codec import ProjectionCodecError
from lib.storage_authority_sql.migration.tofudb_migration_source import MigrationSourceError, SQLiteMigrationSource


_JSON_STRUCTURE = re.compile(rb'["\\\\{}\[\]]')


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def _source_integer(value: str) -> int | float:
    # Match the application's orjson boundary: signed/unsigned 64-bit
    # integers remain exact; larger finite numbers use its float projection.
    result = int(value)
    return result if -(2**63) <= result < 2**64 else _finite_float(value)


def _invalid_constant(value: str):
    raise ValueError('non-JSON numeric constant')


def _array_members(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """Scan JSON structure in bytes; decode only a complete message object.

    Structural ASCII cannot occur inside a UTF-8 continuation byte. Tracking
    string escapes across chunk edges finds object boundaries without making a
    whole-transcript Python Unicode copy or reparsing every incomplete prefix.
    The authoritative JSON decoder still validates each complete object.
    """
    state = 'start'
    total = 0
    pending = bytearray()
    depth = 0
    in_string = False
    escaped_offset = -1
    iterator = iter(chunks)
    try:
        for chunk in iterator:
            base = total
            total += len(chunk)
            if total > MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES:
                raise MigrationSourceError('frozen transcript exceeds source byte budget')
            position = 0
            while position < len(chunk):
                if state == 'object':
                    end = None
                    for token in _JSON_STRUCTURE.finditer(chunk, position):
                        byte = chunk[token.start()]
                        offset = base + token.start()
                        if in_string:
                            if offset == escaped_offset:
                                continue
                            if byte == 92:
                                escaped_offset = offset + 1
                            elif byte == 34:
                                in_string = False
                        elif byte == 34:
                            in_string = True
                        elif byte in (123, 91):
                            depth += 1
                        elif byte in (125, 93):
                            depth -= 1
                            if depth == 0:
                                end = token.end()
                                break
                    pending.extend(chunk[position:end])
                    if end is None:
                        break
                    member = json.loads(pending, parse_float=_finite_float,
                                        parse_constant=_invalid_constant, parse_int=_source_integer)
                    if not isinstance(member, dict):
                        raise MigrationSourceError('frozen transcript member is not an object')
                    pending = bytearray()
                    position, state = end, 'separator'
                    yield member
                    del member
                    continue
                byte = chunk[position]
                if byte in (32, 9, 13, 10):
                    position += 1
                    continue
                if state == 'start' and byte == 91:
                    state = 'first'
                elif state == 'first' and byte == 93:
                    state = 'done'
                elif state in ('first', 'member') and byte == 123:
                    # Include the opening brace in the structural scan.
                    state = 'object'
                    continue
                elif state == 'separator' and byte == 44:
                    state = 'member'
                elif state == 'separator' and byte == 93:
                    state = 'done'
                else:
                    raise MigrationSourceError('frozen transcript array structure is invalid')
                position += 1
        if state != 'done':
            raise MigrationSourceError('frozen transcript JSON is incomplete')
    except (UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, MigrationSourceError):
            raise
        raise MigrationSourceError('frozen transcript encoding is invalid') from None
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            close()


def frozen_messages(source: SQLiteMigrationSource, conversation_id: str, owner_id: int,
                    *, trashed: bool = False) -> Iterator[dict[str, Any]]:
    """Yield public message documents without materializing the outer archive.

    Consumers must exhaust this iterator before certifying a transcript: a
    valid prefix does not prove the trailing JSON or compression envelopes.
    Close an abandoned iterator to release its source handle immediately.
    """
    table = 'storage_conversation_trash' if trashed else 'storage_conversations'
    identity = 'conversation_id' if trashed else 'id'
    chunks = source.blob_chunks(table, 'messages_json', (identity, 'user_id'),
                                (conversation_id, owner_id),
                                maximum_bytes=MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES)
    members = _array_members(chunks)
    try:
        for member in members:
            try:
                yield decode_archived_message_sequence_from_storage([member])[0]
            except ProjectionCodecError:
                raise MigrationSourceError('frozen transcript message codec is invalid') from None
    finally:
        members.close()


def frozen_source_metadata(source, conversation_id, owner_id, created_at_ms, *, trashed=False):
    """Hash every stored source byte, including the original codec envelopes."""
    import hashlib
    digest = hashlib.sha256()
    size = 0
    table = 'storage_conversation_trash' if trashed else 'storage_conversations'
    identity = 'conversation_id' if trashed else 'id'
    for chunk in source.blob_chunks(table, 'messages_json', (identity, 'user_id'),
                                    (conversation_id, owner_id), maximum_bytes=MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES):
        size += len(chunk)
        digest.update(chunk)
    return {'conversation_id': conversation_id, 'source_sha256': digest.hexdigest(),
            'source_bytes': size, 'created_at_ms': created_at_ms}


def frozen_pages(source, conversation_id, owner_id, *, trashed=False):
    """Pack whole public messages into bounded pages, never splitting a message."""
    import hashlib
    import orjson
    from lib.storage_authority_sql.migration.generated_tofudb_migration import FROZEN_TRANSCRIPTS as limits
    pending = bytearray(b'[')
    page_count = message_count = start = decoded_bytes = 0
    members = frozen_messages(source, conversation_id, owner_id, trashed=trashed)

    def page(raw, count, start):
        return raw, {'start': start, 'count': count, 'bytes': len(raw),
                     'sha256': hashlib.sha256(raw).hexdigest()}

    try:
        for member in members:
            encoded = orjson.dumps(member, option=orjson.OPT_SORT_KEYS)
            if len(encoded) + 2 > limits['maximumPageBytes']:
                raise MigrationSourceError('frozen message exceeds native page byte budget')
            if message_count and len(pending) + len(encoded) + 2 > limits['targetPageBytes']:
                pending.append(93)
                raw = bytes(pending)
                decoded_bytes += len(raw)
                page_count += 1
                if page_count > limits['maximumPages'] or decoded_bytes > limits['maximumDecodedBytes']:
                    raise MigrationSourceError('frozen transcript exceeds native directory or byte budget')
                yield page(raw, message_count, start)
                start += message_count
                pending, message_count = bytearray(b'['), 0
            if start + message_count >= limits['maximumMessages']:
                raise MigrationSourceError('frozen transcript exceeds native message budget')
            if message_count:
                pending.append(44)
            pending.extend(encoded)
            message_count += 1
        if message_count:
            pending.append(93)
            raw = bytes(pending)
            decoded_bytes += len(raw)
            page_count += 1
            if page_count > limits['maximumPages'] or decoded_bytes > limits['maximumDecodedBytes']:
                raise MigrationSourceError('frozen transcript exceeds native directory or byte budget')
            yield page(raw, message_count, start)
    finally:
        members.close()


def frozen_snapshot(source, row, *, trashed=False):
    metadata = frozen_source_metadata(source, row['id'], row['user_id'], row['created_at_ms'], trashed=trashed)
    pages = [descriptor for _, descriptor in frozen_pages(source, row['id'], row['user_id'], trashed=trashed)]
    return {'source': metadata, 'complete': True, 'messages': sum(page['count'] for page in pages),
            'decoded_bytes': sum(page['bytes'] for page in pages), 'pages': pages}
