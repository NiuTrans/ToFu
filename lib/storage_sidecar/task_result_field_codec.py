"""Bounded private compression for heavy task-result string fields.

Responsibility
--------------
``storage_records/task_results`` keeps small lifecycle and ownership facts next
to several JSON/text strings that can grow with a long task.  This module keeps
the outer task-result object and its queryable fields visible while replacing
only individually large controlled strings with a versioned JSON envelope.

Semantic operation handlers encode before persistence and hydrate at public
read boundaries.  Compact projections may request only the fields they consume,
so a status scan never decompresses a multi-MiB segment timeline.  The format is
backend-neutral for SQLite JSONDOC and PostgreSQL JSONB, and has no adapter or
database dependency.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
import zlib

import orjson

from lib.storage.protocol import MAX_FRAME_BYTES, validate_finite_json_numbers
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    encode_projection_for_storage,
)


TASK_RESULT_FIELD_CODEC_KEY = "_tofuTaskResultFieldCodec"
TASK_RESULT_FIELD_CODEC_VERSION = 1
TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES = 32 * 1024
MAX_DECODED_TASK_RESULT_FIELD_BYTES = MAX_FRAME_BYTES
MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES = MAX_FRAME_BYTES
TASK_RESULT_COMPRESSIBLE_FIELDS = (
    "segments",
    "metadata",
    "tool_rounds",
    "content",
    "thinking",
    "error",
)
_TASK_RESULT_FIELD_ENCODING = "zlib-base64"
_TASK_RESULT_FIELD_CODEC_FIELDS = frozenset(
    {"version", "encoding", "decodedBytes", "payload"}
)


class TaskResultFieldCodecError(ProjectionCodecError):
    """One private task-result field envelope is invalid."""


@dataclass(frozen=True, slots=True)
class TaskResultFieldEncoding:
    """Encoded value/document plus the fields changed by this pass."""

    stored_value: Any
    stored_document: bytes
    compressed_fields: tuple[str, ...]

    @property
    def stored_document_bytes(self) -> int:
        return len(self.stored_document)


def _codec_error(reason: str) -> TaskResultFieldCodecError:
    return TaskResultFieldCodecError(
        f"invalid stored task-result field codec: {reason}"
    )


def _serialized_document(value: Any) -> bytes:
    validate_finite_json_numbers(value)
    try:
        storage_value = encode_projection_for_storage(value)
        return orjson.dumps(storage_value, option=orjson.OPT_SORT_KEYS)
    except ProjectionCodecError:
        raise
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise _codec_error("task result is not serializable") from exc


def _encoded_text_field(text: str) -> dict[str, Any] | str:
    source = text.encode("utf-8")
    if len(source) > MAX_DECODED_TASK_RESULT_FIELD_BYTES:
        raise _codec_error("decoded field exceeds its byte budget")
    if len(source) < TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES:
        return text
    compressed = zlib.compress(source, level=1)
    payload = base64.b64encode(compressed).decode("ascii")
    if len(payload) > MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES:
        return text
    envelope = {
        TASK_RESULT_FIELD_CODEC_KEY: {
            "version": TASK_RESULT_FIELD_CODEC_VERSION,
            "encoding": _TASK_RESULT_FIELD_ENCODING,
            "decodedBytes": len(source),
            "payload": payload,
        }
    }
    encoded = orjson.dumps(envelope, option=orjson.OPT_SORT_KEYS)
    return envelope if len(encoded) < len(source) else text


def encode_task_result_fields_for_storage(
    value: Any,
    *,
    accept_stored: bool = False,
) -> TaskResultFieldEncoding:
    """Return a deterministic private representation of one task result."""
    public_value = (
        decode_task_result_fields_from_storage(value)
        if accept_stored
        else value
    )
    if not isinstance(public_value, Mapping):
        return TaskResultFieldEncoding(
            stored_value=public_value,
            stored_document=_serialized_document(public_value),
            compressed_fields=(),
        )
    stored_value: Mapping[str, Any] | dict[str, Any] = public_value
    compressed_fields: list[str] = []
    for field in TASK_RESULT_COMPRESSIBLE_FIELDS:
        candidate = public_value.get(field)
        if isinstance(candidate, Mapping) and TASK_RESULT_FIELD_CODEC_KEY in candidate:
            raise _codec_error("public field uses the reserved codec key")
        if not isinstance(candidate, str):
            continue
        encoded = _encoded_text_field(candidate)
        if encoded is candidate:
            continue
        if stored_value is public_value:
            stored_value = dict(public_value)
        stored_value[field] = encoded
        compressed_fields.append(field)
    return TaskResultFieldEncoding(
        stored_value=stored_value,
        stored_document=_serialized_document(stored_value),
        compressed_fields=tuple(compressed_fields),
    )


def _decoded_text_field(
    candidate: Mapping[str, Any],
    *,
    decoded_budget_bytes: int,
    payload_budget_bytes: int,
) -> tuple[str, int, int]:
    if set(candidate) != {TASK_RESULT_FIELD_CODEC_KEY}:
        raise _codec_error("field envelope has sibling fields")
    envelope = candidate.get(TASK_RESULT_FIELD_CODEC_KEY)
    if not isinstance(envelope, Mapping):
        raise _codec_error("field envelope is not an object")
    if set(envelope) != _TASK_RESULT_FIELD_CODEC_FIELDS:
        raise _codec_error("field envelope fields are invalid")
    version = envelope.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != TASK_RESULT_FIELD_CODEC_VERSION
    ):
        raise _codec_error("field envelope version is unsupported")
    if envelope.get("encoding") != _TASK_RESULT_FIELD_ENCODING:
        raise _codec_error("field envelope encoding is unsupported")
    decoded_bytes = envelope.get("decodedBytes")
    if (
        not isinstance(decoded_bytes, int)
        or isinstance(decoded_bytes, bool)
        or decoded_bytes < TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES
        or decoded_bytes > MAX_DECODED_TASK_RESULT_FIELD_BYTES
    ):
        raise _codec_error("decoded field length is invalid")
    if decoded_bytes > decoded_budget_bytes:
        raise _codec_error("selected decoded fields exceed their byte budget")
    payload = envelope.get("payload")
    if (
        not isinstance(payload, str)
        or not payload
        or len(payload) > MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES
    ):
        raise _codec_error("compressed field payload is invalid")
    if not payload.isascii():
        raise _codec_error("compressed field base64 is invalid")
    payload_bytes = len(payload)
    if payload_bytes > payload_budget_bytes:
        raise _codec_error("selected stored fields exceed their byte budget")
    try:
        compressed = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _codec_error("compressed field base64 is invalid") from exc
    if not compressed:
        raise _codec_error("compressed field payload is empty")
    decompressor = zlib.decompressobj()
    try:
        source = decompressor.decompress(compressed, decoded_bytes + 1)
    except zlib.error as exc:
        raise _codec_error("compressed field payload is corrupt") from exc
    if (
        len(source) != decoded_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise _codec_error("compressed field length mismatched")
    try:
        return source.decode("utf-8"), decoded_bytes, payload_bytes
    except UnicodeDecodeError as exc:
        raise _codec_error("decoded field is not UTF-8") from exc


def decode_task_result_fields_from_storage(
    value: Any,
    *,
    fields: Collection[str] | None = None,
) -> Any:
    """Hydrate all or selected controlled fields from one stored value."""
    if not isinstance(value, Mapping):
        return value
    selected = (
        frozenset(TASK_RESULT_COMPRESSIBLE_FIELDS)
        if fields is None
        else frozenset(fields)
    )
    unknown = selected - frozenset(TASK_RESULT_COMPRESSIBLE_FIELDS)
    if unknown:
        raise ValueError(
            "unknown task-result codec fields: " + ", ".join(sorted(unknown))
        )
    decoded: Mapping[str, Any] | dict[str, Any] = value
    decoded_bytes = 0
    payload_bytes = 0
    for field in TASK_RESULT_COMPRESSIBLE_FIELDS:
        if field not in selected:
            continue
        candidate = value.get(field)
        if not (
            isinstance(candidate, Mapping)
            and TASK_RESULT_FIELD_CODEC_KEY in candidate
        ):
            continue
        if decoded is value:
            decoded = dict(value)
        hydrated, decoded_size, payload_size = _decoded_text_field(
            candidate,
            decoded_budget_bytes=(
                MAX_DECODED_TASK_RESULT_FIELD_BYTES - decoded_bytes
            ),
            payload_budget_bytes=(
                MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES - payload_bytes
            ),
        )
        decoded[field] = hydrated
        decoded_bytes += decoded_size
        payload_bytes += payload_size
    return decoded


__all__ = [
    "MAX_DECODED_TASK_RESULT_FIELD_BYTES",
    "MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES",
    "TASK_RESULT_COMPRESSIBLE_FIELDS",
    "TASK_RESULT_FIELD_CODEC_KEY",
    "TASK_RESULT_FIELD_CODEC_VERSION",
    "TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES",
    "TaskResultFieldCodecError",
    "TaskResultFieldEncoding",
    "decode_task_result_fields_from_storage",
    "encode_task_result_fields_for_storage",
]
