"""Offline task-result codec backfill is lossless, scoped, and bounded."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import orjson
import pytest

from lib.storage_sidecar import offline_maintenance
from lib.storage_sidecar import offline_task_result_maintenance as maintenance
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.task_result_field_codec import (
    TASK_RESULT_FIELD_CODEC_KEY,
    decode_task_result_fields_from_storage,
)


pytestmark = pytest.mark.unit


def _canonical(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _large_result(label: str = "result") -> dict:
    return {
        "task_id": f"task-{label}",
        "conv_id": f"conversation-{label}",
        "user_id": 17,
        "status": "done",
        "segments": f"{label} segment payload " * 20_000,
        "metadata": orjson.dumps({
            "finishReason": "stop",
            "toolSummary": f"{label} tool summary " * 10_000,
        }).decode(),
        "tool_rounds": None,
        "content": "answer",
        "thinking": "",
        "error": None,
        "created_at": 10,
        "completed_at": 20,
    }


@pytest.fixture
def task_result_authority(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "tofu.db"
    seed = sqlite3.connect(db_path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute(
        "CREATE TABLE storage_records("
        "namespace TEXT NOT NULL,record_key TEXT NOT NULL,"
        "value_json BLOB NOT NULL,version INTEGER NOT NULL,"
        "updated_at_ms INTEGER NOT NULL,PRIMARY KEY(namespace,record_key))"
    )
    seed.commit()
    seed.close()
    lease = ProjectLease(
        data_dir,
        owner_kind="offline_maintenance",
        owner_label="Task result maintenance test",
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


def _maintain(task_result_authority) -> dict:
    connection, db_path, lease = task_result_authority
    return maintenance.maintain_task_result_storage(
        connection, db_path=db_path, lease=lease
    )


def _insert(
    connection: sqlite3.Connection,
    namespace: str,
    key: str,
    value: object,
    *,
    version: int = 7,
    updated_at_ms: int = 123,
) -> bytes:
    raw = _canonical(value)
    connection.execute(
        "INSERT INTO storage_records VALUES (?,?,?,?,?)",
        (namespace, key, raw, version, updated_at_ms),
    )
    return raw


def test_backfill_is_lossless_version_neutral_and_idempotent(
    task_result_authority,
):
    connection, _db_path, _lease = task_result_authority
    value = _large_result()
    raw = _insert(connection, "task_results", value["task_id"], value)
    knowledge = {"body": "knowledge payload " * 20_000}
    knowledge_raw = _insert(connection, "knowledge", "document", knowledge)

    first = _maintain(task_result_authority)
    row = connection.execute(
        "SELECT value_json,version,updated_at_ms FROM storage_records "
        "WHERE namespace='task_results'"
    ).fetchone()
    stored_document = bytes(row[0])
    stored_value = orjson.loads(stored_document)

    assert first["updated_rows"] == 1
    assert first["saved_bytes"] == len(raw) - len(stored_document) > 0
    assert first["compressed_field_rows"]["segments"] == 1
    assert first["compressed_field_rows"]["metadata"] == 1
    assert TASK_RESULT_FIELD_CODEC_KEY in stored_value["segments"]
    assert decode_task_result_fields_from_storage(stored_value) == value
    assert tuple(row[1:]) == (7, 123)
    knowledge_row = connection.execute(
        "SELECT value_json,version,updated_at_ms FROM storage_records "
        "WHERE namespace='knowledge'"
    ).fetchone()
    assert tuple(knowledge_row) == (knowledge_raw, 7, 123)

    second = _maintain(task_result_authority)
    assert second["updated_rows"] == 0
    assert connection.execute(
        "SELECT value_json FROM storage_records WHERE namespace='task_results'"
    ).fetchone()[0] == stored_document


def test_malformed_private_field_stays_byte_identical(
    task_result_authority,
):
    connection, _db_path, _lease = task_result_authority
    value = _large_result("invalid")
    value["segments"] = {
        TASK_RESULT_FIELD_CODEC_KEY: {
            "version": 1,
            "encoding": "zlib-base64",
            "decodedBytes": 40_000,
            "payload": "not base64!",
        }
    }
    raw = _insert(connection, "task_results", value["task_id"], value)

    report = _maintain(task_result_authority)

    assert report["invalid_rows"] == 1
    assert report["updated_rows"] == 0
    assert connection.execute(
        "SELECT value_json FROM storage_records WHERE namespace='task_results'"
    ).fetchone()[0] == raw


def test_oversize_document_is_rejected_before_body_read(
    task_result_authority,
    monkeypatch,
):
    connection, _db_path, _lease = task_result_authority
    value = _large_result("oversize")
    _insert(connection, "task_results", value["task_id"], value)
    monkeypatch.setattr(
        maintenance, "TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES", 1
    )
    monkeypatch.setattr(
        maintenance, "TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES", 64
    )
    monkeypatch.setattr(
        maintenance, "TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES", 64
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        report = _maintain(task_result_authority)
    finally:
        connection.set_trace_callback(None)

    assert report["oversize_rows"] == 1
    normalized = [" ".join(statement.split()) for statement in statements]
    assert not any(
        statement.startswith("SELECT CAST(value_json AS BLOB)")
        for statement in normalized
    )


def test_selection_pages_respect_source_payload_budget(
    task_result_authority,
    monkeypatch,
):
    connection, _db_path, _lease = task_result_authority
    value = {"task_id": "small", "segments": "small segment"}
    raw = _canonical(value)
    for index in range(3):
        _insert(
            connection,
            "task_results",
            f"small-{index}",
            {**value, "task_id": f"small-{index}"},
        )
    source_bytes = len(_canonical({**value, "task_id": "small-0"}))
    assert source_bytes >= len(raw)
    monkeypatch.setattr(
        maintenance, "TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES", 1
    )
    monkeypatch.setattr(
        maintenance,
        "TASK_RESULT_MAINTENANCE_DOCUMENT_BYTES",
        source_bytes + 8,
    )
    monkeypatch.setattr(
        maintenance,
        "TASK_RESULT_MAINTENANCE_PAGE_PAYLOAD_BYTES",
        source_bytes + 8,
    )

    report = _maintain(task_result_authority)

    assert report["scanned_rows"] == 3
    assert report["selection_pages"] == 3
    assert report["max_page_payload_bytes"] <= (
        report["page_payload_budget_bytes"]
    )
