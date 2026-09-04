"""Offline compaction-archive maintenance is lossless, scoped, and bounded."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import orjson
import pytest

from lib.storage_sidecar import offline_compaction_archive_maintenance as archive_maintenance
from lib.storage_sidecar import offline_maintenance
from lib.storage_sidecar.archived_message_codec import (
    decode_archived_message_sequence_from_storage,
)
from lib.storage_sidecar.preflight import ProjectLease


pytestmark = pytest.mark.unit

_CURRENT_ARCHIVE_INSERT = (
    "INSERT INTO storage_compaction_archives("
    "archive_id,conversation_id,user_id,messages_json,summary,receipt_json,"
    "trigger,task_id,round_num,model,tokens_before,tokens_after,msgs_before,"
    "msgs_after,reason,payload_size,created_at_ms) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _canonical(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _messages(label: str, *, large: bool = True) -> list[dict]:
    repetitions = 12_000 if large else 4
    return [{
        "role": "assistant",
        "content": f"{label} repeated archive result " * repetitions,
    }]


def _legacy_document(
    conversation_id: str,
    archive_id: str,
    *,
    messages: list[dict] | None = None,
    **overrides,
) -> bytes:
    value = {
        "archive_id": archive_id,
        "conv_id": conversation_id,
        "created_at_ms": 1_700_000_000_123,
        "messages": messages or _messages(archive_id),
        "model": "test-model",
        "msgs_after": 2,
        "msgs_before": 3,
        "reason": "context budget",
        "round_num": 4,
        "summary": f"summary for {archive_id}",
        "task_id": f"task-{archive_id}",
        "tokens_after": 20,
        "tokens_before": 30,
        "trigger": "automatic",
    }
    value.update(overrides)
    return _canonical(value)


def _insert_legacy(
    connection: sqlite3.Connection,
    conversation_id: str,
    archive_id: str,
    *,
    document: bytes | None = None,
    version: int = 1,
) -> bytes:
    raw = document or _legacy_document(conversation_id, archive_id)
    connection.execute(
        "INSERT INTO storage_records(namespace,record_key,value_json,version,"
        "updated_at_ms) VALUES (?,?,?,?,?)",
        (
            "transcript_archive",
            f"{conversation_id}:{archive_id}",
            raw,
            version,
            1_700_000_000_456,
        ),
    )
    return raw


def _insert_current(
    connection: sqlite3.Connection,
    archive_id: str,
    conversation_id: str,
    user_id: int,
    messages: list[dict],
    *,
    summary: str = "",
) -> bytes:
    raw = _canonical(messages)
    connection.execute(
        _CURRENT_ARCHIVE_INSERT,
        (
            archive_id,
            conversation_id,
            user_id,
            raw,
            summary,
            b"{}",
            "force",
            "",
            0,
            "",
            0,
            0,
            len(messages),
            0,
            "",
            len(raw),
            1_700_000_000_000,
        ),
    )
    return raw


@pytest.fixture
def archive_authority(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "tofu.db"
    seed = sqlite3.connect(db_path)
    seed.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE storage_conversations (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL
        );
        CREATE TABLE storage_conversation_trash (
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (conversation_id, user_id)
        );
        CREATE TABLE storage_records (
            namespace TEXT NOT NULL,
            record_key TEXT NOT NULL,
            value_json BLOB NOT NULL,
            version INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (namespace, record_key)
        );
        CREATE TABLE storage_compaction_archives (
            archive_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            messages_json BLOB NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            receipt_json BLOB NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL DEFAULT 'force',
            task_id TEXT NOT NULL DEFAULT '',
            round_num INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '',
            tokens_before INTEGER NOT NULL DEFAULT 0,
            tokens_after INTEGER NOT NULL DEFAULT 0,
            msgs_before INTEGER NOT NULL DEFAULT 0,
            msgs_after INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            payload_size INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL
        );
        """
    )
    seed.commit()
    seed.close()
    lease = ProjectLease(
        data_dir,
        owner_kind="offline_maintenance",
        owner_label="Archive maintenance test",
    )
    lease.acquire()
    connection = offline_maintenance.open_sqlite_tool_connection(
        db_path, writable=True, lease=lease
    )
    try:
        yield connection, db_path, lease
    finally:
        connection.close()
        lease.release()


def _maintain(archive_authority) -> dict:
    connection, db_path, lease = archive_authority
    return archive_maintenance.maintain_compaction_archive_storage(
        connection,
        db_path=db_path,
        lease=lease,
    )


def test_current_archives_use_the_lossless_codec_idempotently(
    archive_authority,
):
    connection, _db_path, _lease = archive_authority
    messages = _messages("current")
    raw = _insert_current(
        connection, "current-archive", "current-conversation", 7, messages
    )

    first = _maintain(archive_authority)["current_archive_codec"]
    row = connection.execute(
        "SELECT messages_json,payload_size FROM storage_compaction_archives "
        "WHERE archive_id='current-archive'"
    ).fetchone()
    stored = bytes(row["messages_json"])

    assert first["updated_rows"] == 1
    assert first["updated_input_bytes"] == len(raw)
    assert first["saved_bytes"] == len(raw) - len(stored) > 0
    assert row["payload_size"] == len(stored)
    assert _canonical(decode_archived_message_sequence_from_storage(
        orjson.loads(stored)
    )) == _canonical(messages)
    second = _maintain(archive_authority)["current_archive_codec"]
    assert second["updated_rows"] == 0
    assert connection.execute(
        "SELECT messages_json FROM storage_compaction_archives "
        "WHERE archive_id='current-archive'"
    ).fetchone()[0] == stored


def test_legacy_archives_resolve_active_and_trash_owners(
    archive_authority,
):
    connection, _db_path, _lease = archive_authority
    connection.execute(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        ("active-conversation", 11),
    )
    connection.execute(
        "INSERT INTO storage_conversation_trash(conversation_id,user_id) "
        "VALUES (?,?)",
        ("trashed-conversation", 22),
    )
    expected = {
        "active-archive": _messages("active-archive"),
        "trashed-archive": _messages("trashed-archive"),
    }
    for conversation_id, archive_id in (
        ("active-conversation", "active-archive"),
        ("trashed-conversation", "trashed-archive"),
    ):
        _insert_legacy(
            connection,
            conversation_id,
            archive_id,
            document=_legacy_document(
                conversation_id,
                archive_id,
                messages=expected[archive_id],
            ),
        )

    migration = _maintain(archive_authority)["legacy_archive_migration"]

    assert migration["migrated_rows"] == 2
    assert migration["inserted_target_rows"] == 2
    assert migration["retained_source_rows"] == 0
    rows = connection.execute(
        "SELECT * FROM storage_compaction_archives ORDER BY archive_id"
    ).fetchall()
    assert {row["archive_id"]: row["user_id"] for row in rows} == {
        "active-archive": 11,
        "trashed-archive": 22,
    }
    for row in rows:
        messages = decode_archived_message_sequence_from_storage(
            orjson.loads(row["messages_json"])
        )
        assert _canonical(messages) == _canonical(expected[row["archive_id"]])
        assert orjson.loads(row["receipt_json"]) == {}
    assert _maintain(archive_authority)["legacy_archive_migration"][
        "migrated_rows"
    ] == 0


def test_matching_target_retires_only_the_redundant_legacy_record(
    archive_authority,
):
    connection, _db_path, _lease = archive_authority
    conversation_id = "matched-conversation"
    archive_id = "matched-archive"
    connection.execute(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        (conversation_id, 33),
    )
    raw = _insert_legacy(connection, conversation_id, archive_id, version=9)
    archive = archive_maintenance.decode_legacy_compaction_archive(
        f"{conversation_id}:{archive_id}", raw
    )
    connection.execute(_CURRENT_ARCHIVE_INSERT, archive.target_values(33))

    migration = _maintain(archive_authority)["legacy_archive_migration"]

    assert migration["migrated_rows"] == 1
    assert migration["inserted_target_rows"] == 0
    assert migration["matched_target_rows"] == 1
    assert migration["retired_source_bytes"] == len(raw)
    assert connection.execute(
        "SELECT count(*) FROM storage_records"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM storage_compaction_archives"
    ).fetchone()[0] == 1


def test_unowned_ambiguous_invalid_and_conflicting_rows_are_preserved(
    archive_authority,
):
    connection, _db_path, _lease = archive_authority
    connection.executemany(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        (("ambiguous-conversation", 41), ("invalid-conversation", 42),
         ("conflict-conversation", 43)),
    )
    connection.execute(
        "INSERT INTO storage_conversation_trash(conversation_id,user_id) "
        "VALUES (?,?)",
        ("ambiguous-conversation", 99),
    )
    _insert_legacy(connection, "missing-conversation", "missing-archive")
    _insert_legacy(connection, "ambiguous-conversation", "ambiguous-archive")
    invalid = orjson.loads(_legacy_document(
        "invalid-conversation", "invalid-archive"
    ))
    invalid["unexpected"] = True
    _insert_legacy(
        connection,
        "invalid-conversation",
        "invalid-archive",
        document=_canonical(invalid),
    )
    _insert_legacy(connection, "conflict-conversation", "conflict-archive")
    _insert_current(
        connection,
        "conflict-archive",
        "conflict-conversation",
        43,
        _messages("different", large=False),
        summary="different target",
    )

    migration = _maintain(archive_authority)["legacy_archive_migration"]

    assert migration["migrated_rows"] == 0
    assert migration["missing_owner_rows"] == 1
    assert migration["ambiguous_owner_rows"] == 1
    assert migration["invalid_rows"] == 1
    assert migration["conflicting_target_rows"] == 1
    assert migration["retained_source_rows"] == 4
    assert connection.execute(
        "SELECT count(*) FROM storage_records"
    ).fetchone()[0] == 4


def test_duplicate_archive_identity_isolated_inside_one_write_page(
    archive_authority,
):
    connection, _db_path, _lease = archive_authority
    connection.executemany(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        (("duplicate-a", 44), ("duplicate-b", 45)),
    )
    _insert_legacy(connection, "duplicate-a", "shared-archive")
    _insert_legacy(connection, "duplicate-b", "shared-archive")

    migration = _maintain(archive_authority)["legacy_archive_migration"]

    assert migration["migrated_rows"] == 1
    assert migration["inserted_target_rows"] == 1
    assert migration["conflicting_target_rows"] == 1
    assert migration["retained_source_rows"] == 1
    target = connection.execute(
        "SELECT conversation_id,user_id FROM storage_compaction_archives "
        "WHERE archive_id='shared-archive'"
    ).fetchone()
    assert tuple(target) == ("duplicate-a", 44)


def test_oversize_documents_are_rejected_before_body_reads(
    archive_authority,
    monkeypatch,
):
    connection, _db_path, _lease = archive_authority
    connection.execute(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        ("oversize-conversation", 51),
    )
    _insert_current(
        connection,
        "oversize-current",
        "oversize-conversation",
        51,
        _messages("oversize current", large=False),
    )
    _insert_legacy(
        connection, "oversize-conversation", "oversize-legacy"
    )
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_CODEC_MIN_DOCUMENT_BYTES", 1
    )
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_MAINTENANCE_DOCUMENT_BYTES", 64
    )
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES", 64
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        report = _maintain(archive_authority)
    finally:
        connection.set_trace_callback(None)

    assert report["current_archive_codec"]["oversize_rows"] == 1
    assert report["legacy_archive_migration"]["oversize_rows"] == 1
    normalized = [" ".join(statement.split()) for statement in statements]
    assert not any(
        statement.startswith("SELECT CAST(messages_json AS BLOB)")
        for statement in normalized
    )
    assert not any(
        statement.startswith("SELECT CAST(value_json AS BLOB)")
        for statement in normalized
    )


def test_oversize_existing_target_is_not_materialized(
    archive_authority,
    monkeypatch,
):
    connection, _db_path, _lease = archive_authority
    conversation_id = "oversize-target-conversation"
    archive_id = "oversize-target-archive"
    connection.execute(
        "INSERT INTO storage_conversations(id,user_id) VALUES (?,?)",
        (conversation_id, 52),
    )
    current_raw = _insert_current(
        connection,
        archive_id,
        conversation_id,
        52,
        _messages("large target"),
    )
    legacy_raw = _insert_legacy(
        connection,
        conversation_id,
        archive_id,
        document=_legacy_document(
            conversation_id,
            archive_id,
            messages=_messages("small legacy", large=False),
        ),
    )
    budget = len(legacy_raw) + 8
    assert len(current_raw) > budget
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_CODEC_MIN_DOCUMENT_BYTES", 1
    )
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_MAINTENANCE_DOCUMENT_BYTES", budget
    )
    monkeypatch.setattr(
        archive_maintenance,
        "ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES",
        budget,
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        report = _maintain(archive_authority)
    finally:
        connection.set_trace_callback(None)

    assert report["current_archive_codec"]["oversize_rows"] == 1
    migration = report["legacy_archive_migration"]
    assert migration["oversize_target_rows"] == 1
    assert migration["migrated_rows"] == 0
    assert migration["retained_source_rows"] == 1
    normalized = [" ".join(statement.split()) for statement in statements]
    assert not any(
        statement.startswith("SELECT * FROM storage_compaction_archives")
        for statement in normalized
    )


def test_current_archive_selection_pages_respect_the_payload_budget(
    archive_authority,
    monkeypatch,
):
    connection, _db_path, _lease = archive_authority
    messages = _messages("page", large=False)
    raw = _canonical(messages)
    for index in range(3):
        _insert_current(
            connection,
            f"page-{index}",
            "page-conversation",
            61,
            messages,
        )
    monkeypatch.setattr(
        archive_maintenance, "ARCHIVE_CODEC_MIN_DOCUMENT_BYTES", 1
    )
    monkeypatch.setattr(
        archive_maintenance,
        "ARCHIVE_MAINTENANCE_DOCUMENT_BYTES",
        len(raw) + 8,
    )
    monkeypatch.setattr(
        archive_maintenance,
        "ARCHIVE_MAINTENANCE_PAGE_PAYLOAD_BYTES",
        len(raw) + 8,
    )

    report = _maintain(archive_authority)["current_archive_codec"]

    assert report["scanned_rows"] == 3
    assert report["selection_pages"] == 3
    assert report["max_page_payload_bytes"] == len(raw)
    assert report["max_page_payload_bytes"] <= (
        report["page_payload_budget_bytes"]
    )
