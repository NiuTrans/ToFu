"""Display-only inbox annotations never alter provider message replay."""

from __future__ import annotations

import json

import pytest

from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
)
from lib.tasks_pkg.conv_message_builder._transform import (
    _build_assistant_messages,
)
from lib.turn_lifecycle import _task_projection


pytestmark = pytest.mark.unit
ANNOTATION_FIELDS = ("_inboxInjects", "_peerInjects", "_userSteerInjects")


def _message() -> dict:
    return {
        "role": "assistant",
        "content": "done",
        "toolRounds": [
            {
                "roundNum": 1,
                "llmRound": 0,
                "toolCallId": "call-1",
                "toolName": "read_file",
                "toolArgs": '{"path":"a.py"}',
                "toolContent": "content",
                "status": "done",
            }
        ],
    }


def test_annotations_are_wire_neutral_and_real_rounds_remain_structured():
    plain = _message()
    annotated = _message()
    annotated.update({
        "_inboxInjects": [{"round": 1, "previews": [{"text": "agent update"}]}],
        "_peerInjects": [{"round": 1, "previews": [{"text": "peer note"}]}],
        "_userSteerInjects": [{"round": 1, "previews": [{"text": "focus"}]}],
    })

    assert _build_assistant_messages(annotated) == _build_assistant_messages(plain)
    structured = _reconstruct_tool_call_messages(annotated["toolRounds"])
    assert structured is not None
    assert [row["tool_call_id"] for row in structured if row["role"] == "tool"] == [
        "call-1"
    ]
    encoded = json.dumps(_build_assistant_messages(annotated))
    assert all(marker not in encoded for marker in ("agent update", "peer note", "focus"))


def test_annotation_fields_are_declared_once_in_the_turn_projection_contract():
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS

    properties = OPENAPI_SCHEMAS["TurnProjection"]["properties"]
    assert all(field in properties for field in ANNOTATION_FIELDS)


def test_projection_assigns_stable_injection_block_ids_without_mutating_task():
    records = [
        {"round": 2, "count": 1},
        {"round": 2, "count": 2},
        {"round": 3, "blockId": "producer-id"},
    ]
    task = {"_userSteerInjects": records}

    first = _task_projection(task, {})["_userSteerInjects"]
    second = _task_projection({}, {"_userSteerInjects": first})[
        "_userSteerInjects"
    ]

    assert [record["blockId"] for record in first] == [
        "injection:user-steer:round-2",
        "injection:user-steer:round-2~2",
        "producer-id",
    ]
    assert second == first
    assert all("blockId" not in record for record in records[:2])
    assert records[2]["blockId"] == "producer-id"
