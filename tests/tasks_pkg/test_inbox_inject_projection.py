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


def test_delivered_steer_revises_the_pending_block_not_a_second_card():
    from lib.turn_lifecycle import _merge_user_steer_records

    first = {'blockId': 'injection:user-steer:one', 'commandId': 'one',
             'round': 1, 'count': 1, 'deliveryState': 'pending',
             'previews': [{'text': 'same words'}], 'acceptedAtMs': 42}
    second = {'blockId': 'injection:user-steer:two', 'commandId': 'two',
              'round': 1, 'count': 1, 'deliveryState': 'pending',
              'previews': [{'text': 'same words'}]}
    confirmed = {'blockId': first['blockId'], 'commandId': 'one',
                 'round': 2, 'count': 1, 'deliveryState': 'delivered',
                 'previews': [{'text': 'same words'}]}
    previous = {'_userSteerInjects': [first, second]}
    projected = _task_projection({'_userSteerInjects': [confirmed]}, previous)
    lane = projected['_userSteerInjects']
    assert len(lane) == 2  # Same text, different accepted messages.
    assert [item['blockId'] for item in lane] == [first['blockId'], second['blockId']]
    assert lane[0]['deliveryState'] == 'delivered'
    assert lane[0]['acceptedAtMs'] == 42
    assert lane[1]['deliveryState'] == 'pending'
    assert _task_projection({'_userSteerInjects': [confirmed]}, projected)[
        '_userSteerInjects'] == lane
    assert _merge_user_steer_records(lane, [first]) == lane  # stale replay


def test_undelivered_steer_queued_for_next_turn_supersedes_pending_receipt():
    previous = {'_userSteerInjects': [
        {'blockId': 'injection:user-steer:one', 'commandId': 'one',
         'deliveryState': 'pending', 'previews': [{'text': 'same words'}]},
        {'blockId': 'injection:user-steer:two', 'commandId': 'two',
         'deliveryState': 'delivered', 'previews': [{'text': 'same words'}]},
    ]}
    queued = {'blockId': 'injection:user-steer:one',
              'deliveryState': 'queued', 'queueId': 'queue-next'}
    projected = _task_projection({'_userSteerInjects': [queued]}, previous)
    lane = projected['_userSteerInjects']
    assert len(lane) == 2
    assert lane[0]['deliveryState'] == 'queued'
    assert lane[0]['queueId'] == 'queue-next'
    assert lane[0]['previews'] == [{'text': 'same words'}]
    assert lane[1]['deliveryState'] == 'delivered'
    stale = _task_projection({'_userSteerInjects': [previous['_userSteerInjects'][0]]},
                             projected)
    assert stale['_userSteerInjects'] == lane


def test_legacy_unidentified_steers_keep_distinct_occurrences():
    from lib.turn_lifecycle import _merge_user_steer_records

    old = [{'round': 1, 'previews': [{'text': 'repeat'}]}]
    new = [{'round': 1, 'previews': [{'text': 'repeat'}]}]
    result = _merge_user_steer_records(old, new)
    assert len(result) == 2
