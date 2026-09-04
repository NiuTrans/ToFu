"""Bounded private compression for frozen conversation message arrays.

Responsibility
--------------
Pre-Turn conversations retain their only durable transcript as one JSON
array.  This module keeps that top-level array and each message boundary
visible, but replaces individually large messages with a versioned JSON
envelope containing deterministic zlib level-1 bytes.  Budget-admitted
head/tail readers can therefore locate and hydrate only requested messages
instead of decompressing an entire long conversation.

The codec layers on :mod:`projection_codec`: exact segment/tool-round copies
are interned before compression and hydrated after decompression.  Every
compressed message's decoded JSON and stored base64 payload has an explicit
64 MiB ceiling;
malformed, truncated, trailing, nested, or future-version envelopes fail
closed.  The JSON representation is shared by SQLite JSONDOC and PostgreSQL
JSONB and introduces no storage-adapter dependency.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any
import zlib

import orjson

from lib.storage.protocol import MAX_FRAME_BYTES
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    STORAGE_PROJECTION_CODEC_KEY,
    decode_projection_from_storage,
    encode_projection_sequence_for_storage,
)


ARCHIVED_MESSAGE_CODEC_KEY = "_tofuArchivedMessageCodec"
ARCHIVED_MESSAGE_CODEC_VERSION = 1
ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES = 64 * 1024
MAX_DECODED_ARCHIVED_MESSAGE_BYTES = MAX_FRAME_BYTES
MAX_STORED_ARCHIVED_MESSAGE_PAYLOAD_BYTES = MAX_FRAME_BYTES
_ARCHIVED_MESSAGE_ENCODING = "zlib-base64"
_ARCHIVED_MESSAGE_CODEC_FIELDS = frozenset(
    {"version", "encoding", "decodedBytes", "payload"}
)


class ArchivedMessageCodecError(ProjectionCodecError):
    """One frozen message compression envelope is invalid."""


@dataclass(frozen=True, slots=True)
class ArchivedMessageSequenceEncoding:
    """Stored sequence plus attributable projection/compression metrics."""

    stored_document: bytes
    projection_encoded_messages: int
    compressed_messages: int
    projected_document_bytes: int

    @property
    def stored_document_bytes(self) -> int:
        """Return exact serialized bytes without retaining a second scalar."""
        return len(self.stored_document)

    @property
    def stored_messages(self) -> list[dict[str, Any]]:
        """Materialize the JSON representation only for list-valued callers."""
        value = orjson.loads(self.stored_document)
        if not isinstance(value, list):  # pragma: no cover - encoder invariant
            raise _codec_error("encoded message sequence is not an array")
        return value


def _codec_error(reason: str) -> ArchivedMessageCodecError:
    return ArchivedMessageCodecError(
        f"invalid stored archived-message codec: {reason}"
    )


def _compress_projected_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return one smaller envelope or the canonical projection unchanged."""
    if ARCHIVED_MESSAGE_CODEC_KEY in message:
        raise _codec_error("public message uses the reserved codec key")
    try:
        raw = orjson.dumps(message, option=orjson.OPT_SORT_KEYS)
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise _codec_error("message is not serializable") from exc
    if len(raw) > MAX_DECODED_ARCHIVED_MESSAGE_BYTES:
        raise _codec_error("decoded message exceeds its byte budget")
    if len(raw) < ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES:
        return message

    compressed = zlib.compress(raw, level=1)
    envelope = {
        ARCHIVED_MESSAGE_CODEC_KEY: {
            "version": ARCHIVED_MESSAGE_CODEC_VERSION,
            "encoding": _ARCHIVED_MESSAGE_ENCODING,
            "decodedBytes": len(raw),
            "payload": base64.b64encode(compressed).decode("ascii"),
        }
    }
    encoded = orjson.dumps(envelope, option=orjson.OPT_SORT_KEYS)
    return envelope if len(encoded) < len(raw) else message


def encode_archived_message_sequence_with_metrics(
    messages: Any,
    *,
    accept_stored: bool = False,
) -> ArchivedMessageSequenceEncoding:
    """Encode one frozen message list and expose exact storage attribution.

    ``accept_stored`` is reserved for idempotent offline maintenance.  Normal
    callers must provide public messages and cannot inject a private envelope.
    """
    public_messages = (
        decode_archived_message_sequence_from_storage(messages)
        if accept_stored
        else messages
    )
    if not isinstance(public_messages, list):
        raise _codec_error("message sequence is not an array")
    for message in public_messages:
        if not isinstance(message, dict):
            raise _codec_error("message sequence member is not an object")
        if ARCHIVED_MESSAGE_CODEC_KEY in message:
            raise _codec_error("public message uses the reserved codec key")

    projected_messages = encode_projection_sequence_for_storage(
        public_messages
    )
    stored_messages = [
        _compress_projected_message(message)
        for message in projected_messages
    ]
    projection_encoded_messages = sum(
        STORAGE_PROJECTION_CODEC_KEY in message
        for message in projected_messages
    )
    compressed_messages = sum(
        ARCHIVED_MESSAGE_CODEC_KEY in message
        for message in stored_messages
    )
    try:
        projected_document_bytes = len(
            orjson.dumps(projected_messages, option=orjson.OPT_SORT_KEYS)
        )
        del projected_messages
        stored_document = orjson.dumps(
            stored_messages, option=orjson.OPT_SORT_KEYS
        )
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise _codec_error("message sequence is not serializable") from exc
    return ArchivedMessageSequenceEncoding(
        stored_document=stored_document,
        projection_encoded_messages=projection_encoded_messages,
        compressed_messages=compressed_messages,
        projected_document_bytes=projected_document_bytes,
    )


def encode_archived_message_sequence_for_storage(
    messages: Any,
    *,
    accept_stored: bool = False,
) -> list[dict[str, Any]]:
    """Return the deterministic private representation of a frozen archive."""
    return encode_archived_message_sequence_with_metrics(
        messages, accept_stored=accept_stored
    ).stored_messages


def _decode_compressed_message(message: dict[str, Any]) -> dict[str, Any]:
    if set(message) != {ARCHIVED_MESSAGE_CODEC_KEY}:
        raise _codec_error("compression envelope has sibling fields")
    envelope = message.get(ARCHIVED_MESSAGE_CODEC_KEY)
    if not isinstance(envelope, dict):
        raise _codec_error("compression envelope is not an object")
    if set(envelope) != _ARCHIVED_MESSAGE_CODEC_FIELDS:
        raise _codec_error("compression envelope fields are invalid")
    version = envelope.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != ARCHIVED_MESSAGE_CODEC_VERSION
    ):
        raise _codec_error("compression envelope version is unsupported")
    if envelope.get("encoding") != _ARCHIVED_MESSAGE_ENCODING:
        raise _codec_error("compression envelope encoding is unsupported")
    decoded_bytes = envelope.get("decodedBytes")
    if (
        not isinstance(decoded_bytes, int)
        or isinstance(decoded_bytes, bool)
        or decoded_bytes < ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES
        or decoded_bytes > MAX_DECODED_ARCHIVED_MESSAGE_BYTES
    ):
        raise _codec_error("decoded message length is invalid")
    payload = envelope.get("payload")
    if (
        not isinstance(payload, str)
        or not payload
        or len(payload) > MAX_STORED_ARCHIVED_MESSAGE_PAYLOAD_BYTES
    ):
        raise _codec_error("compressed message payload is invalid")
    try:
        compressed = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _codec_error("compressed message base64 is invalid") from exc
    if not compressed:
        raise _codec_error("compressed message payload is empty")

    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed, decoded_bytes + 1)
    except zlib.error as exc:
        raise _codec_error("compressed message payload is corrupt") from exc
    if (
        len(raw) != decoded_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise _codec_error("compressed message length mismatched")
    try:
        decoded = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise _codec_error("decoded message JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise _codec_error("decoded message is not an object")
    if ARCHIVED_MESSAGE_CODEC_KEY in decoded:
        raise _codec_error("nested compression envelope is invalid")
    return decoded


def decode_archived_message_sequence_from_storage(
    messages: Any,
) -> list[dict[str, Any]]:
    """Hydrate every legacy, projected, or compressed archive message."""
    if not isinstance(messages, list):
        raise _codec_error("message sequence is not an array")
    decoded_messages: list[dict[str, Any]] = []
    for candidate in messages:
        if not isinstance(candidate, dict):
            raise _codec_error("message sequence member is not an object")
        projected = (
            _decode_compressed_message(candidate)
            if ARCHIVED_MESSAGE_CODEC_KEY in candidate
            else candidate
        )
        decoded = decode_projection_from_storage(projected)
        if not isinstance(decoded, dict):
            raise _codec_error("decoded message is not an object")
        decoded_messages.append(decoded)
    return decoded_messages


__all__ = [
    "ARCHIVED_MESSAGE_CODEC_KEY",
    "ARCHIVED_MESSAGE_CODEC_VERSION",
    "ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES",
    "ArchivedMessageCodecError",
    "ArchivedMessageSequenceEncoding",
    "MAX_DECODED_ARCHIVED_MESSAGE_BYTES",
    "MAX_STORED_ARCHIVED_MESSAGE_PAYLOAD_BYTES",
    "decode_archived_message_sequence_from_storage",
    "encode_archived_message_sequence_for_storage",
    "encode_archived_message_sequence_with_metrics",
]
