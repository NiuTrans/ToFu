"""Legacy upgrade and compact-witness lifetime on a disposable SQL authority."""

import json
import sqlite3
import uuid
import pytest
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import initialize_schema, deferred_index_statements
from lib.storage_sidecar.operations_pkg import _records as records

pytestmark = pytest.mark.unit


@pytest.fixture
def session():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    current = SQLiteSession(connection, event_read_epoch=uuid.uuid4().hex)
    initialize_schema(current)
    for sql in deferred_index_statements("sqlite"):
        current.execute(sql)
    try:
        yield current
    finally:
        connection.close()


def test_schema_upgrade_does_not_backfill_or_rewrite_legacy_events(session):
    body = json.dumps({"type": "round_end", "v": 1, "value": "old"}).encode()
    session.execute(
        "INSERT INTO storage_events(task_id,sequence,event_type,event_json,created_at_ms) "
        "VALUES (?,?,?,?,?)",
        ("legacy", 0, "round_end", body, 1000),
    )
    session.execute("DROP TABLE storage_event_read_guards")
    session.execute(
        "UPDATE storage_meta SET meta_value=? WHERE meta_key=?",
        ("62", "schema_version"),
    )
    initialize_schema(session)
    assert (
        session.fetch_one("SELECT COUNT(*) AS n FROM storage_event_read_guards")["n"]
        == 0
    )
    assert (
        bytes(session.fetch_one("SELECT event_json FROM storage_events")["event_json"])
        == body
    )
    before = records._event_bounds(
        session, {"task_id": "legacy", "include_read_guard": True}
    )
    assert (
        records._event_bounds(
            session, {"task_id": "legacy", "include_read_guard": True}
        )
        == before
    )
    records._event_append(
        session,
        {
            "task_id": "legacy",
            "sequence": 1,
            "event": {"type": "round_end", "value": "new"},
        },
    )
    after = records._event_bounds(
        session, {"task_id": "legacy", "include_read_guard": True}
    )
    assert after["read_guard"] != before["read_guard"]


def test_witnesses_scale_by_task_and_empty_source_keeps_its_fence(session):
    events = [
        {
            "task_id": f"task-{task}",
            "sequence": seq,
            "event": {"type": "round_end", "value": "x" * 1000},
        }
        for task in range(10)
        for seq in range(10)
    ]
    records._event_append_batch(session, {"events": events})
    rows = session.fetch_all("SELECT * FROM storage_event_read_guards")
    assert len(rows) == 10 and all(row["append_version"] == 10 for row in rows)
    assert all(
        set(row.keys()) == {"task_id", "source_id", "append_version", "prune_version"}
        for row in rows
    )
    before = records._event_bounds(
        session, {"task_id": "task-0", "include_read_guard": True}
    )
    records._event_prune(
        session,
        {
            "created_before_ms": 4_000_000_000_000,
            "limit": 1000,
            "retention_class": "structural",
        },
    )
    assert session.fetch_one("SELECT COUNT(*) AS n FROM storage_events")["n"] == 0
    assert (
        session.fetch_one("SELECT COUNT(*) AS n FROM storage_event_read_guards")["n"]
        == 10
    )
    after = records._event_bounds(
        session, {"task_id": "task-0", "include_read_guard": True}
    )
    assert after["read_guard"] != before["read_guard"]
