"""Bounded private compression for durable task-event JSON payloads.

Responsibility
--------------
Task-event rows preserve Request Inspector and cold-replay evidence, including
occasional MiB-scale message snapshots. Small and incompressible payloads keep
their legacy canonical JSON bytes. Payloads of at least 64 KiB get one zlib
level-1 attempt and use the private envelope only when it is smaller.

Entry points are :func:`encode_task_event_payload`,
:func:`task_event_decoded_size`, and :func:`decode_task_event_payload`. The
decoded form remains bounded by the storage RPC's 64 MiB frame ceiling; corrupt
and unknown envelopes fail closed. The size preflight reads the validated
length header without allocating the decoded body, so maintenance can enforce a
smaller memory budget. The codec has no backend or transaction dependency, so
SQLite BLOB and PostgreSQL BYTEA rows share byte-identical behavior.
"""

from __future__ import annotations

import struct
from typing import Any
import zlib

from lib.storage.errors import StorageError
from lib.storage.protocol import MAX_FRAME_BYTES


TASK_EVENT_COMPRESSION_MIN_BYTES = 64 * 1024
MAX_DECODED_TASK_EVENT_BYTES = MAX_FRAME_BYTES
MAX_STORED_TASK_EVENT_BYTES = MAX_FRAME_BYTES
COMPRESSED_TASK_EVENT_MAGIC = b"tofu.task-event.zlib.v1\x00"
_TASK_EVENT_MAGIC_PREFIX = b"tofu.task-event."
_DECODED_LENGTH = struct.Struct("!I")


def _payload_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise StorageError(
        "database_integrity", "Stored task event has an invalid payload type"
    )


def encode_task_event_payload(raw: bytes) -> bytes:
    """Return legacy JSON bytes or a deterministic, smaller v1 envelope."""
    if not isinstance(raw, bytes):
        raise StorageError(
            "database_protocol_error", "Task event encoding requires JSON bytes"
        )
    if len(raw) > MAX_DECODED_TASK_EVENT_BYTES:
        raise StorageError(
            "database_protocol_error", "Task event exceeds its decoded byte budget"
        )
    if len(raw) < TASK_EVENT_COMPRESSION_MIN_BYTES:
        return raw
    compressed = zlib.compress(raw, level=1)
    encoded = (
        COMPRESSED_TASK_EVENT_MAGIC
        + _DECODED_LENGTH.pack(len(raw))
        + compressed
    )
    return encoded if len(encoded) < len(raw) else raw


def _decoded_size_from_encoded(encoded: bytes) -> int:
    """Validate an encoded payload and return its exact decoded byte size."""
    if len(encoded) > MAX_STORED_TASK_EVENT_BYTES:
        raise StorageError(
            "database_integrity", "Stored task event exceeds its byte budget"
        )
    if not encoded.startswith(COMPRESSED_TASK_EVENT_MAGIC):
        if encoded.startswith(_TASK_EVENT_MAGIC_PREFIX):
            raise StorageError(
                "database_integrity",
                "Stored task event uses an unsupported codec",
            )
        return len(encoded)

    header_start = len(COMPRESSED_TASK_EVENT_MAGIC)
    payload_start = header_start + _DECODED_LENGTH.size
    if len(encoded) <= payload_start:
        raise StorageError(
            "database_integrity", "Stored compressed task event is truncated"
        )
    (decoded_length,) = _DECODED_LENGTH.unpack(
        encoded[header_start:payload_start]
    )
    if (
        decoded_length < TASK_EVENT_COMPRESSION_MIN_BYTES
        or decoded_length > MAX_DECODED_TASK_EVENT_BYTES
    ):
        raise StorageError(
            "database_integrity",
            "Stored compressed task-event length is invalid",
        )
    return decoded_length


def task_event_decoded_size(value: Any) -> int:
    """Return decoded bytes without decompressing a valid stored payload."""
    return _decoded_size_from_encoded(_payload_bytes(value))


def decode_task_event_payload(value: Any) -> bytes:
    """Decode legacy JSON or one bounded v1 task-event envelope."""
    encoded = _payload_bytes(value)
    decoded_length = _decoded_size_from_encoded(encoded)
    if not encoded.startswith(COMPRESSED_TASK_EVENT_MAGIC):
        return encoded

    payload_start = len(COMPRESSED_TASK_EVENT_MAGIC) + _DECODED_LENGTH.size

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(
            encoded[payload_start:], decoded_length + 1
        )
    except zlib.error as exc:
        raise StorageError(
            "database_integrity", "Stored compressed task event is corrupt"
        ) from exc
    if (
        len(raw) != decoded_length
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise StorageError(
            "database_integrity",
            "Stored compressed task-event length mismatched",
        )
    return raw


__all__ = [
    "COMPRESSED_TASK_EVENT_MAGIC",
    "MAX_DECODED_TASK_EVENT_BYTES",
    "MAX_STORED_TASK_EVENT_BYTES",
    "TASK_EVENT_COMPRESSION_MIN_BYTES",
    "decode_task_event_payload",
    "encode_task_event_payload",
    "task_event_decoded_size",
]
