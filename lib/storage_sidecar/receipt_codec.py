"""Bounded private codec for exactly-once command response receipts.

Responsibility
--------------
Sidecar command receipts make an acknowledged mutation replayable without
executing it twice. Most responses stay as their legacy canonical JSON bytes.
Responses that exceed the permanent 64 KiB receipt budget get one deterministic
zlib level-1 attempt; the stored blob remains capped at 64 KiB and the decoded
response remains capped at 4 MiB. Corrupt or unknown envelopes fail closed.

Entry points are :func:`encode_receipt_response` and
:func:`decode_receipt_response`. This module has no backend or transaction
dependency, so SQLite and PostgreSQL share byte-identical behavior.
"""

from __future__ import annotations

import struct
from typing import Any
import zlib

import orjson

from lib.storage.errors import StorageError


MAX_STORED_RECEIPT_BYTES = 64 * 1024
MAX_DECODED_RECEIPT_BYTES = 4 * 1024 * 1024
COMPRESSED_RECEIPT_MAGIC = b"tofu.receipt.zlib.v1\x00"
_RECEIPT_MAGIC_PREFIX = b"tofu.receipt."
_DECODED_LENGTH = struct.Struct("!I")


def encode_receipt_response(response: Any) -> bytes:
    """Encode one command response without weakening either hard byte cap."""
    try:
        raw = orjson.dumps(response, option=orjson.OPT_SORT_KEYS)
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            "database_protocol_error",
            "Command response is not serializable for a receipt",
        ) from exc
    if len(raw) <= MAX_STORED_RECEIPT_BYTES:
        # Preserve every existing receipt byte and its replay path.
        return raw
    if len(raw) > MAX_DECODED_RECEIPT_BYTES:
        raise StorageError(
            "database_protocol_error",
            "Command response exceeds the decoded receipt budget",
        )

    encoded = (
        COMPRESSED_RECEIPT_MAGIC
        + _DECODED_LENGTH.pack(len(raw))
        + zlib.compress(raw, level=1)
    )
    if len(encoded) > MAX_STORED_RECEIPT_BYTES:
        raise StorageError(
            "database_protocol_error",
            "Command response is too large for a receipt",
        )
    return encoded


def _receipt_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise StorageError(
        "database_integrity", "Stored command receipt has an invalid type"
    )


def _decode_json(value: bytes) -> Any:
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError as exc:
        raise StorageError(
            "database_integrity", "Stored command receipt JSON is invalid"
        ) from exc


def decode_receipt_response(value: Any) -> Any:
    """Decode a legacy JSON or v1 compressed receipt with bounded output."""
    encoded = _receipt_bytes(value)
    if len(encoded) > MAX_STORED_RECEIPT_BYTES:
        raise StorageError(
            "database_integrity", "Stored command receipt exceeds its byte budget"
        )
    if not encoded.startswith(COMPRESSED_RECEIPT_MAGIC):
        if encoded.startswith(_RECEIPT_MAGIC_PREFIX):
            raise StorageError(
                "database_integrity",
                "Stored command receipt uses an unsupported codec",
            )
        return _decode_json(encoded)

    header_start = len(COMPRESSED_RECEIPT_MAGIC)
    payload_start = header_start + _DECODED_LENGTH.size
    if len(encoded) <= payload_start:
        raise StorageError(
            "database_integrity", "Stored compressed receipt is truncated"
        )
    (decoded_length,) = _DECODED_LENGTH.unpack(
        encoded[header_start:payload_start]
    )
    if (
        decoded_length <= MAX_STORED_RECEIPT_BYTES
        or decoded_length > MAX_DECODED_RECEIPT_BYTES
    ):
        raise StorageError(
            "database_integrity", "Stored compressed receipt length is invalid"
        )

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(
            encoded[payload_start:], decoded_length + 1
        )
    except zlib.error as exc:
        raise StorageError(
            "database_integrity", "Stored compressed receipt is corrupt"
        ) from exc
    if (
        len(raw) != decoded_length
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise StorageError(
            "database_integrity", "Stored compressed receipt length mismatched"
        )
    return _decode_json(raw)


__all__ = [
    "COMPRESSED_RECEIPT_MAGIC",
    "MAX_DECODED_RECEIPT_BYTES",
    "MAX_STORED_RECEIPT_BYTES",
    "decode_receipt_response",
    "encode_receipt_response",
]
