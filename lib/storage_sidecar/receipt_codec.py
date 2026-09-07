"""Bounded private storage contract for exactly-once command receipts.

Responsibility
--------------
Sidecar command receipts make an acknowledged mutation replayable without
executing it twice. V2 rows replace the arbitrary command ID with a stable
SHA-256 binary key and store the canonical request digest as 32 bytes, while
retaining the operation name for growth diagnostics. Legacy rows remain
readable. Responses above 64 KiB get one deterministic zlib level-1 attempt;
stored and decoded responses remain capped at 64 KiB and 4 MiB respectively.
Corrupt, ambiguous, or unknown representations fail closed.

Entry points derive the immutable v2 identity, decode the dual-format lookup,
and encode/decode response bytes. This module has no backend or transaction
dependency, so SQLite and PostgreSQL share byte-identical behavior.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
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
_COMMAND_KEY_DOMAIN = b"tofu.command-receipt.command.v2\x00"


COMMAND_RECEIPT_LOOKUP_SQL = (
    "SELECT 'legacy' AS receipt_format, "
    "CASE WHEN operation = ? AND request_digest = ? THEN 1 ELSE 0 END "
    "AS request_matches, response_json FROM storage_command_receipts "
    "WHERE command_id = ? UNION ALL "
    "SELECT 'v2' AS receipt_format, "
    "CASE WHEN operation = ? AND request_digest = ? THEN 1 ELSE 0 END "
    "AS request_matches, response_json FROM storage_command_receipts_v2 "
    "WHERE command_key = ?"
)


def command_receipt_key_v2(command_id: str) -> bytes:
    """Return the permanent domain-separated key for one raw command ID."""
    if not isinstance(command_id, str) or not command_id:
        raise StorageError(
            "database_protocol_error", "A valid command_id is required"
        )
    return hashlib.sha256(
        _COMMAND_KEY_DOMAIN + command_id.encode("utf-8")
    ).digest()


def command_receipt_identity_v2(
    command_id: str,
    operation: str,
    request_digest: str,
) -> tuple[bytes, bytes]:
    """Return the permanent fixed-width command key and request digest.

    The command-key domain and algorithm are durable storage format, not a
    tunable hash choice. ``request_digest`` is the server's SHA-256 over the
    canonical operation and payload; preserving ``operation`` separately keeps
    the physical table attributable without weakening conflict detection.
    """
    if not isinstance(operation, str) or not operation:
        raise StorageError(
            "database_protocol_error", "A valid receipt operation is required"
        )
    if not isinstance(request_digest, str):
        raise StorageError(
            "database_protocol_error", "A valid request digest is required"
        )
    try:
        digest_bytes = bytes.fromhex(request_digest)
    except ValueError as exc:
        raise StorageError(
            "database_protocol_error", "A valid request digest is required"
        ) from exc
    if len(digest_bytes) != hashlib.sha256().digest_size:
        raise StorageError(
            "database_protocol_error", "A valid request digest is required"
        )
    return command_receipt_key_v2(command_id), digest_bytes


def decode_command_receipt_lookup(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[bool, Any]:
    """Validate one dual-format lookup and decode an exact replay response."""
    if not rows:
        return False, None
    if len(rows) != 1:
        raise StorageError(
            "database_integrity",
            "Command receipt exists in multiple storage formats",
        )
    row = rows[0]
    if row.get("receipt_format") not in {"legacy", "v2"}:
        raise StorageError(
            "database_integrity", "Stored command receipt format is invalid"
        )
    request_matches = row.get("request_matches")
    if request_matches not in (0, 1, False, True):
        raise StorageError(
            "database_integrity", "Stored command receipt match is invalid"
        )
    if not bool(request_matches):
        raise StorageError(
            "database_conflict", "command_id was reused for a different request"
        )
    return True, decode_receipt_response(row.get("response_json"))


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
    "COMMAND_RECEIPT_LOOKUP_SQL",
    "COMPRESSED_RECEIPT_MAGIC",
    "MAX_DECODED_RECEIPT_BYTES",
    "MAX_STORED_RECEIPT_BYTES",
    "command_receipt_identity_v2",
    "command_receipt_key_v2",
    "decode_command_receipt_lookup",
    "decode_receipt_response",
    "encode_receipt_response",
]
