"""Stable source-message identity crosses the queue-to-turn boundary."""

from __future__ import annotations

import time

import pytest

import lib.message_queue as queue
from tests._seed import delete_conversation, seed_conversation


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit
USER_ID = 1


@pytest.fixture
def conversation_id(chat_sidecar):
    value = f"queue-message-id-{time.time_ns()}"
    seed_conversation(value, user_id=USER_ID, title='Message identity test')
    yield value
    delete_conversation(value, user_id=USER_ID)


def _capture_submitted_input(monkeypatch, conversation_id: str) -> dict:
    captured: list[dict] = []

    def submit(conv_id, user_id, command):
        assert conv_id == conversation_id
        captured.append(command["inputTurn"])
        return {"attempt": {"attemptId": "attempt-accepted"}}

    monkeypatch.setattr(queue, "_submit_queued_turn_command", submit)
    monkeypatch.setattr(
        queue, '_conv_has_live_task',
        lambda conv_id, *, user_id: False,
    )
    assert queue.dispatch_next_queued(
        conversation_id, user_id=USER_ID) == "attempt-accepted"
    assert len(captured) == 1
    return captured[0]


def test_prebuilt_user_projection_preserves_source_message_id(
    monkeypatch, conversation_id,
):
    user_projection = {
        "role": "user",
        "content": "hello",
        "timestamp": 2000,
        "_msgId": "client-message-prebuilt",
    }
    queue.enqueue_message(
        conversation_id,
        {
            "text": "hello",
            "timestamp": 2000,
            "_msgId": "client-message-prebuilt",
            "_user_msg": user_projection,
        },
        {},
        user_id=USER_ID,
    )

    submitted = _capture_submitted_input(monkeypatch, conversation_id)
    assert submitted["_msgId"] == "client-message-prebuilt"
    assert submitted["content"] == "hello"


def test_engine_built_projection_preserves_source_message_id(
    monkeypatch, conversation_id,
):
    queue.enqueue_message(
        conversation_id,
        {
            "text": "workflow input",
            "timestamp": 3000,
            "_msgId": "engine-source-message",
        },
        {},
        kind=queue.KIND_WORKFLOW,
        user_id=USER_ID,
    )

    submitted = _capture_submitted_input(monkeypatch, conversation_id)
    assert submitted["_msgId"] == "engine-source-message"
    assert submitted["content"] == "workflow input"
