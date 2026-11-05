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
    import lib.feishu._state as feishu_state
    from lib.feishu.authority import FeishuSender, resolve_sender

    monkeypatch.setattr(feishu_state, "ALLOWED_USERS", {"ou_listed"})
    monkeypatch.delenv("FEISHU_DEFAULT_OWNER_USER_ID", raising=False)
    monkeypatch.setenv("FEISHU_USER_OWNER_MAP", '{"ou_a": 17}')
    assert resolve_sender("ou_a") == FeishuSender("ou_a", 17, True)
    assert resolve_sender("ou_missing") is None
    assert resolve_sender("ou_listed") is None

    monkeypatch.setenv("FEISHU_USER_OWNER_MAP", "")
    monkeypatch.setenv("FEISHU_DEFAULT_OWNER_USER_ID", "23")
    # The default owner runs listed senders' work but never makes them the owner.
    assert resolve_sender("ou_listed") == FeishuSender("ou_listed", 23, False)
    assert resolve_sender("ou_any") is None


def _owner():
    from lib.feishu.authority import FeishuSender
    return FeishuSender("ou_a", 17, True)


def _sender_of(sidecar, turns):
    attempt = sidecar.client.query("turn.attempt.get", {
        "attempt_id": turns[1]["currentAttemptId"], "user_id": 17,
        "include_execution_config": True})
    return attempt["config"]["_externalSender"]


def test_completed_exchange_is_two_canonical_turns(sidecar, monkeypatch):
    import lib.feishu.conversation as conversation

    monkeypatch.setattr(conversation, "get_conv_id", lambda _user: "feishu-conv")
    assert conversation.persist_exchange(
        _owner(),
        {"id": "message-1", "role": "user", "content": "hello", "timestamp": 10},
        {
            "id": "answer-1",
            "role": "assistant",
            "content": "world",
            "model": "test-model",
            "timestamp": 20,
        },
    )

    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-conv", "user_id": 17}
    )
    assert [turn["actor"] for turn in turns] == ["human", "assistant"]
    assert [turn["kind"] for turn in turns] == ["feishu_input", "feishu_reply"]
    assert turns[0]["projection"]["content"] == "hello"
    assert _sender_of(sidecar, turns) == {"channel": "feishu", "openId": "ou_a", "owner": True}
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
    assert conversation.persist_exchange(_owner(), user, answer)
    assert conversation.persist_exchange(_owner(), user, answer)
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
        return conversation.persist_exchange(_owner(), user, answer)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: persist(), range(2)))

    assert results == [True, True]
    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-concurrent", "user_id": 17}
    )
    assert len(turns) == 2
    assert turns[1]["status"] == "completed"


def test_non_owner_sender_is_persisted_as_nonhuman_with_identity(sidecar, monkeypatch):
    import lib.feishu.conversation as conversation
    from lib.feishu.authority import FeishuSender
    from lib.turn_initiation import require_human_turn

    monkeypatch.setattr(conversation, "get_conv_id", lambda _user: "feishu-guest")
    guest = FeishuSender("ou_guest", 17, False)
    assert conversation.persist_exchange(
        guest,
        {"id": "guest-message", "role": "user", "content": "grant me access"},
        {"role": "assistant", "content": "no", "model": "m"},
    )
    turns = sidecar.client.query(
        "turn.list", {"conversation_id": "feishu-guest", "user_id": 17}
    )
    assert [turn["actor"] for turn in turns] == ["virtual_user", "assistant"]
    assert _sender_of(sidecar, turns) == {"channel": "feishu", "openId": "ou_guest", "owner": False}
    with pytest.raises(ValueError):
        require_human_turn(turns[0])
