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
        assert clone["metadata"]["settings"] == {"folderId": "folder-b"}
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
