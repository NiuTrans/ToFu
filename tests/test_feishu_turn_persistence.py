"""Feishu durable history uses owner-scoped turn authority, never a blob mirror."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture()
def sidecar(tmp_path: Path, monkeypatch):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend="sqlite", startup_timeout=60
    )
    supervisor.start()
    monkeypatch.setattr(
        "lib.storage.get_storage_client",
        lambda write=False: supervisor.client,
    )
    try:
        yield supervisor
    finally:
        supervisor.stop()


def test_owner_mapping_is_explicit(monkeypatch):
    from lib.feishu.conversation import resolve_owner_user_id

    monkeypatch.delenv("FEISHU_DEFAULT_OWNER_USER_ID", raising=False)
    monkeypatch.setenv("FEISHU_USER_OWNER_MAP", '{"ou_a": 17}')
    assert resolve_owner_user_id("ou_a") == 17
    assert resolve_owner_user_id("ou_missing") is None

    monkeypatch.setenv("FEISHU_USER_OWNER_MAP", "")
    monkeypatch.setenv("FEISHU_DEFAULT_OWNER_USER_ID", "23")
    assert resolve_owner_user_id("ou_any") == 23


def test_completed_exchange_is_two_canonical_turns(sidecar, monkeypatch):
    import lib.feishu.conversation as conversation

    monkeypatch.setattr(conversation, "get_conv_id", lambda _user: "feishu-conv")
    assert conversation.persist_exchange(
        "ou_a",
        {"id": "message-1", "role": "user", "content": "hello", "timestamp": 10},
        {
            "id": "answer-1",
            "role": "assistant",
            "content": "world",
            "model": "test-model",
            "timestamp": 20,
        },
        owner_user_id=17,
    )

    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-conv", "user_id": 17}
    )
    assert [turn["actor"] for turn in turns] == ["human", "assistant"]
    assert [turn["kind"] for turn in turns] == ["feishu_input", "feishu_reply"]
    assert turns[0]["projection"]["content"] == "hello"
    assert turns[1]["projection"]["content"] == "world"
    assert turns[1]["status"] == "completed"

    document = sidecar.client.query(
        "conversation.get",
        {"conv_id": "feishu-conv", "user_id": 17, "derive_messages": True},
    )
    assert [message["content"] for message in document["messages"]] == [
        "hello",
        "world",
    ]
    assert document["metadata"]["msg_count"] == 2


def test_same_source_message_is_idempotent(sidecar, monkeypatch):
    import lib.feishu.conversation as conversation

    monkeypatch.setattr(conversation, "get_conv_id", lambda _user: "feishu-replay")
    user = {"id": "stable-message", "role": "user", "content": "q"}
    answer = {"role": "assistant", "content": "a", "model": "m"}
    assert conversation.persist_exchange(
        "ou_a", user, answer, owner_user_id=17
    )
    assert conversation.persist_exchange(
        "ou_a", user, answer, owner_user_id=17
    )
    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-replay", "user_id": 17}
    )
    assert len(turns) == 2


def test_concurrent_source_replay_settles_one_exchange(sidecar, monkeypatch):
    import lib.feishu.conversation as conversation

    monkeypatch.setattr(
        conversation, "get_conv_id", lambda _user: "feishu-concurrent"
    )
    user = {"id": "concurrent-message", "role": "user", "content": "q"}
    answer = {"role": "assistant", "content": "a", "model": "m"}
    start_barrier = threading.Barrier(2)

    def persist() -> bool:
        start_barrier.wait(timeout=5)
        return conversation.persist_exchange(
            "ou_a", user, answer, owner_user_id=17
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: persist(), range(2)))

    assert results == [True, True]
    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-concurrent", "user_id": 17}
    )
    assert len(turns) == 2
    assert turns[1]["status"] == "completed"
