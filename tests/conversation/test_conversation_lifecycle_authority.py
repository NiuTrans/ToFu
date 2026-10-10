"""Conversation delete/restore/clone invariants at the Sidecar boundary."""

from __future__ import annotations

import time
import uuid

import pytest

from tests._seed import seed_conversation


pytestmark = pytest.mark.unit
pytest_plugins = ("tests._chat_sidecar",)


def _client():
    from lib.storage import get_storage_client

    return get_storage_client(write=True)


def _purge(client, *conversation_ids: str) -> None:
    for conversation_id in conversation_ids:
        client.command(
            "conversation.purge",
            {"conv_id": conversation_id, "user_id": 1},
            f"lifecycle-cleanup:{conversation_id}:{uuid.uuid4().hex}",
        )


def test_delete_hides_authority_and_restore_recovers_the_turn_graph(chat_sidecar):
    client = _client()
    conversation_id = f"trash-{uuid.uuid4().hex}"
    try:
        seed_conversation(
            conversation_id,
            title="Recover me",
            settings={"folderId": "folder-a", "activeTaskId": "stale-task"},
            messages=[
                {"role": "user", "content": "question", "_msgId": "msg-user"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "_msgId": "msg-answer",
                    "_taskId": "historical-task",
                },
            ],
        )
        deleted = client.command(
            "conversation.delete",
            {"conv_id": conversation_id, "user_id": 1},
            f"delete:{conversation_id}",
        )
        assert deleted["deleted"] and deleted["recoverable"]
        assert client.query(
            "conversation.get", {"conv_id": conversation_id, "user_id": 1}
        ) is None
        assert client.query(
            "turn.sync.snapshot",
            {"conversation_id": conversation_id, "user_id": 1},
        ) is None
        assert client.command(
            "conversation.restore",
            {"conv_id": conversation_id, "user_id": 2},
            f"foreign-restore:{conversation_id}",
        )["missing"]

        restored = client.command(
            "conversation.restore",
            {"conv_id": conversation_id, "user_id": 1},
            f"restore:{conversation_id}",
        )
        assert restored["restored"] and restored["turnCount"] == 2
        document = client.query(
            "conversation.get", {"conv_id": conversation_id, "user_id": 1}
        )
        assert [item["content"] for item in document["messages"]] == [
            "question", "answer"
        ]
        assert document["metadata"]["settings"] == {"folderId": "folder-a"}
    finally:
        _purge(client, conversation_id)


def test_clone_is_server_atomic_and_severs_executable_identity(chat_sidecar):
    client = _client()
    source_id = f"clone-source-{uuid.uuid4().hex}"
    destination_id = f"clone-destination-{uuid.uuid4().hex}"
    try:
        seed_conversation(
            source_id,
            title="Source",
            settings={"folderId": "folder-b", "activeTaskId": "live-latch"},
            messages=[
                {"role": "user", "content": "hello", "_msgId": "source-user"},
                {
                    "role": "assistant",
                    "content": "world",
                    "_msgId": "source-assistant",
                    "toolRounds": [{"_taskId": "source-task", "name": "write"}],
                    "approvalRequired": {"id": "must-not-copy"},
                },
            ],
        )
        result = client.command(
            "conversation.clone",
            {
                "conv_id": source_id,
                "destination_conv_id": destination_id,
                "user_id": 1,
                "title": "Source (copy)",
            },
            f"clone:{source_id}:{destination_id}",
        )
        assert result["cloned"] and result["turnCount"] == 2
        source = client.query(
            "conversation.get", {"conv_id": source_id, "user_id": 1}
        )
        clone = client.query(
            "conversation.get", {"conv_id": destination_id, "user_id": 1}
        )
        assert [item["content"] for item in clone["messages"]] == [
            "hello", "world"
        ]
        assert clone["metadata"]["settings"] == {
            "folderId": "folder-b",
            "clonedFrom": source_id,
        }
        assert clone["messages"][0]["_turnId"] != source["messages"][0]["_turnId"]
        assert "_msgId" not in clone["messages"][0]
        clone_task_id = clone["messages"][1]["toolRounds"][0]["_taskId"]
        assert clone_task_id != "source-task"
        assert clone_task_id.startswith("clone-task-")
        assert "approvalRequired" not in clone["messages"][1]
        assert clone["messages"][1]["_attemptId"] is None

        assert client.command(
            "conversation.clone",
            {
                "conv_id": source_id,
                "destination_conv_id": f"foreign-{uuid.uuid4().hex}",
                "user_id": 2,
            },
            f"foreign-clone:{source_id}",
        )["missing"]
    finally:
        _purge(client, source_id, destination_id)



def test_clone_freezes_the_current_visible_state_while_source_keeps_running(
    chat_sidecar,
):
    client = _client()
    source_id = f"live-clone-source-{uuid.uuid4().hex}"
    destination_id = f"live-clone-destination-{uuid.uuid4().hex}"
    task_id = f"live-clone-task-{uuid.uuid4().hex}"
    try:
        seed_conversation(
            source_id,
            title="Generating source",
            settings={"activeTaskId": task_id},
            messages=[],
        )
        created = client.command(
            "turn.create_pair",
            {
                "conversation_id": source_id,
                "user_id": 1,
                "command_id": f"create:{source_id}",
                "input_projection": {"content": "write progressively"},
                "config": {},
            },
            f"create-pair:{source_id}",
        )
        attempt_id = created["attempt"]["attemptId"]
        source_turn_id = created["turn"]["turnId"]
        assert client.command(
            "turn.attempt.bind",
            {"attempt_id": attempt_id, "task_id": task_id, "user_id": 1},
            f"bind:{attempt_id}",
        )["taskId"] == task_id
        partial_projection = {
            "content": "visible partial answer",
            "thinking": "visible partial reasoning",
            "toolRounds": [
                {
                    "attemptId": attempt_id,
                    "taskId": task_id,
                    "roundNum": 1,
                    "toolCallId": "done-call",
                    "toolName": "read_files",
                    "status": "done",
                    "toolContent": "kept result",
                },
                {
                    "attemptId": attempt_id,
                    "taskId": task_id,
                    "roundNum": 2,
                    "toolCallId": "live-call",
                    "toolName": "ask_human",
                    "status": "awaiting_human",
                    "guidanceId": "must-not-remain-live",
                },
            ],
            "timingTrace": {
                "version": 1,
                "taskId": task_id,
                "status": "running",
                "running": True,
            },
            "approvalRequired": {"id": "must-not-copy"},
        }
        first_event = client.command(
            "turn.event.record",
            {
                "attempt_id": attempt_id,
                "user_id": 1,
                "task_id": task_id,
                "projection": partial_projection,
                "terminal": False,
                "status": "running",
                "settlement": {},
                "event_type": "projection_updated",
                "event_payload": {"updateKind": "stream"},
            },
            f"event:{attempt_id}:1",
            priority="event",
        )
        assert first_event["applied"] is True

        cloned = client.command(
            "conversation.clone",
            {
                "conv_id": source_id,
                "destination_conv_id": destination_id,
                "user_id": 1,
            },
            f"clone-live:{source_id}:{destination_id}",
        )
        assert cloned["cloned"] is True
        assert cloned["busy"] is False

        snapshot = client.query(
            "turn.sync.snapshot",
            {"conversation_id": destination_id, "user_id": 1},
        )
        cloned_turn = next(
            turn for turn in snapshot["turns"]
            if turn["actor"] == "assistant"
        )
        assert cloned_turn["status"] == "interrupted"
        assert cloned_turn["currentAttemptId"] is None
        assert cloned_turn["runId"] == ""
        projection = cloned_turn["projection"]
        assert projection["content"] == "visible partial answer"
        assert projection["thinking"] == "visible partial reasoning"
        assert [item["status"] for item in projection["toolRounds"]] == [
            "done", "aborted"
        ]
        assert projection["toolRounds"][0]["toolContent"] == "kept result"
        assert all("attemptId" not in item for item in projection["toolRounds"])
        cloned_task_ids = {
            item["taskId"] for item in projection["toolRounds"]
        }
        assert len(cloned_task_ids) == 1
        assert task_id not in cloned_task_ids
        assert next(iter(cloned_task_ids)).startswith("clone-task-")
        assert projection["timingTrace"]["taskId"] in cloned_task_ids
        assert projection["timingTrace"]["status"] == "aborted"
        assert projection["timingTrace"]["running"] is False
        assert "approvalRequired" not in projection
        tool_segments = [
            item for item in projection["segments"]
            if item.get("type") == "tool_use"
        ]
        assert [item["result"]["status"] for item in tool_segments] == [
            "done", "aborted"
        ]

        source_before = client.query(
            "turn.get",
            {"conversation_id": source_id, "turn_id": source_turn_id, "user_id": 1},
        )
        assert source_before["status"] == "running"
        source_revision = source_before["projectionRevision"]
        second_event = client.command(
            "turn.event.record",
            {
                "attempt_id": attempt_id,
                "user_id": 1,
                "task_id": task_id,
                "projection": {**partial_projection, "content": "source advanced"},
                "terminal": False,
                "status": "running",
                "settlement": {},
                "event_type": "projection_updated",
                "event_payload": {"updateKind": "stream"},
            },
            f"event:{attempt_id}:2",
            priority="event",
        )
        assert second_event["applied"] is True
        assert client.query(
            "turn.get",
            {"conversation_id": source_id, "turn_id": source_turn_id, "user_id": 1},
        )["projectionRevision"] == source_revision + 1
        clone_document = client.query(
            "conversation.get", {"conv_id": destination_id, "user_id": 1}
        )
        assert clone_document["messages"][-1]["content"] == "visible partial answer"
        assert clone_document["metadata"]["settings"] == {
            "clonedFrom": source_id
        }
    finally:
        _purge(client, source_id, destination_id)

def test_trash_retention_prune_is_bounded_and_irreversible(chat_sidecar):
    client = _client()
    conversation_id = f"expired-trash-{uuid.uuid4().hex}"
    seed_conversation(conversation_id, messages=[], title="Expired")
    client.command(
        "conversation.delete",
        {"conv_id": conversation_id, "user_id": 1},
        f"delete-expired:{conversation_id}",
    )
    result = client.command(
        "conversation.trash.prune",
        {
            "deleted_before_ms": int(time.time() * 1000) + 1,
            "max_conversations": 1,
        },
        None,
    )
    assert result["purgedConversations"] == 1
    assert client.command(
        "conversation.restore",
        {"conv_id": conversation_id, "user_id": 1},
        f"restore-expired:{conversation_id}",
    )["missing"]
