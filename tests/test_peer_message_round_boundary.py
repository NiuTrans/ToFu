"""Exactly-once contracts for durable peer messages and their live fast path."""

from __future__ import annotations

from contextlib import contextmanager
import time

import pytest


pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1


@contextmanager
def _live_task(conversation_id: str, *, aborted: bool = False):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    task_id = f"peer-live-{time.time_ns()}"
    task = {
        "id": task_id,
        "convId": conversation_id,
        "_userId": TEST_OWNER_USER_ID,
        "status": "running",
        "aborted": aborted,
        "config": {"model": "test-model"},
    }
    with tasks_lock:
        tasks[task_id] = task
    try:
        yield task
    finally:
        with tasks_lock:
            tasks.pop(task_id, None)


@pytest.fixture
def peer_environment(tmp_path, monkeypatch, chat_sidecar):
    from lib import agent_inbox
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    from tests._seed import delete_conversation, seed_conversation
    import lib.conversations.project_peer as peer
    import lib.message_queue as queue
    target_id = "peer-target-conversation"
    sender_id = "peer-sender-conversation"
    for conversation_id in (target_id, sender_id):
        seed_conversation(conversation_id, title="Peer test")

    with peer._rate_lock:
        peer._peer_msg_history.clear()
    agent_inbox.reset_for_test(target_id)
    agent_inbox.reset_for_test(sender_id)
    with tasks_lock:
        for task_id, task in list(tasks.items()):
            if task.get("convId") in {target_id, sender_id}:
                tasks.pop(task_id, None)

    submitted = []

    def submit(conversation_id, user_id, command):
        attempt_id = f"peer-attempt-{len(submitted) + 1}"
        submitted.append({
            "conversationId": conversation_id,
            "userId": user_id,
            "command": command,
            "attemptId": attempt_id,
        })
        return {"attempt": {"attemptId": attempt_id}}

    monkeypatch.setattr(queue, "_submit_queued_turn_command", submit)

    yield {
        "target": target_id,
        "sender": sender_id,
        "project": str(tmp_path),
        "submitted": submitted,
    }

    agent_inbox.reset_for_test(target_id)
    agent_inbox.reset_for_test(sender_id)
    with tasks_lock:
        for task_id, task in list(tasks.items()):
            if task.get("convId") in {target_id, sender_id}:
                tasks.pop(task_id, None)
    for conversation_id in (target_id, sender_id):
        delete_conversation(conversation_id)


def _send(environment, *, text="check the parser", human=False, wake=True):
    from lib.conversations.project_peer import send_peer_message

    return send_peer_message(
        environment["project"],
        environment["sender"],
        environment["target"],
        text,
        human=human,
        wake=wake,
        user_id=TEST_OWNER_USER_ID,
    )


def test_idle_target_is_dispatched_immediately_as_peer_turn(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import get_queue

    result = _send(peer_environment)

    assert result["ok"] is True
    assert get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID) == []
    assert agent_inbox.peek(peer_environment["target"]) == 0
    [submission] = peer_environment["submitted"]
    projection = submission["command"]["inputTurn"]
    assert projection["_peerMessage"] is True
    assert projection["_fromConv"] == peer_environment["sender"]
    assert projection["_initiator"] == "peer"


def test_live_target_has_durable_row_and_inbox_accelerator(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import get_queue

    with _live_task(peer_environment["target"]):
        result = _send(peer_environment)

    [queued] = get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID)
    [inbox_item] = agent_inbox.drain(peer_environment["target"])
    assert queued["queueId"] == result["queueId"]
    assert inbox_item["queueId"] == result["queueId"]
    assert peer_environment["submitted"] == []


def test_inbox_first_delivery_removes_durable_twin(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import (
        dedup_peer_durable_rows,
        dispatch_next_queued,
        get_queue,
    )

    with _live_task(peer_environment["target"]):
        _send(peer_environment)
        [inbox_item] = agent_inbox.drain(peer_environment["target"])
        assert dedup_peer_durable_rows(
            peer_environment["target"], [inbox_item["queueId"]], user_id=TEST_OWNER_USER_ID) == 1

    assert get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID) == []
    assert dispatch_next_queued(peer_environment["target"], user_id=TEST_OWNER_USER_ID) is None
    assert peer_environment["submitted"] == []


def test_queue_first_delivery_consumes_inbox_twin(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import dispatch_next_queued, get_queue

    with _live_task(peer_environment["target"]):
        _send(peer_environment)
    assert agent_inbox.peek(peer_environment["target"]) == 1

    attempt_id = dispatch_next_queued(peer_environment["target"], user_id=TEST_OWNER_USER_ID)

    assert attempt_id == "peer-attempt-1"
    assert get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID) == []
    assert agent_inbox.peek(peer_environment["target"]) == 0
    projection = peer_environment["submitted"][0]["command"]["inputTurn"]
    assert projection["_peerMessage"] is True


def test_unconfirmed_inbox_injection_keeps_durable_copy(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import dispatch_next_queued, get_queue

    with _live_task(peer_environment["target"]):
        _send(peer_environment, text="do not lose this message")
        assert len(agent_inbox.drain(peer_environment["target"])) == 1
        # The caller has only injected in memory; it has not confirmed model
        # consumption with dedup_peer_durable_rows yet.
        assert len(get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID)) == 1

    assert dispatch_next_queued(peer_environment["target"], user_id=TEST_OWNER_USER_ID) == "peer-attempt-1"
    assert len(peer_environment["submitted"]) == 1


def test_mailbox_only_message_does_not_wake_idle_target(peer_environment):
    from lib import agent_inbox
    from lib.message_queue import drain_idle_peer_messages, get_queue

    result = _send(peer_environment, wake=False)

    assert result["ok"] is True
    assert len(get_queue(peer_environment["target"], user_id=TEST_OWNER_USER_ID)) == 1
    assert agent_inbox.peek(peer_environment["target"]) == 0
    assert drain_idle_peer_messages() == []
    assert peer_environment["submitted"] == []


def test_operator_message_has_distinct_initiator(peer_environment):
    _send(peer_environment, human=True)

    projection = peer_environment["submitted"][0]["command"]["inputTurn"]
    assert projection["_peerHuman"] is True
    assert projection["_initiator"] == "operator"
