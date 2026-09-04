"""Bounded SQLite maintenance for durable compaction transcript archives.

Responsibility
--------------
Physical offline deep-clean calls :func:`maintain_compaction_archive_storage`
to (1) backfill the production per-message codec on current owner-scoped
archives and (2) migrate the exact retired ``storage_records``
``transcript_archive`` shape into that authority.  This module never invents
an owner: legacy records resolve through active or recoverable-trash headers,
and missing/ambiguous/conflicting evidence stays byte-identical.

The caller owns the stopped-server project lease and physical reclaim window.
This module bounds source pages/documents, verifies canonical public-message
round trips, CAS-fences every mutation, and checkpoints the WAL per write page.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping

import orjson

from lib.storage_sidecar import offline_maintenance as _SQLITE_TOOLING
from lib.storage_sidecar.archived_message_codec import (
    ARCHIVED_MESSAGE_CODEC_KEY,
    decode_archived_message_sequence_from_storage,
    encode_archived_message_sequence_with_metrics,
)
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    STORAGE_PROJECTION_CODEC_KEY,
)


ARCHIVE_MAINTENANCE_SELECT_ROWS = 64
ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES = 64 * 1024 * 1024
ARCHIVE_MAINTENANCE_DOCUMENT_BYTES = 64 * 1024 * 1024
ARCHIVE_CODEC_MIN_DOCUMENT_BYTES = 64 * 1024
_LEGACY_NAMESPACE = "transcript_archive"
_LEGACY_FIELDS = frozenset({
    "archive_id",
    "conv_id",
    "created_at_ms",
    "messages",
    "model",
    "msgs_after",
    "msgs_before",
    "reason",
    "round_num",
    "summary",
    "task_id",
    "tokens_after",
    "tokens_before",
    "trigger",
})
_CURRENT_ARCHIVE_COLUMNS = {
    "archive_id",
    "conversation_id",
    "user_id",
    "messages_json",
    "summary",
    "receipt_json",
    "trigger",
    "task_id",
    "round_num",
    "model",
    "tokens_before",
    "tokens_after",
    "msgs_before",
    "msgs_after",
    "reason",
    "payload_size",
    "created_at_ms",
}


class LegacyCompactionArchiveError(ValueError):
    """The retired archive record cannot be migrated without information loss."""


@dataclass(frozen=True, slots=True)
class _CurrentArchiveCandidate:
    archive_id: str
    conversation_id: str
    user_id: int
    source_bytes: int


@dataclass(frozen=True, slots=True)
class _CurrentArchiveUpdate:
    candidate: _CurrentArchiveCandidate
    stored_document: bytes


@dataclass(frozen=True, slots=True)
class LegacyCompactionArchive:
    """One validated retired record translated to current archive columns."""

    record_key: str
    archive_id: str
    conversation_id: str
    stored_messages_document: bytes
    public_messages_digest: bytes
    summary: str
    trigger: str
    task_id: str
    round_num: int
    model: str
    tokens_before: int
    tokens_after: int
    msgs_before: int
    msgs_after: int
    reason: str
    created_at_ms: int

    def target_values(self, user_id: int) -> tuple[Any, ...]:
        """Return the exact current-table insert tuple for one resolved owner."""
        return (
            self.archive_id,
            self.conversation_id,
            int(user_id),
            self.stored_messages_document,
            self.summary,
            b"{}",
            self.trigger,
            self.task_id,
            self.round_num,
            self.model,
            self.tokens_before,
            self.tokens_after,
            self.msgs_before,
            self.msgs_after,
            self.reason,
            len(self.stored_messages_document),
            self.created_at_ms,
        )


@dataclass(frozen=True, slots=True)
class _LegacyMigrationCandidate:
    archive: LegacyCompactionArchive
    user_id: int
    source_version: int
    source_bytes: int


@dataclass(frozen=True, slots=True)
class _LegacyMigrationWriteResult:
    migrated_rows: int
    inserted_target_rows: int
    matched_target_rows: int
    conflicting_target_rows: int
    oversize_target_rows: int
    retired_source_bytes: int
    target_message_bytes: int


_CurrentTargetStatus = Literal["missing", "loaded", "oversize"]


def _table_columns(
    connection: sqlite3.Connection, table: str,
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
    raise LegacyCompactionArchiveError(
        "archive payload has an invalid SQLite value type"
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return orjson.loads(_payload_bytes(value))


def _bounded_text(
    value: Any, field: str, maximum: int, *, required: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise LegacyCompactionArchiveError(
            f"legacy archive {field} is invalid"
        )
    if required and not value:
        raise LegacyCompactionArchiveError(
            f"legacy archive {field} is empty"
        )
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LegacyCompactionArchiveError(
            f"legacy archive {field} is invalid"
        )
    return value


def _encode_public_messages(messages: Any) -> tuple[bytes, bytes]:
    """Return stored bytes and a fixed canonical parity witness."""
    public_messages = decode_archived_message_sequence_from_storage(messages)
    public_document = orjson.dumps(
        public_messages, option=orjson.OPT_SORT_KEYS
    )
    encoding = encode_archived_message_sequence_with_metrics(public_messages)
    del public_messages
    hydrated = decode_archived_message_sequence_from_storage(
        orjson.loads(encoding.stored_document)
    )
    if orjson.dumps(hydrated, option=orjson.OPT_SORT_KEYS) != public_document:
        raise LegacyCompactionArchiveError(
            "archive message codec round-trip mismatched"
        )
    return encoding.stored_document, hashlib.sha256(public_document).digest()


def decode_legacy_compaction_archive(
    record_key: str, raw_value: bytes,
) -> LegacyCompactionArchive:
    """Validate and translate the exact retired generic-record archive shape."""
    try:
        value = orjson.loads(raw_value)
    except orjson.JSONDecodeError as exc:
        raise LegacyCompactionArchiveError(
            "legacy archive JSON is invalid"
        ) from exc
    if not isinstance(value, dict) or set(value) != _LEGACY_FIELDS:
        raise LegacyCompactionArchiveError(
            "legacy archive fields are not the frozen supported shape"
        )
    archive_id = _bounded_text(
        value["archive_id"], "archive_id", 128, required=True
    )
    conversation_id = _bounded_text(
        value["conv_id"], "conv_id", 512, required=True
    )
    if record_key != f"{conversation_id}:{archive_id}":
        raise LegacyCompactionArchiveError(
            "legacy archive record key does not match its identity"
        )
    messages = value["messages"]
    if not isinstance(messages, list):
        raise LegacyCompactionArchiveError(
            "legacy archive messages are not an array"
        )
    try:
        stored_messages, public_messages = _encode_public_messages(messages)
    except (ProjectionCodecError, TypeError, ValueError) as exc:
        raise LegacyCompactionArchiveError(
            "legacy archive messages are invalid"
        ) from exc
    return LegacyCompactionArchive(
        record_key=record_key,
        archive_id=archive_id,
        conversation_id=conversation_id,
        stored_messages_document=stored_messages,
        public_messages_digest=public_messages,
        summary=_bounded_text(value["summary"], "summary", 200_000),
        trigger=_bounded_text(value["trigger"], "trigger", 32) or "force",
        task_id=_bounded_text(value["task_id"], "task_id", 512),
        round_num=_nonnegative_integer(value["round_num"], "round_num"),
        model=_bounded_text(value["model"], "model", 256),
        tokens_before=_nonnegative_integer(
            value["tokens_before"], "tokens_before"
        ),
        tokens_after=_nonnegative_integer(
            value["tokens_after"], "tokens_after"
        ),
        msgs_before=_nonnegative_integer(value["msgs_before"], "msgs_before"),
        msgs_after=_nonnegative_integer(value["msgs_after"], "msgs_after"),
        reason=_bounded_text(value["reason"], "reason", 500),
        created_at_ms=_nonnegative_integer(
            value["created_at_ms"], "created_at_ms"
        ),
    )


def _current_archive_matches(
    row: Mapping[str, Any],
    archive: LegacyCompactionArchive,
    user_id: int,
) -> bool:
    """Fail closed unless a target row preserves every legacy public fact."""
    try:
        stored_messages = _json_value(row["messages_json"])
        public_messages = decode_archived_message_sequence_from_storage(
            stored_messages
        )
        public_digest = hashlib.sha256(orjson.dumps(
            public_messages, option=orjson.OPT_SORT_KEYS
        )).digest()
        receipt = _json_value(row["receipt_json"])
        return (
            str(row["archive_id"]) == archive.archive_id
            and str(row["conversation_id"]) == archive.conversation_id
            and int(row["user_id"]) == user_id
            and public_digest == archive.public_messages_digest
            and str(row["summary"] or "") == archive.summary
            and receipt == {}
            and str(row["trigger"] or "force") == archive.trigger
            and str(row["task_id"] or "") == archive.task_id
            and int(row["round_num"] or 0) == archive.round_num
            and str(row["model"] or "") == archive.model
            and int(row["tokens_before"] or 0) == archive.tokens_before
            and int(row["tokens_after"] or 0) == archive.tokens_after
            and int(row["msgs_before"] or 0) == archive.msgs_before
            and int(row["msgs_after"] or 0) == archive.msgs_after
            and str(row["reason"] or "") == archive.reason
            and int(row["created_at_ms"]) == archive.created_at_ms
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        orjson.JSONDecodeError,
        ProjectionCodecError,
    ):
        return False


def _bounded_current_archive_target(
    connection: sqlite3.Connection,
    archive_id: str,
) -> tuple[_CurrentTargetStatus, Mapping[str, Any] | None]:
    """Load a target only after its message document passes the byte budget."""
    length_row = connection.execute(
        "SELECT length(CAST(messages_json AS BLOB)) "
        "FROM storage_compaction_archives WHERE archive_id=?",
        (archive_id,),
    ).fetchone()
    if length_row is None:
        return "missing", None
    if max(0, int(length_row[0] or 0)) > ARCHIVE_MAINTENANCE_DOCUMENT_BYTES:
        return "oversize", None
    row = connection.execute(
        "SELECT * FROM storage_compaction_archives WHERE archive_id=?",
        (archive_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "compaction archive target disappeared during maintenance"
        )
    return "loaded", row


def _write_legacy_migration_batch(
    connection: sqlite3.Connection,
    migrations: list[_LegacyMigrationCandidate],
) -> _LegacyMigrationWriteResult:
    """Atomically publish safe targets and retire only their exact sources."""
    migrated = 0
    inserted = 0
    matched = 0
    conflicts = 0
    oversize_targets = 0
    retired_bytes = 0
    target_bytes = 0
    for migration in migrations:
        archive = migration.archive
        target_status, target = _bounded_current_archive_target(
            connection, archive.archive_id
        )
        if target_status == "oversize":
            oversize_targets += 1
            continue
        if target_status == "loaded":
            if target is None or not _current_archive_matches(
                target, archive, migration.user_id
            ):
                conflicts += 1
                continue
            matched += 1
        else:
            cursor = connection.execute(
                "INSERT INTO storage_compaction_archives("
                "archive_id,conversation_id,user_id,messages_json,"
                "summary,receipt_json,trigger,task_id,round_num,model,"
                "tokens_before,tokens_after,msgs_before,msgs_after,"
                "reason,payload_size,created_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(archive_id) DO NOTHING",
                archive.target_values(migration.user_id),
            )
            changed = max(0, int(cursor.rowcount))
            if changed != 1:
                raise RuntimeError(
                    "legacy archive target insert count mismatched"
                )
            inserted += 1
            target_bytes += len(archive.stored_messages_document)
        cursor = connection.execute(
            "DELETE FROM storage_records WHERE namespace=? "
            "AND record_key=? AND version=? "
            "AND length(CAST(value_json AS BLOB))=?",
            (
                _LEGACY_NAMESPACE,
                archive.record_key,
                migration.source_version,
                migration.source_bytes,
            ),
        )
        deleted = max(0, int(cursor.rowcount))
        if deleted != 1:
            raise RuntimeError(
                "legacy archive source delete count mismatched"
            )
        migrated += 1
        retired_bytes += migration.source_bytes
    return _LegacyMigrationWriteResult(
        migrated_rows=migrated,
        inserted_target_rows=inserted,
        matched_target_rows=matched,
        conflicting_target_rows=conflicts,
        oversize_target_rows=oversize_targets,
        retired_source_bytes=retired_bytes,
        target_message_bytes=target_bytes,
    )


def _owner_ids(
    connection: sqlite3.Connection,
    conversation_id: str,
    *,
    trash_available: bool,
) -> list[int]:
    owners = {
        int(row[0])
        for row in connection.execute(
            "SELECT user_id FROM storage_conversations WHERE id=?",
            (conversation_id,),
        )
    }
    if trash_available:
        owners.update(
            int(row[0])
            for row in connection.execute(
                "SELECT user_id FROM storage_conversation_trash "
                "WHERE conversation_id=?",
                (conversation_id,),
            )
        )
    return sorted(owners)


def _compact_current_archive_document(raw: bytes) -> tuple[bytes, bool]:
    stored = orjson.loads(raw)
    already_encoded = (
        isinstance(stored, list)
        and any(
            isinstance(message, dict)
            and (
                ARCHIVED_MESSAGE_CODEC_KEY in message
                or STORAGE_PROJECTION_CODEC_KEY in message
            )
            for message in stored
        )
    )
    encoded, _public = _encode_public_messages(stored)
    return encoded, already_encoded


def _maintain_current_archives(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict[str, Any]:
    required = {
        "archive_id", "conversation_id", "user_id", "messages_json",
        "payload_size",
    }
    if not required <= _table_columns(
        connection, "storage_compaction_archives"
    ):
        return {"mode": "unsupported_schema", "updated_rows": 0}
    report: dict[str, Any] = {
        "mode": "lossless_per_message_archive_codec",
        "minimum_document_bytes": ARCHIVE_CODEC_MIN_DOCUMENT_BYTES,
        "scanned_rows": 0,
        "scanned_payload_bytes": 0,
        "already_encoded_rows": 0,
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
        "selection_row_limit": ARCHIVE_MAINTENANCE_SELECT_ROWS,
        "page_payload_budget_bytes": ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES,
        "document_budget_bytes": ARCHIVE_MAINTENANCE_DOCUMENT_BYTES,
    }
    last_archive_id = ""
    while True:
        rows = connection.execute(
            "SELECT archive_id,conversation_id,user_id,"
            "length(CAST(messages_json AS BLOB)) "
            "FROM storage_compaction_archives WHERE archive_id>? "
            "AND length(CAST(messages_json AS BLOB))>=? "
            "ORDER BY archive_id LIMIT ?",
            (
                last_archive_id,
                ARCHIVE_CODEC_MIN_DOCUMENT_BYTES,
                ARCHIVE_MAINTENANCE_SELECT_ROWS,
            ),
        ).fetchall()
        if not rows:
            break
        report["selection_pages"] += 1
        selected: list[_CurrentArchiveCandidate] = []
        page_bytes = 0
        for row in rows:
            candidate = _CurrentArchiveCandidate(
                archive_id=str(row[0]),
                conversation_id=str(row[1]),
                user_id=int(row[2]),
                source_bytes=max(0, int(row[3] or 0)),
            )
            if candidate.source_bytes > ARCHIVE_MAINTENANCE_DOCUMENT_BYTES:
                report["oversize_rows"] += 1
                last_archive_id = candidate.archive_id
                continue
            if (
                selected
                and page_bytes + candidate.source_bytes
                > ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES
            ):
                break
            selected.append(candidate)
            page_bytes += candidate.source_bytes
            last_archive_id = candidate.archive_id
            if page_bytes >= ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES:
                break
        if not selected:
            continue
        report["scanned_rows"] += len(selected)
        report["scanned_payload_bytes"] += page_bytes
        report["max_page_payload_bytes"] = max(
            report["max_page_payload_bytes"], page_bytes
        )
        updates: list[_CurrentArchiveUpdate] = []
        for candidate in selected:
            row = connection.execute(
                "SELECT CAST(messages_json AS BLOB) "
                "FROM storage_compaction_archives WHERE archive_id=? "
                "AND conversation_id=? AND user_id=?",
                (
                    candidate.archive_id,
                    candidate.conversation_id,
                    candidate.user_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "compaction archive disappeared during maintenance"
                )
            raw = _payload_bytes(row[0])
            if len(raw) != candidate.source_bytes:
                raise RuntimeError(
                    "compaction archive length changed during maintenance"
                )
            try:
                stored_document, already_encoded = (
                    _compact_current_archive_document(raw)
                )
            except (
                LegacyCompactionArchiveError,
                ProjectionCodecError,
                orjson.JSONDecodeError,
                orjson.JSONEncodeError,
                TypeError,
                ValueError,
            ):
                report["invalid_rows"] += 1
                continue
            if already_encoded:
                report["already_encoded_rows"] += 1
            if len(stored_document) >= len(raw):
                report["unchanged_rows"] += 1
                continue
            updates.append(_CurrentArchiveUpdate(candidate, stored_document))
        if not updates:
            continue

        def _write_batch(conn: sqlite3.Connection) -> int:
            updated = 0
            for update in updates:
                candidate = update.candidate
                cursor = conn.execute(
                    "UPDATE storage_compaction_archives "
                    "SET messages_json=?,payload_size=? WHERE archive_id=? "
                    "AND conversation_id=? AND user_id=? "
                    "AND length(CAST(messages_json AS BLOB))=?",
                    (
                        update.stored_document,
                        len(update.stored_document),
                        candidate.archive_id,
                        candidate.conversation_id,
                        candidate.user_id,
                        candidate.source_bytes,
                    ),
                )
                changed = max(0, int(cursor.rowcount))
                if changed != 1:
                    raise RuntimeError(
                        "compaction archive codec update count mismatched"
                    )
                updated += changed
            return updated

        updated = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose="compaction archive codec maintenance",
            operation=_write_batch,
        )
        input_bytes = sum(update.candidate.source_bytes for update in updates)
        stored_bytes = sum(len(update.stored_document) for update in updates)
        report["updated_rows"] += int(updated)
        report["updated_input_bytes"] += input_bytes
        report["updated_stored_bytes"] += stored_bytes
        report["saved_bytes"] += input_bytes - stored_bytes
        report["write_batches"] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
    return report


def _migrate_legacy_archives(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict[str, Any]:
    record_columns = _table_columns(connection, "storage_records")
    current_columns = _table_columns(
        connection, "storage_compaction_archives"
    )
    conversation_columns = _table_columns(
        connection, "storage_conversations"
    )
    if (
        not {"namespace", "record_key", "value_json", "version"}
        <= record_columns
        or not _CURRENT_ARCHIVE_COLUMNS <= current_columns
        or not {"id", "user_id"} <= conversation_columns
    ):
        return {"mode": "unsupported_schema", "migrated_rows": 0}
    trash_available = {
        "conversation_id", "user_id"
    } <= _table_columns(connection, "storage_conversation_trash")
    report: dict[str, Any] = {
        "mode": "owner_resolved_semantic_migration",
        "scanned_rows": 0,
        "scanned_payload_bytes": 0,
        "invalid_rows": 0,
        "oversize_rows": 0,
        "missing_owner_rows": 0,
        "ambiguous_owner_rows": 0,
        "conflicting_target_rows": 0,
        "oversize_target_rows": 0,
        "prepared_rows": 0,
        "migrated_rows": 0,
        "inserted_target_rows": 0,
        "matched_target_rows": 0,
        "retired_source_bytes": 0,
        "target_message_bytes": 0,
        "selection_pages": 0,
        "write_batches": 0,
        "max_page_payload_bytes": 0,
        "selection_row_limit": ARCHIVE_MAINTENANCE_SELECT_ROWS,
        "page_payload_budget_bytes": ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES,
        "document_budget_bytes": ARCHIVE_MAINTENANCE_DOCUMENT_BYTES,
        "trash_owner_lookup_available": trash_available,
    }
    last_record_key = ""
    while True:
        rows = connection.execute(
            "SELECT record_key,version,length(CAST(value_json AS BLOB)) "
            "FROM storage_records WHERE namespace=? AND record_key>? "
            "ORDER BY record_key LIMIT ?",
            (
                _LEGACY_NAMESPACE,
                last_record_key,
                ARCHIVE_MAINTENANCE_SELECT_ROWS,
            ),
        ).fetchall()
        if not rows:
            break
        report["selection_pages"] += 1
        selected: list[tuple[str, int, int]] = []
        page_bytes = 0
        for row in rows:
            record_key = str(row[0])
            version = int(row[1])
            source_bytes = max(0, int(row[2] or 0))
            if source_bytes > ARCHIVE_MAINTENANCE_DOCUMENT_BYTES:
                report["oversize_rows"] += 1
                last_record_key = record_key
                continue
            if (
                selected
                and page_bytes + source_bytes
                > ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES
            ):
                break
            selected.append((record_key, version, source_bytes))
            page_bytes += source_bytes
            last_record_key = record_key
            if page_bytes >= ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES:
                break
        if not selected:
            continue
        report["scanned_rows"] += len(selected)
        report["scanned_payload_bytes"] += page_bytes
        report["max_page_payload_bytes"] = max(
            report["max_page_payload_bytes"], page_bytes
        )
        migrations: list[_LegacyMigrationCandidate] = []
        for record_key, version, source_bytes in selected:
            row = connection.execute(
                "SELECT CAST(value_json AS BLOB) FROM storage_records "
                "WHERE namespace=? AND record_key=? AND version=?",
                (_LEGACY_NAMESPACE, record_key, version),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "legacy compaction archive disappeared during maintenance"
                )
            raw = _payload_bytes(row[0])
            if len(raw) != source_bytes:
                raise RuntimeError(
                    "legacy compaction archive length changed during maintenance"
                )
            try:
                archive = decode_legacy_compaction_archive(record_key, raw)
            except (
                LegacyCompactionArchiveError,
                ProjectionCodecError,
                orjson.JSONDecodeError,
                orjson.JSONEncodeError,
                TypeError,
                ValueError,
            ):
                report["invalid_rows"] += 1
                continue
            owners = _owner_ids(
                connection,
                archive.conversation_id,
                trash_available=trash_available,
            )
            if not owners:
                report["missing_owner_rows"] += 1
                continue
            if len(owners) != 1:
                report["ambiguous_owner_rows"] += 1
                continue
            user_id = owners[0]
            target_status, target = _bounded_current_archive_target(
                connection, archive.archive_id
            )
            if target_status == "oversize":
                report["oversize_target_rows"] += 1
                continue
            if (
                target_status == "loaded"
                and target is not None
                and not _current_archive_matches(target, archive, user_id)
            ):
                report["conflicting_target_rows"] += 1
                continue
            migrations.append(_LegacyMigrationCandidate(
                archive=archive,
                user_id=user_id,
                source_version=version,
                source_bytes=source_bytes,
            ))
        report["prepared_rows"] += len(migrations)
        if not migrations:
            continue

        def _write_batch(conn: sqlite3.Connection):
            return _write_legacy_migration_batch(conn, migrations)

        write_result = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose="legacy compaction archive semantic migration",
            operation=_write_batch,
        )
        report["migrated_rows"] += write_result.migrated_rows
        report["inserted_target_rows"] += write_result.inserted_target_rows
        report["matched_target_rows"] += write_result.matched_target_rows
        report["conflicting_target_rows"] += (
            write_result.conflicting_target_rows
        )
        report["oversize_target_rows"] += write_result.oversize_target_rows
        report["retired_source_bytes"] += write_result.retired_source_bytes
        report["target_message_bytes"] += write_result.target_message_bytes
        report["write_batches"] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
    report["retained_source_rows"] = int(connection.execute(
        "SELECT count(*) FROM storage_records WHERE namespace=?",
        (_LEGACY_NAMESPACE,),
    ).fetchone()[0])
    return report


def maintain_compaction_archive_storage(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict[str, Any]:
    """Run current-codec backfill then exact retired-record migration."""
    return {
        "mode": "bounded_codec_and_legacy_migration",
        "current_archive_codec": _maintain_current_archives(
            connection, db_path=db_path, lease=lease
        ),
        "legacy_archive_migration": _migrate_legacy_archives(
            connection, db_path=db_path, lease=lease
        ),
    }


__all__ = [
    "ARCHIVE_CODEC_MIN_DOCUMENT_BYTES",
    "ARCHIVE_MAINTENANCE_DOCUMENT_BYTES",
    "ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES",
    "ARCHIVE_MAINTENANCE_SELECT_ROWS",
    "LegacyCompactionArchive",
    "LegacyCompactionArchiveError",
    "decode_legacy_compaction_archive",
    "maintain_compaction_archive_storage",
]
