"""Owner-scoped durable provider request/response archive operations.

The application captures and redacts bytes at the provider transport boundary;
this module owns only backend-neutral validation, quota admission, durable
storage, and bounded lazy reads. Raw archives have no TTL and are never
evicted implicitly. Their lifecycle follows the owning Attempt/Turn.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import re
import time
from typing import Any
import zlib

from lib.storage.errors import StorageError
from lib.raw_archive_contract import RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)


_MAX_ARCHIVE_STORED_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_DECODED_PART_BYTES = 64 * 1024 * 1024
_MAX_GLOBAL_BUDGET_BYTES = 4 * 1024 * 1024 * 1024
_MAX_READ_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUNCATION_REASONS = {
    "", "attempt_limit", "quota_exhausted", "secret_scrubbed",
    "transport_interrupted",
}


def _compressed_part(payload: Mapping[str, Any], key: str) -> bytes:
    encoded = payload.get(key)
    if not isinstance(encoded, str):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in raw archive request"
        )
    try:
        value = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in raw archive request"
        ) from exc
    if len(value) > _MAX_ARCHIVE_STORED_BYTES:
        raise StorageError(
            "database_protocol_error", "Raw archive part exceeds its byte budget"
        )
    return value


def _digest(payload: Mapping[str, Any], key: str) -> str:
    value = _required_text(payload, key, 64)
    if not _SHA256.fullmatch(value):
        raise StorageError(
            "database_protocol_error", f"Invalid {key} in raw archive request"
        )
    return value


def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("summary") or {}
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid raw archive summary"
        )
    result = {str(key): item for key, item in value.items()}
    if len(result) > 32 or len(_dump(result)) > 64 * 1024:
        raise StorageError(
            "database_protocol_error", "Raw archive summary exceeds its budget"
        )
    return result


def _public_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    summary = _load(row["summary_json"]) or {}
    if not isinstance(summary, Mapping):
        raise StorageError(
            "database_integrity", "Stored raw archive summary is malformed"
        )
    return {
        "archiveId": str(row["archive_id"]),
        "attemptId": str(row["attempt_id"]),
        "taskId": str(row["task_id"]),
        "turnId": str(row["turn_id"]),
        "roundNum": int(row["round_num"]),
        "transportAttempt": int(row["transport_attempt"]),
        "summary": str(summary.get("text") or "Provider request/response"),
        "byteCount": int(row["request_bytes"] or 0)
        + int(row["response_bytes"] or 0),
        "requestBytes": int(row["request_bytes"] or 0),
        "responseBytes": int(row["response_bytes"] or 0),
        "storedBytes": int(row["stored_bytes"] or 0),
        "requestSha256": str(row["request_sha256"]),
        "responseSha256": str(row["response_sha256"]),
        "sha256": str(summary.get("combinedSha256") or row["response_sha256"]),
        "integrity": str(row["integrity"]),
        "truncationReason": str(row["truncation_reason"] or ""),
        "requestAvailable": bool(row["request_blob"]),
        "responseAvailable": bool(row["response_blob"]),
        "createdAt": int(row["created_at_ms"]),
        "details": dict(summary),
    }


def _raw_archive_put(session: Session, payload: Mapping[str, Any]) -> Any:
    archive_id = _required_text(payload, "archive_id", 160)
    conversation_id = _required_text(payload, "conversation_id", 128)
    turn_id = _required_text(payload, "turn_id", 128)
    attempt_id = _required_text(payload, "attempt_id", 128)
    task_id = _required_text(payload, "task_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    round_num = _integer(payload, "round_num", minimum=1, maximum=1_000_000)
    transport_attempt = _integer(
        payload, "transport_attempt", default=0, minimum=0, maximum=10_000
    )
    request_bytes = _integer(
        payload, "request_bytes", minimum=0, maximum=1_000_000_000
    )
    response_bytes = _integer(
        payload, "response_bytes", minimum=0, maximum=1_000_000_000
    )
    request_sha256 = _digest(payload, "request_sha256")
    response_sha256 = _digest(payload, "response_sha256")
    integrity = _required_text(payload, "integrity", 16)
    if integrity not in {"complete", "partial"}:
        raise StorageError(
            "database_protocol_error", "Invalid raw archive integrity"
        )
    truncation_reason = str(payload.get("truncation_reason") or "")
    if truncation_reason not in _TRUNCATION_REASONS:
        raise StorageError(
            "database_protocol_error", "Invalid raw archive truncation reason"
        )
    summary = _summary(payload)
    request_blob = _compressed_part(payload, "request_blob_b64")
    response_blob = _compressed_part(payload, "response_blob_b64")
    requested_stored_bytes = len(request_blob) + len(response_blob)
    if requested_stored_bytes > _MAX_ARCHIVE_STORED_BYTES:
        raise StorageError(
            "database_protocol_error", "Raw archive exceeds its per-attempt budget"
        )
    budget_bytes = _integer(
        payload,
        "budget_bytes",
        minimum=1,
        maximum=_MAX_GLOBAL_BUDGET_BYTES,
    )
    min_free_bytes = _integer(
        payload, "min_free_bytes", default=0, minimum=0,
        maximum=RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES,
    )
    available_free_bytes = _integer(
        payload, "available_free_bytes", default=0, minimum=0,
        maximum=RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES,
    )

    session.lock_key("raw_archive_budget", "global")
    existing = session.fetch_one(
        "SELECT * FROM storage_raw_archives WHERE archive_id=?", (archive_id,)
    )
    if existing is not None:
        if (
            str(existing["attempt_id"]) != attempt_id
            or str(existing["request_sha256"]) != request_sha256
            or str(existing["response_sha256"]) != response_sha256
        ):
            raise StorageError(
                "database_conflict", "Raw archive identity has conflicting content"
            )
        return {**_public_metadata(existing), "idempotentReplay": True}

    owner = session.fetch_one(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE a.attempt_id=? AND a.task_id=? AND a.conversation_id=? "
        "AND a.turn_id=? AND t.user_id=?",
        (attempt_id, task_id, conversation_id, turn_id, user_id),
    )
    if owner is None:
        raise StorageError("database_not_found", "Raw archive owner not found")

    usage_row = session.fetch_one(
        "SELECT COALESCE(SUM(stored_bytes),0) AS total "
        "FROM storage_raw_archives"
    )
    current_usage = int(usage_row["total"] or 0) if usage_row else 0
    attempt_usage_row = session.fetch_one(
        "SELECT COALESCE(SUM(stored_bytes),0) AS total "
        "FROM storage_raw_archives WHERE attempt_id=?",
        (attempt_id,),
    )
    attempt_usage = (
        int(attempt_usage_row["total"] or 0) if attempt_usage_row else 0
    )
    attempt_limit_exhausted = (
        attempt_usage + requested_stored_bytes > _MAX_ARCHIVE_STORED_BYTES
    )
    quota_exhausted = current_usage + requested_stored_bytes > budget_bytes
    if (
        available_free_bytes > 0
        and available_free_bytes - requested_stored_bytes < min_free_bytes
    ):
        quota_exhausted = True
    if quota_exhausted:
        request_blob = b""
        response_blob = b""
        requested_stored_bytes = 0
        integrity = "partial"
        truncation_reason = "quota_exhausted"
    elif attempt_limit_exhausted:
        request_blob = b""
        response_blob = b""
        requested_stored_bytes = 0
        integrity = "partial"
        truncation_reason = "attempt_limit"

    created_at_ms = int(time.time() * 1000)
    session.execute(
        "INSERT INTO storage_raw_archives("
        "archive_id,user_id,conversation_id,turn_id,attempt_id,task_id,"
        "round_num,transport_attempt,request_blob,response_blob,request_bytes,"
        "response_bytes,stored_bytes,request_sha256,response_sha256,integrity,"
        "truncation_reason,summary_json,created_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            archive_id, user_id, conversation_id, turn_id, attempt_id, task_id,
            round_num, transport_attempt, request_blob, response_blob,
            request_bytes, response_bytes, requested_stored_bytes,
            request_sha256, response_sha256, integrity, truncation_reason,
            _dump(summary), created_at_ms,
        ),
    )
    row = session.fetch_one(
        "SELECT * FROM storage_raw_archives WHERE archive_id=?", (archive_id,)
    )
    return {**_public_metadata(row), "idempotentReplay": False}


def _raw_archive_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = _required_text(payload, "task_id", 256)
    limit = _integer(payload, "limit", default=16, minimum=1, maximum=64)
    values: list[Any] = [user_id, task_id]
    where = "user_id=? AND task_id=?"
    if "round_num" in payload:
        round_num = _integer(
            payload, "round_num", minimum=1, maximum=1_000_000
        )
        where += " AND round_num=?"
        values.append(round_num)
    rows = session.fetch_all(
        "SELECT * FROM storage_raw_archives WHERE " + where
        + " ORDER BY created_at_ms,archive_id LIMIT ?",
        tuple([*values, limit]),
    )
    return {"archives": [_public_metadata(row) for row in rows]}


def _decode_part(value: Any) -> bytes:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        value = value.encode("latin1")
    if not isinstance(value, bytes):
        raise StorageError(
            "database_integrity", "Stored raw archive body has invalid type"
        )
    if not value:
        return b""
    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(
            value, _MAX_ARCHIVE_DECODED_PART_BYTES + 1
        )
    except zlib.error as exc:
        raise StorageError(
            "database_integrity", "Stored raw archive body is corrupt"
        ) from exc
    if (
        len(decoded) > _MAX_ARCHIVE_DECODED_PART_BYTES
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise StorageError(
            "database_integrity", "Stored raw archive body is malformed"
        )
    return decoded


def _raw_archive_read(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    task_id = _required_text(payload, "task_id", 256)
    archive_id = _required_text(payload, "archive_id", 160)
    part = _required_text(payload, "part", 16)
    if part not in {"request", "response"}:
        raise StorageError("database_protocol_error", "Invalid raw archive part")
    offset = _integer(
        payload, "offset", default=0, minimum=0,
        maximum=_MAX_ARCHIVE_DECODED_PART_BYTES,
    )
    limit = _integer(
        payload, "limit", default=256 * 1024, minimum=1,
        maximum=_MAX_READ_CHUNK_BYTES,
    )
    row = session.fetch_one(
        "SELECT * FROM storage_raw_archives "
        "WHERE archive_id=? AND user_id=? AND task_id=?",
        (archive_id, user_id, task_id),
    )
    if row is None:
        return None
    raw = _decode_part(row[f"{part}_blob"])
    end = min(len(raw), offset + limit)
    chunk = raw[offset:end] if offset < len(raw) else b""
    return {
        "archive": _public_metadata(row),
        "part": part,
        "offset": offset,
        "nextOffset": end,
        "hasMore": end < len(raw),
        "availableBytes": len(raw),
        "dataBase64": base64.b64encode(chunk).decode("ascii"),
    }


__all__ = [
    "_raw_archive_list",
    "_raw_archive_put",
    "_raw_archive_read",
]
