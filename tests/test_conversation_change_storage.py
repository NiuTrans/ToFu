"""Physical Conversation Sync replay storage and AttemptEvent lifecycles."""

from __future__ import annotations

from collections.abc import Mapping
import sqlite3
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.operations_pkg._common import _dump, _load
from lib.storage_sidecar.schema import initialize_schema


pytestmark = pytest.mark.unit


@pytest.fixture()
def session(tmp_path):
    connection = sqlite3.connect(tmp_path / "conversation-changes.db")
    connection.row_factory = sqlite3.Row
    authority = SQLiteSession(connection)
    initialize_schema(authority)
    authority.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("conversation", 1, "sync", _dump([]), 1, 1, _dump({}), 0, "", 1),
    )
    try:
        yield authority
    finally:
        connection.close()


def _attempt_event(
    *, turn_id: str = "turn", attempt_id: str = "attempt", sequence: int = 1,
) -> dict:
    return {
        "conversationId": "conversation",
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": sequence,
        "projectionRevision": sequence,
        "type": "projection_updated",
        "payload": {
            "projectionPatch": {
                "version": 1,
                "baseRevision": sequence - 1,
                "targetRevision": sequence,
                "operations": [],
            },
        },
    }


def _seed_attempt(
    session,
    *,
    turn_id: str = "turn",
    attempt_id: str = "attempt",
    status: str = "completed",
    current: bool = True,
    superseded_at: int | None = None,
) -> dict:
    from lib.storage_sidecar.operations_pkg._turns_events import (
        _insert_attempt_event,
    )

    ordinal = int(session.fetch_one(
        "SELECT COALESCE(MAX(ordinal),-1)+1 AS ordinal "
        "FROM storage_conversation_turns WHERE conversation_id='conversation'"
    )["ordinal"])
    session.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,ordinal,actor,status,current_attempt_id,"
        "projection_json,projection_revision,settlement_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            turn_id, "conversation", 1, ordinal, "assistant", "completed",
            attempt_id if current else None, _dump({"content": "answer"}),
            1, _dump({}), 1, 1,
        ),
    )
    session.execute(
        "INSERT INTO storage_generation_attempts("
        "attempt_id,conversation_id,turn_id,command_id,operation,status,"
        "base_projection_revision,created_at,settled_at,superseded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id, "conversation", turn_id, f"command-{attempt_id}",
            "reply", status, 0, 1, 1, superseded_at,
        ),
    )
    event = _attempt_event(turn_id=turn_id, attempt_id=attempt_id)
    _insert_attempt_event(
        session,
        attempt_id=attempt_id,
        sequence=1,
        conversation_id="conversation",
        turn_id=turn_id,
        projection_revision=1,
        event_type="projection_updated",
        envelope=event,
        created_at=1,
    )
    return event


def _capture_attempt_change(session, event: Mapping) -> dict:
    from lib.storage_sidecar.operations_pkg._turns_core import (
        _turn_change_capture,
    )

    return _turn_change_capture(
        session,
        "turn.event.record",
        {},
        {"applied": True, "_conversationSyncAttemptEvents": [dict(event)]},
    )


def test_attempt_change_stores_reference_and_round_trips_exact_public_event(
    session, monkeypatch,
):
    from lib.storage_sidecar.operations_pkg import _turns_core as core
    from lib.storage_sidecar.operations_pkg._turns_core import (
        _append_conversation_change,
    )
    from lib.storage_sidecar.operations_pkg._turns_read import _turn_sync_changes

    attempt_event = _seed_attempt(session)
    real_dump = core._dump

    def reject_duplicate_envelope(value):
        if (
            isinstance(value, Mapping)
            and value.get("type") == "attempt.event"
            and "syncSeq" in value
        ):
            raise AssertionError("AttemptEvent sync envelope was encoded twice")
        return real_dump(value)

    monkeypatch.setattr(core, "_dump", reject_duplicate_envelope)
    captured = _capture_attempt_change(session, attempt_event)
    committed_event = captured["events"][0]["event"]
    physical = session.fetch_one(
        "SELECT attempt_sequence,event_json FROM storage_conversation_changes "
        "WHERE sync_sequence=1"
    )

    assert physical["attempt_sequence"] == 1
    assert _load(physical["event_json"]) == {}
    page = _turn_sync_changes(session, {
        "conversation_id": "conversation", "user_id": 1, "after": 0,
    })
    assert page["events"] == [committed_event]

    mismatched_event = {**attempt_event, "turnId": "different-turn"}
    session.execute(
        "UPDATE storage_attempt_events SET payload_json=? "
        "WHERE attempt_id='attempt' AND sequence=1",
        (_dump(mismatched_event),),
    )
    with pytest.raises(StorageError, match="reference is unresolved") as mismatch:
        _turn_sync_changes(session, {
            "conversation_id": "conversation", "user_id": 1, "after": 0,
        })
    assert mismatch.value.code == "database_integrity"
    session.execute(
        "UPDATE storage_attempt_events SET payload_json=? "
        "WHERE attempt_id='attempt' AND sequence=1",
        (_dump(attempt_event),),
    )

    legacy = _append_conversation_change(
        session,
        conversation_id="conversation",
        user_id=1,
        change_type="conversation.activity",
        payload={"requiresSnapshot": True},
    )
    legacy_row = session.fetch_one(
        "SELECT attempt_sequence,event_json FROM storage_conversation_changes "
        "WHERE sync_sequence=2"
    )
    assert legacy_row["attempt_sequence"] is None
    assert _load(legacy_row["event_json"]) == legacy
    assert _turn_sync_changes(session, {
        "conversation_id": "conversation", "user_id": 1, "after": 1,
    })["events"] == [legacy]

    session.execute(
        "DELETE FROM storage_attempt_events WHERE attempt_id='attempt'")
    with pytest.raises(StorageError, match="reference is unresolved") as raised:
        _turn_sync_changes(session, {
            "conversation_id": "conversation", "user_id": 1, "after": 0,
        })
    assert raised.value.code == "database_integrity"


def test_replay_reference_protects_ttl_and_superseded_attempt_cleanup(session):
    from lib.storage_sidecar.operations_pkg._turns_lifecycle import _turn_cleanup
    from lib.storage_sidecar.operations_pkg._turns_read import (
        _turn_events_prune,
        _turn_sync_prune,
    )

    event = _seed_attempt(session)
    _capture_attempt_change(session, event)
    protected = _turn_events_prune(session, {
        "settled_before_ms": 2, "max_attempts": 8, "max_rows": 64,
    })
    assert protected["deleted_rows"] == 0

    future = int(time.time() * 1000) + 10_000
    assert _turn_sync_prune(session, {
        "created_before_ms": future, "max_rows": 64,
    })["deletedRows"] == 1
    released = _turn_events_prune(session, {
        "settled_before_ms": 2, "max_attempts": 8, "max_rows": 64,
    })
    assert released["deleted_rows"] == 1

    old_event = _seed_attempt(
        session,
        turn_id="old-turn",
        attempt_id="old-attempt",
        status="superseded",
        current=False,
        superseded_at=1,
    )
    _capture_attempt_change(session, old_event)
    assert _turn_cleanup(session, {"retention_ms": 0, "limit": 8}) == 0
    assert session.fetch_one(
        "SELECT 1 FROM storage_generation_attempts "
        "WHERE attempt_id='old-attempt'"
    ) is not None
    _turn_sync_prune(session, {"created_before_ms": future, "max_rows": 64})
    assert _turn_cleanup(session, {"retention_ms": 0, "limit": 8}) == 1


def test_offline_retention_uses_the_same_sync_reference_fence(session):
    from lib.storage_sidecar.offline_maintenance import (
        sqlite_transport_retention_candidate_queries,
    )

    event = _seed_attempt(session)
    _capture_attempt_change(session, event)
    query = sqlite_transport_retention_candidate_queries(
        attempt_cutoff_ms=2,
        now_ms=2,
        aggregate=True,
    )["storage_attempt_events"]

    protected = session.connection.execute(
        str(query["sql"]), tuple(query["params"])
    ).fetchone()
    assert protected[0] == 0
    assert protected[1] == 0

    session.execute("DELETE FROM storage_conversation_changes")
    released = session.connection.execute(
        str(query["sql"]), tuple(query["params"])
    ).fetchone()
    assert released[0] == 1
    assert released[1] > 0


def test_pre_schema_51_offline_retention_does_not_require_reference_column():
    from lib.storage_sidecar.offline_maintenance import (
        sqlite_conversation_change_references_available,
        sqlite_transport_retention_candidate_queries,
    )

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE storage_generation_attempts ("
        "attempt_id TEXT PRIMARY KEY,status TEXT NOT NULL,settled_at BIGINT)"
    )
    connection.execute(
        "CREATE TABLE storage_attempt_events ("
        "attempt_id TEXT NOT NULL,sequence BIGINT NOT NULL,payload_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE storage_conversation_changes (attempt_id TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO storage_generation_attempts VALUES ('old','completed',1)"
    )
    connection.execute(
        "INSERT INTO storage_attempt_events VALUES ('old',1,'{}')"
    )

    references_available = (
        sqlite_conversation_change_references_available(connection)
    )
    query = sqlite_transport_retention_candidate_queries(
        attempt_cutoff_ms=2,
        now_ms=2,
        aggregate=True,
        protect_conversation_change_references=references_available,
    )["storage_attempt_events"]
    row = connection.execute(
        str(query["sql"]), tuple(query["params"])
    ).fetchone()
    connection.close()

    assert references_available is False
    assert row == (1, 2)
    assert query["required_tables"] == (
        "storage_attempt_events",
        "storage_generation_attempts",
    )


def test_sync_prune_batches_composite_keys_below_sqlite_parameter_limit(
    session, monkeypatch,
):
    from lib.storage_sidecar.operations_pkg._turns_read import _turn_sync_prune

    session.connection.executemany(
        "INSERT INTO storage_conversation_changes("
        "conversation_id,user_id,sync_sequence,change_type,event_json,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            ("conversation", 1, sequence, "conversation.activity", _dump({}), 1)
            for sequence in range(1, 601)
        ),
    )
    delete_statements: list[tuple[str, int]] = []
    real_execute = session.execute

    def tracked_execute(sql, params=()):
        if sql.startswith("DELETE FROM storage_conversation_changes"):
            delete_statements.append((sql, len(params)))
        return real_execute(sql, params)

    monkeypatch.setattr(session, "execute", tracked_execute)
    result = _turn_sync_prune(session, {
        "created_before_ms": 2,
        "max_rows": 600,
    })

    assert result == {"deletedRows": 600, "remaining": False}
    assert [parameter_count for _, parameter_count in delete_statements] == [
        768, 768, 264,
    ]


def test_turn_delete_expires_reference_prefix_before_attempt_events(session):
    from lib.storage_sidecar.operations_pkg._turns_core import (
        _append_conversation_change,
        _turn_change_capture,
    )
    from lib.storage_sidecar.operations_pkg._turns_lifecycle import _turn_delete
    from lib.storage_sidecar.operations_pkg._turns_read import _turn_sync_changes

    event = _seed_attempt(session)
    _append_conversation_change(
        session,
        conversation_id="conversation",
        user_id=1,
        change_type="turn.upsert",
        payload={"turns": []},
        turn_id="turn",
    )
    _capture_attempt_change(session, event)
    _append_conversation_change(
        session,
        conversation_id="conversation",
        user_id=1,
        change_type="conversation.activity",
        payload={"requiresSnapshot": True},
    )

    deleted = _turn_delete(session, {
        "conversation_id": "conversation", "user_id": 1, "turn_ids": ["turn"],
    })
    _turn_change_capture(
        session,
        "turn.delete",
        {"conversation_id": "conversation", "user_id": 1},
        deleted,
    )

    sequences = [
        int(row["sync_sequence"])
        for row in session.fetch_all(
            "SELECT sync_sequence FROM storage_conversation_changes "
            "ORDER BY sync_sequence"
        )
    ]
    assert sequences == [3, 4]
    assert session.fetch_one(
        "SELECT 1 FROM storage_attempt_events WHERE attempt_id='attempt'"
    ) is None
    expired = _turn_sync_changes(session, {
        "conversation_id": "conversation", "user_id": 1, "after": 0,
    })
    assert expired["resetRequired"] is True
    assert expired["resetReason"] == "cursor_expired"
    tail = _turn_sync_changes(session, {
        "conversation_id": "conversation", "user_id": 1, "after": 3,
    })
    assert [change["type"] for change in tail["events"]] == ["turn.deleted"]
