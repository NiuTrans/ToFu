"""Conversation-sync v3 is one generated, user-scoped authority.

These tests pin the clean-checkout generation chain and the sole HTTP boundary
used by the frontend turn runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]
pytest_plugins = ("tests._chat_sidecar",)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def conversation_sync_db(chat_sidecar, monkeypatch):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as sync_routes

    client = get_storage_client(write=True)
    fixture_id = uuid.uuid4().hex
    monkeypatch.setattr(
        sync_routes,
        "_service",
        ConversationSyncService(SidecarConversationSyncRepository(
            client_factory=lambda write=False: client,
        )),
    )
    for conversation_id, title in (
        ("conv-sync-a", "A"), ("conv-sync-b", "B")
    ):
        client.command(
            "conversation.create",
            {
                "conv_id": conversation_id,
                "user_id": 1,
                "title": title,
                "created_at": 1,
                "updated_at": 1,
                "settings": {},
            },
            f"create-sync-fixture-{fixture_id}-{conversation_id}",
        )
    try:
        yield client
    finally:
        for conversation_id in ("conv-sync-a", "conv-sync-b"):
            client.command(
                "conversation.purge",
                {"conv_id": conversation_id, "user_id": 1},
                f"cleanup-{conversation_id}-{fixture_id}",
            )


def test_generated_conversation_sync_contract_is_current_and_shippable():
    generator = ROOT / "scripts/gen_conversation_sync_contract.py"
    assert generator.is_file()
    assert (ROOT / "contracts/conversation_sync_v3.yaml").is_file()
    assert (ROOT / "lib/conversation_sync/generated_contract.py").is_file()
    assert (ROOT / "frontend/src/api/conversation-sync.generated.ts").is_file()

    result = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_file_change_routes_validate_and_bind_stable_turn_commands(
    flask_client, monkeypatch,
):
    from lib.conversation_sync.command_service import CommandOutcome
    from routes import conversation_sync_v3 as sync_routes

    calls = []

    def mutate(conversation_id, turn_id, user_id, body, *, operation):
        calls.append((conversation_id, turn_id, user_id, dict(body), operation))
        return CommandOutcome({"conversationRevision": 9})

    monkeypatch.setattr(
        sync_routes.conversation_turn_commands,
        "mutate_turn_file_changes",
        mutate,
    )
    payload = {"commandId": "files-1", "expectedProjectionRevision": 4}
    for operation in ("undo", "redo"):
        response = flask_client.post(
            f"/api/v3/conversations/conv-a/turns/turn-a/file-changes/{operation}",
            json=payload,
        )
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "conversationRevision": 9}

    assert [call[-1] for call in calls] == ["undo", "redo"]
    assert all(call[:3] == ("conv-a", "turn-a", 1) for call in calls)
    assert all(call[3] == payload for call in calls)

    rejected = flask_client.post(
        "/api/v3/conversations/conv-a/turns/turn-a/file-changes/undo",
        json={"expectedProjectionRevision": 4},
    )
    assert rejected.status_code == 400
    assert len(calls) == 2


def test_perception_route_accepts_only_content_free_bounded_receipts(
    flask_client, monkeypatch,
):
    from lib.conversation_sync.command_service import CommandOutcome
    from routes import conversation_sync_v3 as sync_routes

    calls = []

    def record_perception(conversation_id, turn_id, user_id, body):
        calls.append((conversation_id, turn_id, user_id, dict(body)))
        return CommandOutcome({"conversationRevision": 12})

    monkeypatch.setattr(
        sync_routes.conversation_turn_commands,
        "record_perception",
        record_perception,
    )
    payload = {
        "observationId": "page-a:paint:1",
        "attemptId": "attempt-a",
        "kind": "phase_painted",
        "clientId": "page-a",
        "phase": "waiting_model",
        "serverEmittedAt": 1000,
        "receivedAt": 1125,
        "paintedAt": 1160,
        "visibility": "visible",
    }
    response = flask_client.post(
        "/api/v3/conversations/conv-a/turns/turn-a/perception",
        json=payload,
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "conversationRevision": 12}
    assert calls == [("conv-a", "turn-a", 1, payload)]

    content_bearing = flask_client.post(
        "/api/v3/conversations/conv-a/turns/turn-a/perception",
        json={**payload, "observationId": "page-a:paint:2",
              "content": "must never be captured"},
    )
    assert content_bearing.status_code == 400
    assert len(calls) == 1


def test_generator_rejects_duplicate_keys_and_derives_client_paths():
    import yaml

    from scripts.gen_conversation_sync_contract import (
        _UniqueKeyLoader,
        _load_contract,
        render_typescript,
    )

    with pytest.raises(ValueError, match="duplicate contract key 'state'"):
        yaml.load(
            "schema:\n  state: live\n  state: offline\n",
            Loader=_UniqueKeyLoader,
        )

    document = _load_contract()
    document["x-tofu-command-retry"]["maxAttempts"] = 1
    with pytest.raises(ValueError, match="maxAttempts"):
        render_typescript(document)
    document["x-tofu-command-retry"]["maxAttempts"] = 4
    old_path = "/api/v3/conversations/{conversationId}/sync"
    new_path = "/api/v3/conversations/{conversationId}/authoritative-sync"
    document["paths"][new_path] = document["paths"].pop(old_path)
    generated = render_typescript(document)
    assert (
        "/authoritative-sync?segmentPayload=refs&turnWindow=tail-96&artifactHint=has-any`"
        in generated
    )
    assert "/sync`" not in generated
    assert '"maxAttempts": 4' in generated
    assert "attempt < policy.maxAttempts" in generated
    assert "streamClientId?: string" in generated
    assert "streamGeneration?: number" in generated
    assert "streamClientId=${encodeURIComponent(streamClientId)}" in generated
    assert "streamGeneration=${encodeURIComponent(String(streamGeneration))}" in generated
    assert "snapshot(conversationId: string, options?: RequestOptions)" in generated
    assert (
        "turnPage(conversationId: string, laneId: string, syncSeq: number, "
        "beforeOrdinal?: number, limit?: number, options?: RequestOptions)"
        in generated
    )
    assert "`laneId=${encodeURIComponent(laneId)}`" in generated

    document["paths"][new_path]["get"]["x-tofu-client-fixed-query"] = {
        "undeclared": "refs",
    }
    with pytest.raises(ValueError, match="fixes undeclared query"):
        render_typescript(document)


def test_turn_projection_and_settlement_have_named_generated_contracts():
    from scripts.gen_conversation_sync_contract import _load_contract, render_typescript

    document = _load_contract()
    schemas = document['components']['schemas']
    turn = schemas['TurnRecord']['properties']
    assert turn['projection']['$ref'].endswith('/TurnProjection')
    assert turn['settlement']['$ref'].endswith('/TurnSettlement')
    assert schemas['TurnProjection']['properties']['segments']['items'][
        '$ref'
    ].endswith('/TurnContentSegment')
    assert schemas['TurnProjection']['properties']['lastRoundUsage'][
        '$ref'
    ].endswith('/TurnLastRoundUsage')
    assert schemas['TurnLastRoundUsage']['additionalProperties'] is False
    assert schemas['TurnContentSegment']['oneOf']
    assert schemas['TurnSettlement']['additionalProperties'] is False
    settlement = schemas['TurnSettlement']['properties']
    assert settlement['outcome']['$ref'].endswith('/TurnOutcome')
    assert settlement['evidence']['$ref'].endswith(
        '/TurnTerminationEvidence')
    assert settlement['streamState']['oneOf'][0]['$ref'].endswith(
        '/ProviderStreamState')

    generated = render_typescript(document)
    assert 'export type TurnProjection = {' in generated
    assert 'export type TurnLastRoundUsage = {' in generated
    assert 'export type TurnContentSegment = (TurnTextSegment)' in generated
    assert 'export type TurnSettlement = {' in generated
    assert 'export type ProviderStreamState = ' in generated
    assert 'export type TurnTerminationEvidence = ' in generated


def test_sidecar_commit_atomically_appends_compact_owner_scoped_replay(
    conversation_sync_db, monkeypatch,
):
    """A durable turn write and its replay row are one sidecar transaction."""
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import (
        ConversationSyncNotFound,
        ConversationSyncService,
    )
    from lib.storage import StorageError
    from lib.storage.commit_events import subscribe_committed_events
    from lib.turn_lifecycle import (
        bind_task,
        claim_attempt_start,
        create_attempt,
        create_turn_pair,
        get_turn,
        mark_task_started,
        record_task_event,
        update_turn_projection,
    )

    client = conversation_sync_db
    monkeypatch.setenv("TOFU_TURN_DELTA_RECORD_MS", "0")
    notices: list[dict] = []
    unsubscribe = subscribe_committed_events(
        lambda batch: notices.extend(dict(item) for item in batch)
    )
    try:
        created = create_turn_pair(
            "conv-sync-a",
            command_id="atomic-create",
            input_projection={"content": "q" * 100_000},
            config={"model": "gpt-4o"},
            user_id=1,
        )
        attempt_id = created["attempt"]["attemptId"]
        assert "_storageCommitContract" not in created
        assert client.command(
            "turn.attempt.claim",
            {"attempt_id": attempt_id, "user_id": 2},
            "wrong-owner-attempt-claim",
        ) is False
        assert client.command(
            "turn.attempt.bind",
            {
                "attempt_id": attempt_id,
                "task_id": "wrong-owner-task",
                "user_id": 2,
            },
            "wrong-owner-attempt-bind",
        ) is None
        assert client.command(
            "turn.event.record",
            {"attempt_id": attempt_id, "user_id": 2},
            "wrong-owner-attempt-event",
        ) == {"applied": False}
        with pytest.raises(StorageError, match="Invalid user_id"):
            client.command(
                "turn.attempt.claim",
                {"attempt_id": attempt_id},
                "missing-owner-attempt-claim",
            )

        changes = client.query(
            "turn.sync.changes",
            {"conversation_id": "conv-sync-a", "user_id": 1, "after": 0},
        )
        assert [event["syncSeq"] for event in changes["events"]] == [1]
        assert changes["events"][0]["type"] == "turn.upsert"
        assert any(
            notice["userId"] == 1
            and notice["event"]["syncSeq"] == 1
            for notice in notices
        )

        task = {
            "_attemptId": attempt_id,
            "_userId": 1,
            "id": "task-atomic",
            "status": "running",
            "content": "x" * 100_000,
            "thinking": "",
            "toolRounds": [],
            "segments": [],
            "config": {"model": "gpt-4o"},
        }
        assert claim_attempt_start(attempt_id, user_id=1) is True
        assert bind_task(attempt_id, "task-atomic", user_id=1)["status"] == "pending"
        assert mark_task_started(
            attempt_id, "task-atomic", user_id=1,
        )["status"] == "running"
        assert record_task_event(task, {"type": "delta"}) is True
        task["content"] += "y"
        assert record_task_event(task, {"type": "delta"}) is True

        tail = client.query(
            "turn.sync.changes",
            {"conversation_id": "conv-sync-a", "user_id": 1, "after": 4},
        )
        assert [event["syncSeq"] for event in tail["events"]] == [5]
        compact_event = tail["events"][0]
        attempt_payload = compact_event["payload"]["event"]["payload"]
        assert "projection" not in attempt_payload
        assert attempt_payload["projectionPatch"]["baseRevision"] == 4
        assert attempt_payload["projectionPatch"]["targetRevision"] == 5
        assert len(json.dumps(compact_event)) < 5_000

        # Low-frequency settled mutations must obey the same payload rule.
        # Otherwise editing a single byte on a historic, tool-heavy turn would
        # reintroduce the exact retained multi-MiB replay amplification that
        # the conversation protocol exists to eliminate.
        task["status"] = "completed"
        assert record_task_event(task, {"type": "done", "finishReason": "stop"})
        settled = get_turn("conv-sync-a", created["turn"]["turnId"], user_id=1)
        before_update_head = client.query(
            "turn.sync.snapshot",
            {"conversation_id": "conv-sync-a", "user_id": 1},
        )["syncSequence"]
        edited_projection = dict(settled["projection"])
        edited_projection["content"] += "z"
        update_turn_projection(
            "conv-sync-a",
            settled["turnId"],
            projection=edited_projection,
            expected_projection_revision=settled["projectionRevision"],
            user_id=1,
        )
        edit_replay = client.query(
            "turn.sync.changes",
            {
                "conversation_id": "conv-sync-a",
                "user_id": 1,
                "after": before_update_head,
            },
        )["events"]
        assert len(edit_replay) == 1
        edit_event = edit_replay[0]
        assert edit_event["type"] == "turn.patch"
        assert "turns" not in edit_event["payload"]
        assert edit_event["payload"]["turnPatches"][0][
            "projectionPatch"
        ]["operations"] == [
            {"op": "append_text", "path": ["content"], "value": "z"},
            {
                "op": "append_text",
                "path": ["segments", 0, "text"],
                "value": "z",
            },
        ]
        assert len(json.dumps(edit_event)) < 5_000

        assert client.query(
            "turn.attempt.get", {"attempt_id": attempt_id, "user_id": 2}
        ) is None
        assert client.query(
            "turn.events.list",
            {"attempt_id": attempt_id, "user_id": 2, "projection_mode": "patch"},
        ) is None

        service = ConversationSyncService(SidecarConversationSyncRepository(
            client_factory=lambda write=False: client,
        ))
        snapshot = service.snapshot("conv-sync-a", 1)
        expected_head = before_update_head + 1
        assert snapshot["syncSeq"] == expected_head
        assert snapshot["settings"] == {}
        with pytest.raises(ConversationSyncNotFound):
            service.snapshot("conv-sync-a", 2)

        # Starting a new attempt on a giant settled turn must not retain that
        # historic projection again.  The first status event carries the
        # revision patch and compact runtime/attempt state needed by peers.
        before_attempt_head = snapshot["syncSeq"]
        latest = get_turn("conv-sync-a", settled["turnId"], user_id=1)
        submitted = get_turn(
            "conv-sync-a", created["submittedTurn"]["turnId"], user_id=1
        )
        next_submitted_projection = dict(submitted["projection"])
        next_submitted_projection["content"] += "!"
        regenerated = create_attempt(
            "conv-sync-a",
            settled["turnId"],
            command_id="compact-attempt-create",
            operation="regenerate",
            expected_projection_revision=latest["projectionRevision"],
            config={"model": "gpt-4o"},
            input_update=next_submitted_projection,
            expected_input_projection_revision=submitted["projectionRevision"],
            user_id=1,
        )
        attempt_replay = client.query(
            "turn.sync.changes",
            {
                "conversation_id": "conv-sync-a",
                "user_id": 1,
                "after": before_attempt_head,
            },
        )["events"]
        assert [item["type"] for item in attempt_replay] == [
            "turn.patch",
            "attempt.event",
        ]
        submitted_change = attempt_replay[0]["payload"]["turnPatches"][0]
        assert submitted_change["turnId"] == submitted["turnId"]
        assert submitted_change["projectionPatch"]["operations"] == [
            {"op": "append_text", "path": ["content"], "value": "!"}
        ]
        assert len(json.dumps(attempt_replay[0])) < 5_000
        initial_event = attempt_replay[1]["payload"]["event"]
        initial_payload = initial_event["payload"]
        assert "projection" not in initial_payload
        assert "turns" not in initial_payload
        assert "submittedTurn" not in initial_payload
        assert initial_payload["projectionPatch"]["baseRevision"] == (
            latest["projectionRevision"]
        )
        assert initial_payload["projectionPatch"]["targetRevision"] == (
            latest["projectionRevision"] + 1
        )
        assert initial_payload["turnState"]["currentAttemptId"] == (
            regenerated["attempt"]["attemptId"]
        )
        assert len(json.dumps(attempt_replay[1])) < 5_000

        replayed = create_attempt(
            "conv-sync-a",
            settled["turnId"],
            command_id="compact-attempt-create",
            operation="regenerate",
            expected_projection_revision=latest["projectionRevision"],
            config={"model": "gpt-4o"},
            input_update=next_submitted_projection,
            expected_input_projection_revision=submitted["projectionRevision"],
            user_id=1,
        )
        assert replayed["idempotentReplay"] is True
        assert client.query(
            "turn.sync.snapshot",
            {"conversation_id": "conv-sync-a", "user_id": 1},
        )["syncSequence"] == before_attempt_head + 2

        client.command(
            "turn.sync.prune",
            {
                "created_before_ms": int(time.time() * 1000) + 10_000,
                "max_rows": 100,
            },
            "prune-conversation-sync-replay",
        )
        expired = client.query(
            "turn.sync.changes",
            {"conversation_id": "conv-sync-a", "user_id": 1, "after": 0},
        )
        assert expired["resetRequired"] is True
        assert expired["resetReason"] == "cursor_expired"
        assert service.snapshot("conv-sync-a", 1)["syncSeq"] == (
            before_attempt_head + 2
        )
    finally:
        unsubscribe()


def test_turn_history_page_is_bounded_owner_scoped_and_exclusive(
    conversation_sync_db,
):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.conversation_sync.validation import decode

    client = conversation_sync_db
    for ordinal in range(7):
        client.command(
            "turn.append_settled",
            {
                "conversation_id": "conv-sync-a",
                "user_id": 1,
                "command_id": f"history-page-{ordinal}",
                "actor": "assistant",
                "kind": "fixture",
                "projection": {"content": f"page-{ordinal}-" * 100},
                "created_at": ordinal + 1,
            },
            f"history-page-{ordinal}",
        )

    head = client.query(
        "turn.sync.snapshot",
        {"conversation_id": "conv-sync-a", "user_id": 1},
    )["syncSequence"]
    first = client.query(
        "turn.sync.page",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "lane_id": "main",
            "sync_sequence": head,
            "limit": 3,
        },
    )
    assert [turn["ordinal"] for turn in first["turns"]] == [4, 5, 6]
    assert first["nextBeforeOrdinal"] == 4
    assert first["hasMore"] is True
    assert first["totalTurns"] == 7
    assert len(first["attempts"]) == 3
    assert {attempt["turnId"] for attempt in first["attempts"]} == {
        turn["turnId"] for turn in first["turns"]
    }

    second = client.query(
        "turn.sync.page",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "lane_id": "main",
            "sync_sequence": first["syncSequence"],
            "before_ordinal": first["nextBeforeOrdinal"],
            "limit": 3,
        },
    )
    assert [turn["ordinal"] for turn in second["turns"]] == [1, 2, 3]
    assert {turn["turnId"] for turn in first["turns"]}.isdisjoint(
        turn["turnId"] for turn in second["turns"]
    )
    assert second["nextBeforeOrdinal"] == 1
    assert second["hasMore"] is True
    assert client.query(
        "turn.sync.page",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 2,
            "lane_id": "main",
            "sync_sequence": first["syncSequence"],
            "limit": 3,
        },
    ) is None
    stale = client.query(
        "turn.sync.page",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "lane_id": "main",
            "sync_sequence": head - 1,
            "limit": 3,
        },
    )
    assert stale == {"stale": True, "syncSequence": head}

    service = ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda write=False: client,
    ))
    page = service.turn_page(
        "conv-sync-a",
        1,
        lane_id="main",
        expected_sync_sequence=first["syncSequence"],
        limit=3,
        segment_payload="refs",
    )
    assert decode("ConversationTurnPage", page) is page
    assert page["syncSeq"] == first["syncSequence"]
    assert len(page["turns"]) == 3
    assert len(page["snapshotProjectionRefs"]) == 3
    assert all("content" not in turn["projection"] for turn in page["turns"])


def test_snapshot_tail_window_is_bounded_and_branch_safe(conversation_sync_db):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.conversation_sync.validation import decode

    client = conversation_sync_db
    for ordinal in range(7):
        client.command(
            "turn.append_settled",
            {
                "conversation_id": "conv-sync-a",
                "user_id": 1,
                "command_id": f"snapshot-window-{ordinal}",
                "actor": "assistant",
                "projection": {"content": f"tail-{ordinal}"},
                "created_at": ordinal + 1,
            },
            f"snapshot-window-{ordinal}",
        )

    full = client.query(
        "turn.sync.snapshot",
        {"conversation_id": "conv-sync-a", "user_id": 1},
    )
    assert len(full["turns"]) == 7
    assert "turnWindow" not in full
    assert "hasArtifacts" not in full
    empty_hint = client.query(
        "turn.sync.snapshot",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "include_artifact_hint": True,
        },
    )
    assert empty_hint["hasArtifacts"] is False
    artifact_id = f"snapshot-hint-{uuid.uuid4().hex}"
    client.command(
        "artifact.create",
        {
            "artifact_id": artifact_id,
            "conv_id": "conv-sync-a",
            "source": "test",
            "format": "markdown",
            "content": "bounded hint",
            "created_at": 2,
        },
        f"create-{artifact_id}",
    )
    assert client.query(
        "turn.sync.snapshot",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "include_artifact_hint": True,
        },
    )["hasArtifacts"] is True
    assert client.query(
        "turn.sync.snapshot",
        {
            "conversation_id": "conv-sync-b",
            "user_id": 1,
            "include_artifact_hint": True,
        },
    )["hasArtifacts"] is False
    tail = client.query(
        "turn.sync.snapshot",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "turn_limit": 3,
        },
    )
    assert [turn["ordinal"] for turn in tail["turns"]] == [4, 5, 6]
    assert tail["turnWindow"] == {
        "laneId": "main",
        "nextBeforeOrdinal": 4,
        "hasMore": True,
        "totalTurns": 7,
    }
    assert len(tail["attempts"]) == 3

    service = ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda write=False: client,
    ))
    public_tail = service.snapshot(
        "conv-sync-a",
        1,
        turn_limit=3,
        segment_payload="refs",
        include_artifact_hint=True,
    )
    assert decode("ConversationSyncSnapshot", public_tail) is public_tail
    assert public_tail["turnWindow"] == tail["turnWindow"]
    assert len(public_tail["turns"]) == 3
    assert public_tail["hasArtifacts"] is True

    client.command(
        "turn.append_settled",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "command_id": "snapshot-window-branch",
            "lane_id": "branch-safe",
            "actor": "assistant",
            "projection": {"content": "branch"},
            "created_at": 20,
        },
        "snapshot-window-branch",
    )
    branch_safe = client.query(
        "turn.sync.snapshot",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "turn_limit": 3,
        },
    )
    assert len(branch_safe["turns"]) == 8
    assert "turnWindow" not in branch_safe
    assert {turn["laneId"] for turn in branch_safe["turns"]} == {
        "main", "branch-safe"
    }
    client.command(
        "artifact.delete",
        {"artifact_id": artifact_id, "deleted_at": 3},
        f"delete-{artifact_id}",
    )


def test_conversation_delete_is_hidden_and_restore_rebuilds_settled_authority(
    conversation_sync_db,
):
    from lib.turn_lifecycle import create_turn_pair

    client = conversation_sync_db
    created = create_turn_pair(
        "conv-sync-b",
        command_id="delete-lifecycle-create",
        input_projection={"content": "question"},
        config={},
        user_id=1,
    )
    attempt_id = created["attempt"]["attemptId"]
    deleted = client.command(
        "conversation.delete",
        {"conv_id": "conv-sync-b", "user_id": 1},
        "delete-lifecycle-command",
    )
    assert deleted["deleted"] is True
    assert deleted["recoverable"] is True
    assert client.query(
        "turn.sync.snapshot",
        {"conversation_id": "conv-sync-b", "user_id": 1},
    ) is None
    assert client.query(
        "turn.attempt.get", {"attempt_id": attempt_id, "user_id": 1}
    ) is None
    assert client.query(
        "turn.events.list", {"attempt_id": attempt_id, "user_id": 1},
    ) is None
    restored = client.command(
        "conversation.restore",
        {"conv_id": "conv-sync-b", "user_id": 1},
        "restore-cascade-command",
    )
    assert restored["restored"] is True
    snapshot = client.query(
        "turn.sync.snapshot",
        {"conversation_id": "conv-sync-b", "user_id": 1},
    )
    assert [turn["status"] for turn in snapshot["turns"]] == [
        "completed", "interrupted"
    ]
    assert snapshot["attempts"] == []


def test_v3_create_retry_and_snapshot_share_one_authority(
    flask_client, conversation_sync_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime
    from routes import conversation_sync_v3 as sync_routes

    starts: list[str] = []

    def fake_start(*args, **kwargs):
        starts.append("task-sync-a")
        kwargs["on_task_registered"]("task-sync-a")
        return "task-sync-a", None

    monkeypatch.setattr(task_start_runtime, "start_conversation_attempt_executor", fake_start)
    body = {
        "commandId": "sync-command-a",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }
    first_response = flask_client.post(
        "/api/v3/conversations/conv-sync-a/turns", json=body)
    assert first_response.status_code == 200
    first = first_response.get_json()
    assert first["ok"] is True
    assert first["conversationId"] == "conv-sync-a"
    assert first["turn"]["status"] == "pending"

    retry = flask_client.post(
        "/api/v3/conversations/conv-sync-a/turns", json=body).get_json()
    assert retry["idempotentReplay"] is True
    assert retry["turn"]["turnId"] == first["turn"]["turnId"]
    assert retry["attempt"]["attemptId"] == first["attempt"]["attemptId"]
    assert starts == ["task-sync-a"]

    response = flask_client.get("/api/v3/conversations/conv-sync-a/sync")
    assert response.status_code == 200
    snapshot = response.get_json()
    assert snapshot["contract"] == "tofu.conversation-sync.snapshot/v1"
    assert snapshot["scope"] == {
        "kind": "conversation", "ownerId": 1, "threadId": "conv-sync-a",
    }
    assert snapshot["conversationId"] == "conv-sync-a"
    assert isinstance(snapshot["cursor"], str) and snapshot["cursor"]
    assert snapshot["syncSeq"] >= 1
    assert any(
        turn["turnId"] == first["turn"]["turnId"]
        for turn in snapshot["turns"]
    )
    assert any(
        attempt["attemptId"] == first["attempt"]["attemptId"]
        for attempt in snapshot["attempts"]
    )
    assert sync_routes._service.sequence_from_cursor(
        "conv-sync-a", 1, snapshot["cursor"],
    ) == snapshot["syncSeq"]
    replay = sync_routes._service.changes(
        "conv-sync-a", 1, after_sequence=0,
    )
    assert replay["reset"] is None
    assert replay["events"]
    sequences = [event["syncSeq"] for event in replay["events"]]
    assert sequences == list(range(1, len(sequences) + 1))
    assert all(
        event["conversationId"] == "conv-sync-a"
        for event in replay["events"]
    )


def test_v3_rejects_schema_drift_and_cross_conversation_cursor(
    flask_client, conversation_sync_db,
):
    invalid = flask_client.post(
        "/api/v3/conversations/conv-sync-a/turns",
        json={"commandId": "missing-config"},
    )
    assert invalid.status_code == 400
    invalid_body = invalid.get_json()
    assert "CreateTurnRequest contract violation" in invalid_body["error"]
    assert invalid_body["violations"]

    cursor = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync").get_json()["cursor"]
    wrong_scope = flask_client.get(
        "/api/v3/conversations/conv-sync-b/events",
        query_string={"after": cursor},
    )
    assert wrong_scope.status_code == 400

    # Native EventSource gives Last-Event-ID precedence on reconnect. An
    # invalid header must not be hidden by a valid bootstrap query cursor.
    header_precedence = flask_client.get(
        "/api/v3/conversations/conv-sync-a/events",
        query_string={"after": cursor},
        headers={"Last-Event-ID": "not-a-conversation-cursor"},
    )
    assert header_precedence.status_code == 400

    incomplete_stream_owner = flask_client.get(
        "/api/v3/conversations/conv-sync-a/events",
        query_string={"streamClientId": "page-a"},
    )
    assert incomplete_stream_owner.status_code == 400

    invalid_stream_generation = flask_client.get(
        "/api/v3/conversations/conv-sync-a/events",
        query_string={
            "streamClientId": "page-a",
            "streamGeneration": "0",
        },
    )
    assert invalid_stream_generation.status_code == 400

    invalid_segment_payload = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={"segmentPayload": "compact"},
    )
    assert invalid_segment_payload.status_code == 400
    assert invalid_segment_payload.get_json()["error"] == (
        "Invalid conversation segment payload"
    )

    duplicate_segment_payload = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync?segmentPayload=refs&segmentPayload=full"
    )
    assert duplicate_segment_payload.status_code == 400

    invalid_turn_window = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={"turnWindow": "tail-unbounded"},
    )
    assert invalid_turn_window.status_code == 400
    assert invalid_turn_window.get_json()["error"] == (
        "Invalid conversation snapshot turn window"
    )

    duplicate_turn_window = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync?turnWindow=tail-96&turnWindow=full"
    )
    assert duplicate_turn_window.status_code == 400

    invalid_artifact_hint = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={"artifactHint": "metadata"},
    )
    assert invalid_artifact_hint.status_code == 400
    assert invalid_artifact_hint.get_json()["error"] == (
        "Invalid conversation snapshot artifact hint"
    )

    duplicate_artifact_hint = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync?"
        "artifactHint=has-any&artifactHint=has-any"
    )
    assert duplicate_artifact_hint.status_code == 400

    reference_snapshot = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={"segmentPayload": "refs"},
    )
    assert reference_snapshot.status_code == 200
    reference_payload = reference_snapshot.get_json()
    assert reference_payload["conversationId"] == "conv-sync-a"
    assert "hasArtifacts" not in reference_payload

    bounded_snapshot = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={
            "segmentPayload": "refs",
            "turnWindow": "tail-96",
            "artifactHint": "has-any",
        },
    )
    assert bounded_snapshot.status_code == 200
    assert bounded_snapshot.get_json()["turnWindow"] == {
        "laneId": "main",
        "nextBeforeOrdinal": None,
        "hasMore": False,
        "totalTurns": 0,
    }
    assert bounded_snapshot.get_json()["hasArtifacts"] is False

    history_page = flask_client.get(
        "/api/v3/conversations/conv-sync-a/turns/history",
        query_string={
            "laneId": "main",
            "syncSeq": reference_payload["syncSeq"],
            "limit": 1,
        },
    )
    assert history_page.status_code == 200
    assert history_page.get_json()["contract"] == (
        "tofu.conversation-sync.turn-page/v1"
    )

    missing_history_lane = flask_client.get(
        "/api/v3/conversations/conv-sync-a/turns/history",
    )
    assert missing_history_lane.status_code == 400
    oversized_history_page = flask_client.get(
        "/api/v3/conversations/conv-sync-a/turns/history",
        query_string={
            "laneId": "main",
            "syncSeq": reference_payload["syncSeq"],
            "limit": 257,
        },
    )
    assert oversized_history_page.status_code == 400
    duplicate_history_cursor = flask_client.get(
        "/api/v3/conversations/conv-sync-a/turns/history"
        f"?laneId=main&syncSeq={reference_payload['syncSeq']}"
        "&beforeOrdinal=2&beforeOrdinal=1",
    )
    assert duplicate_history_cursor.status_code == 400
    stale_history_page = flask_client.get(
        "/api/v3/conversations/conv-sync-a/turns/history",
        query_string={
            "laneId": "main",
            "syncSeq": reference_payload["syncSeq"] + 1,
            "limit": 1,
        },
    )
    assert stale_history_page.status_code == 409
    assert stale_history_page.get_json()["error"] == "history_page_stale"


def test_refs_snapshot_serves_legacy_turn_images_lazily_and_immutably(
    flask_client, conversation_sync_db,
):
    import base64

    raw = b"\x89PNG\r\n\x1a\n" + (b"legacy-image" * 100)
    encoded = base64.b64encode(raw).decode("ascii")
    conversation_sync_db.command(
        "turn.append_settled",
        {
            "conversation_id": "conv-sync-a",
            "user_id": 1,
            "command_id": "legacy-turn-image",
            "actor": "assistant",
            "kind": "fixture",
            "projection": {
                "content": "historical image",
                "images": [{
                    "base64": encoded,
                    "preview": f"data:image/png;base64,{encoded}",
                    "mediaType": "image/png",
                }],
            },
            "created_at": 2,
        },
        "legacy-turn-image",
    )
    turn = conversation_sync_db.query(
        "turn.list",
        {"conversation_id": "conv-sync-a", "user_id": 1},
    )[0]

    full = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
    ).get_json()
    assert full["turns"][0]["projection"]["images"][0]["base64"] == encoded

    refs = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync",
        query_string={"segmentPayload": "refs"},
    ).get_json()
    image = refs["turns"][0]["projection"]["images"][0]
    assert "base64" not in image
    assert (
        f"/turns/{turn['turnId']}/images/0"
        f"?projectionRevision={turn['projectionRevision']}"
    ) in image["preview"]
    owner_scope = image["preview"].split("ownerScope=", 1)[1]

    loaded = flask_client.get(image["preview"])

    assert loaded.status_code == 200
    assert loaded.get_data() == raw
    assert loaded.headers["Content-Type"].startswith("image/png")
    assert loaded.headers["Cache-Control"] == (
        "private, max-age=31536000, immutable"
    )
    assert loaded.headers["X-Content-Type-Options"] == "nosniff"
    assert loaded.headers["ETag"]

    not_modified = flask_client.get(
        image["preview"],
        headers={"If-None-Match": loaded.headers["ETag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.get_data() == b""

    wrong_owner_scope = image["preview"][:-1] + (
        "0" if image["preview"][-1] != "0" else "1"
    )
    assert flask_client.get(wrong_owner_scope).status_code == 404

    missing_fence = flask_client.get(image["preview"].split("?", 1)[0])
    assert missing_fence.status_code == 400
    stale = flask_client.get(
        image["preview"].split("?", 1)[0],
        query_string={
            "projectionRevision": turn["projectionRevision"] + 1,
            "ownerScope": owner_scope,
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["currentProjectionRevision"] == (
        turn["projectionRevision"]
    )


def test_v3_stream_capacity_stops_native_reconnect_without_leaking_subscription(
    flask_client, conversation_sync_db, monkeypatch,
):
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as sync_routes

    class RefusingLimiter:
        @staticmethod
        def try_acquire(_principal):
            return None

    isolated_broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(sync_routes, "broker", isolated_broker)
    monkeypatch.setattr(sync_routes, "sse_limiter", RefusingLimiter())

    response = flask_client.get(
        "/api/v3/conversations/conv-sync-a/events",
        query_string={
            "streamClientId": "page-a",
            "streamGeneration": "1",
        },
    )

    assert response.status_code == 204
    assert response.headers["X-Tofu-Stream-Admission"] == "capacity"
    assert response.headers["Cache-Control"] == "no-store"
    assert isolated_broker.snapshot()["active"] == 0


def test_v3_finite_stream_releases_exact_shared_slot_and_subscription(
    flask_client, monkeypatch,
):
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as sync_routes

    class FiniteService:
        heartbeat_interval_ms = 15_000

        @staticmethod
        def sequence_from_cursor(_conversation_id, _user_id, _cursor):
            return 0

        @staticmethod
        def changes(
            conversation_id, _user_id, *, after_sequence, limit,
        ):
            assert after_sequence == 0
            if limit == 1:
                return {"events": [], "reset": None, "hasMore": False}
            return {
                "events": [],
                "hasMore": False,
                "reset": {
                    "contract": "tofu.conversation-sync.event/v1",
                    "type": "sync.reset_required",
                    "conversationId": conversation_id,
                    "cursor": "cursor-reset",
                    "reason": "test_complete",
                },
            }

    class RecordingLimiter:
        refresh_interval_seconds = 60.0

        def __init__(self):
            self.released: list[str] = []

        @staticmethod
        def try_acquire(_principal):
            return "slot-a"

        def release(self, token):
            self.released.append(token)

        @staticmethod
        def refresh(_token):
            raise AssertionError("finite stream should not need a lease refresh")

    isolated_broker = ConversationWakeBroker(owner_history_capacity=16)
    limiter = RecordingLimiter()
    monkeypatch.setattr(sync_routes, "_service", FiniteService())
    monkeypatch.setattr(sync_routes, "broker", isolated_broker)
    monkeypatch.setattr(sync_routes, "sse_limiter", limiter)

    response = flask_client.get(
        "/api/v3/conversations/conv-finite/events",
        query_string={
            "streamClientId": "page-a",
            "streamGeneration": "1",
        },
    )

    assert response.status_code == 200
    assert b"event: sync.reset_required" in response.data
    assert limiter.released == ["slot-a"]
    assert isolated_broker.snapshot()["active"] == 0


def test_v3_start_failure_persists_and_returns_complete_actionable_error(
    flask_client, conversation_sync_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    starts: list[bool] = []

    def fail_start(*args, **kwargs):
        starts.append(True)
        return None, object()

    monkeypatch.setattr(task_start_runtime, "start_conversation_attempt_executor", fail_start)
    body = {
        "commandId": "sync-start-failure",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }
    response = flask_client.post(
        "/api/v3/conversations/conv-sync-a/turns", json=body)
    assert response.status_code == 500
    payload = response.get_json()
    error = payload["error"]
    required = {
        "kind", "severity", "retryable", "message", "hint", "detail",
        "model", "context", "source", "raw",
    }
    assert required <= error.keys()
    assert error["kind"] == "task_start_failed"
    assert error["retryable"] is True

    latest = payload["latestTurn"]
    persisted_error = latest["settlement"]["error"]
    assert required <= persisted_error.keys()
    assert persisted_error["kind"] == "task_start_failed"
    assert persisted_error["message"] == error["message"]

    snapshot = flask_client.get(
        "/api/v3/conversations/conv-sync-a/sync").get_json()
    stored = next(
        turn for turn in snapshot["turns"]
        if turn["turnId"] == latest["turnId"]
    )
    assert stored["status"] == "failed"
    assert stored["settlement"]["error"] == persisted_error

    retry = flask_client.post(
        "/api/v3/conversations/conv-sync-a/turns", json=body).get_json()
    assert retry["idempotentReplay"] is True
    assert starts == [True]


def test_push_withheld_wedge_rides_snapshot_and_heartbeat(
    flask_client, conversation_sync_db,
):
    """A withheld-push storage wedge reaches the client over read-side frames.

    The wedge's own authoritative frames can never ship (that IS the wedge),
    so the sync snapshot + SSE heartbeat carry ``pushWithheld``, stamped from
    the live task's ``_pushWithheldAt`` marker (TaskRuntime.append_event).
    """
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.tasks_pkg.manager import runtime as task_runtime_module
    from tests._registered_chat_task import registered_chat_task

    client = conversation_sync_db
    service = ConversationSyncService(SidecarConversationSyncRepository(
        client_factory=lambda write=False: client,
    ))
    task = {
        "id": "task-wedge-a",
        "status": "running",
        "_pushWithheldAt": time.time(),
        "_pushWithheldCount": 3,
    }
    with registered_chat_task(task, user_id=1):
        task_runtime_module._record_latest_task("conv-sync-a", task["id"])
        try:
            probe = task_runtime_module.push_withheld_for_conv("conv-sync-a")
            assert probe["taskId"] == "task-wedge-a"
            assert probe["count"] == 3

            # The snapshot route ships the flag on the wire payload…
            payload = flask_client.get(
                "/api/v3/conversations/conv-sync-a/sync").get_json()
            assert payload["pushWithheld"] is True

            # …and the service stamps it on heartbeats (contract-validated).
            beat = service.heartbeat(
                "conv-sync-a", 1, 1, degraded=True, push_withheld=True)
            assert beat["degraded"] is True
            assert beat["pushWithheld"] is True

            # A terminal task's stale marker never re-arms the signal.
            task["status"] = "completed"
            assert task_runtime_module.push_withheld_for_conv(
                "conv-sync-a") is None
            task["status"] = "running"

            # Recovery: the marker popped → the next snapshot clears the flag
            # (explicit false, never an absent key the client must guess).
            task.pop("_pushWithheldAt")
            task.pop("_pushWithheldCount")
            cleared = flask_client.get(
                "/api/v3/conversations/conv-sync-a/sync").get_json()
            assert cleared["pushWithheld"] is False
            assert task_runtime_module.push_withheld_for_conv(
                "conv-sync-a") is None
        finally:
            task_runtime_module._clear_latest_task(
                "conv-sync-a", expect_task_id="task-wedge-a")

    # Unknown conversation / no live task → no wedge signal.
    assert task_runtime_module.push_withheld_for_conv("conv-sync-a") is None
    assert task_runtime_module.push_withheld_for_conv("conv-never") is None
    assert task_runtime_module.push_withheld_for_conv("") is None

def test_attempt_scoped_frontend_stream_owner_is_retired():
    assert not (ROOT / "frontend/src/core/attempt-stream.ts").exists()
    runtime = (ROOT / "frontend/src/core/turn-runtime.ts").read_text(
        encoding="utf-8")
    assert "from './conversation-sync'" in runtime
    assert "createAttemptEventStream" not in runtime
    assert "streamCursor" not in runtime


def test_browser_has_one_generated_sync_owner_and_no_v2_fallback():
    generated = (ROOT / "frontend/src/api/conversation-sync.generated.ts").read_text(
        encoding="utf-8"
    )
    app_runtime = (ROOT / "frontend/src/runtime/app-runtime.js").read_text(
        encoding="utf-8"
    )
    coordinator = (ROOT / "frontend/src/core/conversation-sync.ts").read_text(
        encoding="utf-8"
    )
    routes = (ROOT / "routes/conversation_sync_v3.py").read_text(
        encoding="utf-8"
    )

    assert "/api/v3/conversations/" in generated
    assert "turnImageUrl(" in generated
    assert "projectionRevision=${encodeURIComponent" in generated
    assert "/api/v2/conversations/" not in generated
    assert "Api.turnsV2" not in app_runtime
    assert "/api/v2/conversations/" not in app_runtime
    assert app_runtime.count("api: conversationSyncApi") == 1
    assert coordinator.count("new EventSource(") == 1
    assert "from routes.turns_v2" not in routes
    assert not (ROOT / "routes/turns_v2.py").exists()

    invalidation_start = app_runtime.index(
        "function _onConversationInvalidation(")
    invalidation_end = app_runtime.index(
        "runtimeScope._onConversationInvalidation", invalidation_start)
    invalidation_handler = app_runtime[invalidation_start:invalidation_end]
    assert "invalidateConversation" in invalidation_handler
    assert "hydrate" not in invalidation_handler
    assert "dispatch" not in invalidation_handler


def test_browser_does_not_consume_server_only_snapshot_evidence_fields():
    authored_roots = (
        ROOT / "frontend/src/core",
        ROOT / "frontend/src/conversation",
        ROOT / "frontend/src/runtime/sections",
    )
    server_only_fields = (
        "_responsesItems",
        "_anthropicContentBlocks",
        "_codex_cache",
        "_network_route",
        "_stream_state",
        "_transport_bytes_received",
        "pricingSnapshot",
    )
    consumers = []
    for authored_root in authored_roots:
        for path in authored_root.rglob("*"):
            if path.suffix not in {".ts", ".js"}:
                continue
            source = path.read_text(encoding="utf-8")
            if any(field in source for field in server_only_fields):
                consumers.append(path.relative_to(ROOT).as_posix())
    assert consumers == []
