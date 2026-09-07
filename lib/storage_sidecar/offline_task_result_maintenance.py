"""Bounded SQLite backfill for the production task-result field codec.

Responsibility
--------------
Physical offline deep-clean calls :func:`maintain_task_result_storage` to
re-encode large historical ``storage_records/task_results`` documents with the
runtime field codec.  Public task-result values and record versions stay fixed;
only a strictly smaller, canonical-round-trip-proven private document may
replace its source.

The caller owns the stopped-server project lease and physical reclaim window.
Selection is metadata-first, source pages/documents are byte-bounded, every
write is version/length CAS-fenced, and malformed or over-budget rows remain
byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import orjson

from lib.storage_sidecar import offline_maintenance as _SQLITE_TOOLING
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.projection_codec import ProjectionCodecError
from lib.storage_sidecar.task_result_field_codec import (
    TASK_RESULT_COMPRESSIBLE_FIELDS,
    TASK_RESULT_FIELD_CODEC_KEY,
    TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
    decode_task_result_fields_from_storage,
    encode_task_result_fields_for_storage,
)


TASK_RESULT_MAINTENANCE_SELECT_ROWS = 64
TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES = 64 * 1024 * 1024
TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES = 64 * 1024 * 1024
_TASK_RESULT_NAMESPACE = "task_results"


@dataclass(frozen=True, slots=True)
class _TaskResultCandidate:
    record_key: str
    version: int
    source_bytes: int


@dataclass(frozen=True, slots=True)
class _TaskResultUpdate:
    candidate: _TaskResultCandidate
    stored_document: bytes
    compressed_fields: tuple[str, ...]


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    if table not in _SQLITE_TOOLING.sqlite_schema_names(connection, "table"):
        return set()
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _payload_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ProjectionCodecError(
        "task-result document has an invalid SQLite value type"
    )


def _private_fields(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        field
        for field in TASK_RESULT_COMPRESSIBLE_FIELDS
        if isinstance(value.get(field), Mapping)
        and TASK_RESULT_FIELD_CODEC_KEY in value[field]
    )


def _compact_task_result_document(
    raw: bytes,
) -> tuple[bytes, tuple[str, ...], tuple[str, ...]]:
    stored_value = orjson.loads(raw)
    already_encoded_fields = _private_fields(stored_value)
    public_value = decode_task_result_fields_from_storage(stored_value)
    public_document = orjson.dumps(
        public_value, option=orjson.OPT_SORT_KEYS
    )
    encoding = encode_task_result_fields_for_storage(public_value)
    del public_value
    hydrated = decode_task_result_fields_from_storage(encoding.stored_value)
    if (
        orjson.dumps(hydrated, option=orjson.OPT_SORT_KEYS)
        != public_document
    ):
        raise ProjectionCodecError(
            "task-result field codec round-trip mismatched"
        )
    return (
        encoding.stored_document,
        encoding.compressed_fields,
        already_encoded_fields,
    )


def maintain_task_result_storage(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict[str, Any]:
    """Backfill large task results with bounded, version-neutral CAS writes."""
    required = {"namespace", "record_key", "value_json", "version"}
    if not required <= _table_columns(connection, "storage_records"):
        return {"mode": "unsupported_schema", "updated_rows": 0}
    report: dict[str, Any] = {
        "mode": "lossless_task_result_field_codec",
        "minimum_document_bytes": TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
        "scanned_rows": 0,
        "scanned_payload_bytes": 0,
        "already_encoded_rows": 0,
        "already_encoded_fields": 0,
        "unchanged_rows": 0,
        "invalid_rows": 0,
        "oversize_rows": 0,
        "updated_rows": 0,
        "updated_input_bytes": 0,
        "updated_stored_bytes": 0,
        "saved_bytes": 0,
        "selection_pages": 0,
        "write_batches": 0,
        "max_page_payload_bytes": 0,
        "selection_row_limit": TASK_RESULT_MAINTENANCE_SELECT_ROWS,
        "page_payload_budget_bytes": (
            TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES
        ),
        "document_budget_bytes": TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES,
        "compressed_field_rows": {
            field: 0 for field in TASK_RESULT_COMPRESSIBLE_FIELDS
        },
    }
    last_record_key = ""
    while True:
        rows = connection.execute(
            "SELECT record_key,version,length(CAST(value_json AS BLOB)) "
            "FROM storage_records WHERE namespace=? AND record_key>? "
            "AND length(CAST(value_json AS BLOB))>=? "
            "ORDER BY record_key LIMIT ?",
            (
                _TASK_RESULT_NAMESPACE,
                last_record_key,
                TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
                TASK_RESULT_MAINTENANCE_SELECT_ROWS,
            ),
        ).fetchall()
        if not rows:
            break
        report["selection_pages"] += 1
        selected: list[_TaskResultCandidate] = []
        page_bytes = 0
        for row in rows:
            candidate = _TaskResultCandidate(
                record_key=str(row[0]),
                version=int(row[1]),
                source_bytes=max(0, int(row[2] or 0)),
            )
            if candidate.source_bytes > TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES:
                report["oversize_rows"] += 1
                last_record_key = candidate.record_key
                continue
            if (
                selected
                and page_bytes + candidate.source_bytes
                > TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES
            ):
                break
            selected.append(candidate)
            page_bytes += candidate.source_bytes
            last_record_key = candidate.record_key
            if page_bytes >= TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES:
                break
        if not selected:
            continue
        report["scanned_rows"] += len(selected)
        report["scanned_payload_bytes"] += page_bytes
        report["max_page_payload_bytes"] = max(
            report["max_page_payload_bytes"], page_bytes
        )
        updates: list[_TaskResultUpdate] = []
        for candidate in selected:
            row = connection.execute(
                "SELECT CAST(value_json AS BLOB) FROM storage_records "
                "WHERE namespace=? AND record_key=? AND version=?",
                (
                    _TASK_RESULT_NAMESPACE,
                    candidate.record_key,
                    candidate.version,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "task-result row disappeared during maintenance"
                )
            raw = _payload_bytes(row[0])
            if len(raw) != candidate.source_bytes:
                raise RuntimeError(
                    "task-result length changed during maintenance"
                )
            try:
                stored, compressed_fields, already_encoded_fields = (
                    _compact_task_result_document(raw)
                )
            except (
                ProjectionCodecError,
                orjson.JSONDecodeError,
                orjson.JSONEncodeError,
                TypeError,
                ValueError,
            ):
                report["invalid_rows"] += 1
                continue
            if already_encoded_fields:
                report["already_encoded_rows"] += 1
                report["already_encoded_fields"] += len(
                    already_encoded_fields
                )
            if len(stored) >= len(raw):
                report["unchanged_rows"] += 1
                continue
            updates.append(_TaskResultUpdate(
                candidate=candidate,
                stored_document=stored,
                compressed_fields=compressed_fields,
            ))
        if not updates:
            continue

        def _write_batch(conn: sqlite3.Connection) -> int:
            updated = 0
            for update in updates:
                candidate = update.candidate
                cursor = conn.execute(
                    "UPDATE storage_records SET value_json=? "
                    "WHERE namespace=? AND record_key=? AND version=? "
                    "AND length(CAST(value_json AS BLOB))=?",
                    (
                        update.stored_document,
                        _TASK_RESULT_NAMESPACE,
                        candidate.record_key,
                        candidate.version,
                        candidate.source_bytes,
                    ),
                )
                changed = max(0, int(cursor.rowcount))
                if changed != 1:
                    raise RuntimeError(
                        "task-result codec update count mismatched"
                    )
                updated += changed
            return updated

        updated = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose="historical task-result field codec backfill",
            operation=_write_batch,
        )
        input_bytes = sum(update.candidate.source_bytes for update in updates)
        stored_bytes = sum(len(update.stored_document) for update in updates)
        report["updated_rows"] += int(updated)
        report["updated_input_bytes"] += input_bytes
        report["updated_stored_bytes"] += stored_bytes
        report["saved_bytes"] += input_bytes - stored_bytes
        for update in updates:
            for field in update.compressed_fields:
                report["compressed_field_rows"][field] += 1
        report["write_batches"] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
    return report


__all__ = [
    "TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES",
    "TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES",
    "TASK_RESULT_MAINTENANCE_SELECT_ROWS",
    "maintain_task_result_storage",
]
