"""Every public Turn has one backend-normalized stable block timeline."""

from __future__ import annotations

from copy import deepcopy

import pytest


pytestmark = pytest.mark.unit


def test_legacy_projection_is_derived_once_with_stable_reserved_ids():
    from lib.turn_projection_segments import projection_with_stable_segments

    source = {
        "content": "final",
        "thinking": "terminal thought",
        "toolRounds": [{
            "llmRound": 0,
            "toolCallId": "call-a",
            "toolName": "search",
            "toolArgs": {"q": "x"},
            "assistantContent": "I will search",
            "thinking": "pre-tool thought",
            "toolContent": "hit",
            "status": "done",
        }],
        "images": [{"attachmentId": "image-a"}],
        "modifiedFiles": 1,
        "modifiedFileList": [{"path": "src/a.ts", "action": "modified"}],
        "_inboxInjects": [{"round": 1, "count": 1}],
    }
    before = deepcopy(source)
    normalized = projection_with_stable_segments(source)

    assert source == before
    assert [item["blockId"] for item in normalized["segments"]] == [
        "thinking:llm-0",
        "text:llm-0",
        "tool:call-a",
        "thinking:terminal",
        "text:terminal",
    ]
    assert all("_round" not in item for item in normalized["segments"])
    assert normalized["segments"][-1]["text"] == "final"
    assert normalized["fileChanges"] == {
        "blockId": "file-changes",
        "count": 1,
        "state": "applied",
        "files": [{"path": "src/a.ts", "action": "modified"}],
    }
    assert normalized["_inboxInjects"][0]["blockId"] == (
        "injection:inbox:round-1"
    )


def test_existing_terminal_identity_survives_slim_text_and_sidecar_collision():
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments({
        "content": "new streamed text",
        "segments": [{
            "type": "text",
            "blockId": "attachments",
            "text": "old text",
            "deliverable": True,
            "terminal": True,
        }],
        "images": [{"attachmentId": "image-a"}],
    }, status="running")

    assert normalized["segments"] == [{
        "type": "text",
        "blockId": "attachments~2",
        "text": "new streamed text",
        "deliverable": True,
        "terminal": True,
    }]


def test_empty_live_assistant_gets_one_stable_terminal_placeholder():
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments(
        {"content": "", "thinking": "", "segments": []},
        actor="assistant",
        status="running",
    )
    assert normalized["segments"] == [{
        "type": "text",
        "blockId": "text:terminal",
        "text": "",
        "deliverable": True,
        "terminal": True,
    }]


def test_pending_tool_results_sentinel_is_not_published():
    from lib.conversation_sync.validation import decode
    from lib.turn_projection_segments import projection_with_stable_segments

    source = {
        "content": "",
        "toolRounds": [{
            "roundNum": 1,
            "toolCallId": "call-pending",
            "toolName": "web_search",
            "status": "searching",
            "results": None,
        }],
    }
    normalized = projection_with_stable_segments(
        source, actor="assistant", status="running",
    )

    assert source["toolRounds"][0]["results"] is None
    assert "results" not in normalized["toolRounds"][0]
    assert decode("TurnProjection", normalized) == normalized


def test_live_round_missing_segment_is_repaired_at_the_authority_boundary():
    """ask_human blocks the executor, so no later checkpoint ever mints its
    tool_use segment — the fold must repair rather than publish a stale
    checkpoint-era timeline (the "no place to answer" bug)."""
    from lib.turn_projection_segments import projection_with_stable_segments

    stale_segments = [
        {
            "type": "tool_use",
            "blockId": "tool:call-a",
            "id": "call-a",
            "name": "grep_search",
            "input": {"pattern": "x"},
            "result": {"content": "hit", "status": "done"},
        },
    ]
    normalized = projection_with_stable_segments({
        "content": "",
        "thinking": "",
        "segments": stale_segments,
        "toolRounds": [
            {
                "llmRound": 0,
                "roundNum": 1,
                "toolCallId": "call-a",
                "toolName": "grep_search",
                "toolArgs": {"pattern": "x"},
                "toolContent": "hit",
                "status": "done",
            },
            {
                "llmRound": 1,
                "roundNum": 2,
                "toolCallId": "call-b",
                "toolName": "ask_human",
                "toolArgs": {"question": "Which scope?"},
                "status": "awaiting_human",
                "guidanceId": "hg_test",
                "guidanceQuestion": "Which scope?",
                "guidanceType": "free_text",
            },
        ],
    }, status="running")

    tool_segments = [
        item for item in normalized["segments"] if item["type"] == "tool_use"
    ]
    assert [item["id"] for item in tool_segments] == ["call-a", "call-b"]
    assert tool_segments[1]["blockId"] == "tool:call-b"
    assert tool_segments[1]["result"]["status"] == "awaiting_human"
    assert all("_round" not in item for item in normalized["segments"])
    # The interactive round keeps its live fields on the projection's round
    # list — the frontend card reads them off `block.round`.
    awaiting = normalized["toolRounds"][1]
    assert awaiting["status"] == "awaiting_human"
    assert awaiting["guidanceId"] == "hg_test"


@pytest.mark.parametrize('status', ['running', 'completed'])
def test_reused_call_id_does_not_hide_a_missing_tool_occurrence(status):
    """Coverage is occurrence-based; provider call ids are not unique facts."""
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments({
        'content': '',
        'segments': [{
            'type': 'tool_use', 'blockId': 'tool:reused-id',
            'id': 'reused-id', 'name': 'read_files',
            'input': {'path': 'old.py'},
            'result': {'content': 'old', 'status': 'done'},
        }],
        'toolRounds': [
            {
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'reused-id', 'toolName': 'read_files',
                'toolArgs': {'path': 'old.py'}, 'toolContent': 'old',
                'status': 'done',
            },
            {
                'roundNum': 2, 'llmRound': 1,
                'toolCallId': 'reused-id', 'toolName': 'read_files',
                'toolArgs': {'path': 'new.py'}, 'toolContent': 'new',
                'status': 'done',
            },
        ],
    }, status=status)

    tools = [segment for segment in normalized['segments']
             if segment['type'] == 'tool_use']
    assert [segment['id'] for segment in tools] == [
        'reused-id', 'reused-id',
    ]
    assert [segment['blockId'] for segment in tools] == [
        'tool:reused-id', 'tool:reused-id~2',
    ]
    assert [segment['result']['content'] for segment in tools] == [
        'old', 'new',
    ]


def test_early_live_tool_keeps_streamed_thinking_before_tool_until_settled():
    """An early tool_start arrives before parsing moves the current thinking
    onto its tool round.  The live repair must not apply finished-turn ordering
    and temporarily move that thinking behind the new tool block."""
    from lib.turn_projection_segments import projection_with_stable_segments

    thought = "I'll check the installed version."
    early = projection_with_stable_segments({
        "content": "",
        "thinking": thought,
        "segments": [{
            "type": "thinking",
            "blockId": "thinking:terminal",
            "text": thought,
            "deliverable": False,
            "terminal": True,
        }],
        "toolRounds": [{
            "llmRound": 0,
            "roundNum": 1,
            "toolCallId": "call-version",
            "toolName": "run_command",
            "toolArgs": {"command": "tofu --version"},
            "status": "searching",
        }],
    }, status="running")

    def visible_timeline(projection):
        return [
            (segment["type"], segment["blockId"])
            for segment in projection["segments"]
            if segment["type"] != "text" or segment["text"]
        ]

    assert visible_timeline(early) == [
        ("thinking", "thinking:terminal"),
        ("tool_use", "tool:call-version"),
    ]
    early_tool_position = next(
        index for index, segment in enumerate(early["segments"])
        if segment["blockId"] == "tool:call-version"
    )

    tool_done_source = deepcopy(early)
    tool_done_source["toolRounds"][0].update({
        "toolContent": "Tofu 1.2.3",
        "status": "done",
    })
    tool_done = projection_with_stable_segments(
        tool_done_source, status="running",
    )
    assert visible_timeline(tool_done) == visible_timeline(early)
    assert next(
        index for index, segment in enumerate(tool_done["segments"])
        if segment["blockId"] == "tool:call-version"
    ) == early_tool_position

    settled = projection_with_stable_segments({
        "content": "",
        "thinking": "",
        "toolRounds": [{
            **tool_done_source["toolRounds"][0],
            "thinking": thought,
        }],
    }, status="completed")
    assert visible_timeline(settled) == [
        ("thinking", "thinking:llm-0"),
        ("tool_use", "tool:call-version"),
    ]
    assert next(
        index for index, segment in enumerate(settled["segments"])
        if segment["blockId"] == "tool:call-version"
    ) == early_tool_position


def test_covered_segments_are_not_reassembled():
    """Without a coverage gap the existing timeline is preserved verbatim —
    re-assembly must never churn settled/archived documents."""
    from lib.turn_projection_segments import projection_with_stable_segments

    segments = [
        {
            "type": "text",
            "blockId": "text:custom",
            "text": "bespoke prose assembly would not mint",
            "deliverable": False,
        },
        {
            "type": "tool_use",
            "blockId": "tool:call-a",
            "id": "call-a",
            "name": "grep_search",
            "input": {},
            "result": {"content": "hit", "status": "done"},
        },
    ]
    normalized = projection_with_stable_segments({
        "content": "",
        "thinking": "",
        "segments": segments,
        "toolRounds": [{
            "toolCallId": "call-a",
            "toolName": "grep_search",
            "status": "done",
        }],
    }, status="running")

    assert normalized["segments"][0]["text"] == (
        "bespoke prose assembly would not mint"
    )


@pytest.mark.parametrize(
    "compaction_stamp",
    [
        pytest.param({"compactionLayer": "L1"}, id="l1"),
        pytest.param({"_persistCompacted": True}, id="frame-budget"),
    ],
)
def test_explicit_compaction_replaces_only_the_segment_result_mirror(
    compaction_stamp,
):
    import orjson

    from lib.turn_projection_segments import (
        projection_with_reference_tool_segments,
        projection_with_stable_segments,
    )

    original_result = "original-result:" + ("x" * 160_000)
    compacted_result = "[tool result compacted — originalChars=160016]"
    tool_args = {"path": "docs/README.md"}
    source = {
        "content": "done",
        "toolRounds": [{
            "toolCallId": "call-a",
            "toolName": "read_files",
            "toolArgs": tool_args,
            "toolContent": compacted_result,
            "result": {"artifactId": "artifact-a"},
            "status": "done",
            "attemptId": "attempt-a",
            "taskId": "task-a",
            "llmRound": 3,
            **compaction_stamp,
        }],
        "segments": [
            {
                "type": "text",
                "blockId": "text:intro",
                "text": "I will inspect it.",
                "deliverable": False,
            },
            {
                "type": "tool_use",
                "blockId": "tool:custom-call-a",
                "id": "call-a",
                "name": "read_files",
                "input": tool_args,
                "result": {
                    "content": original_result,
                    "status": "done",
                    "artifactId": "artifact-a",
                },
                "attemptId": "attempt-a",
                "taskId": "task-a",
                "llmRound": 3,
                "translatedText": "已读取",
            },
            {
                "type": "text",
                "blockId": "text:terminal",
                "text": "done",
                "deliverable": True,
                "terminal": True,
            },
        ],
    }
    before = deepcopy(source)

    normalized = projection_with_stable_segments(source)
    tool_segment = normalized["segments"][1]

    assert source == before
    assert [segment["blockId"] for segment in normalized["segments"]] == [
        "text:intro",
        "tool:custom-call-a",
        "text:terminal",
    ]
    assert tool_segment["input"] == tool_args
    assert tool_segment["translatedText"] == "已读取"
    assert tool_segment["result"] == {
        "content": compacted_result,
        "status": "done",
        "artifactId": "artifact-a",
    }
    assert len(orjson.dumps(normalized["segments"])) < (
        len(orjson.dumps(source["segments"])) * 0.02
    )
    referenced = projection_with_reference_tool_segments(
        normalized, status="completed",
    )
    referenced_tool = referenced["segments"][1]
    assert referenced_tool["roundRef"] == "call-a"
    assert referenced_tool["result"] == {}
    assert "input" not in referenced_tool
    assert referenced_tool["translatedText"] == "已读取"


@pytest.mark.parametrize(
    ("round_changes", "segment_changes"),
    [
        pytest.param({"compactionLayer": "L0"}, {}, id="not-compacted"),
        pytest.param({"toolName": "write_file"}, {}, id="tool-name"),
        pytest.param({"attemptId": "attempt-b"}, {}, id="attempt"),
        pytest.param({"taskId": "task-b"}, {}, id="task"),
        pytest.param({"llmRound": 4}, {}, id="llm-round"),
        pytest.param({"toolArgs": {"path": "other"}}, {}, id="input"),
        pytest.param({"toolCallId": ""}, {"id": ""}, id="blank-id"),
    ],
)
def test_compacted_segment_sync_fails_closed_for_incompatible_occurrences(
    round_changes, segment_changes,
):
    from lib.turn_projection_segments import projection_with_stable_segments

    round_record = {
        "toolCallId": "call-a",
        "toolName": "read_files",
        "toolArgs": {"path": "README.md"},
        "toolContent": "[compacted]",
        "status": "done",
        "attemptId": "attempt-a",
        "taskId": "task-a",
        "llmRound": 3,
        "compactionLayer": "L1",
        **round_changes,
    }
    segment = {
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "read_files",
        "input": {"path": "README.md"},
        "result": {"content": "original", "status": "done"},
        "attemptId": "attempt-a",
        "taskId": "task-a",
        "llmRound": 3,
        **segment_changes,
    }

    normalized = projection_with_stable_segments({
        "segments": [segment],
        "toolRounds": [round_record],
    }, status="running")

    tool_segment = next(
        item for item in normalized["segments"]
        if item.get("type") == "tool_use"
    )
    assert tool_segment["result"]["content"] == "original"


@pytest.mark.parametrize("duplicated_carrier", ["round", "segment"])
def test_compacted_segment_sync_fails_closed_for_duplicate_call_ids(
    duplicated_carrier,
):
    from lib.turn_projection_segments import projection_with_stable_segments

    round_record = {
        "toolCallId": "call-a",
        "toolName": "read_files",
        "toolArgs": {},
        "toolContent": "[compacted]",
        "status": "done",
        "compactionLayer": "L1",
    }
    segment = {
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "read_files",
        "input": {},
        "result": {"content": "original", "status": "done"},
    }
    rounds = [round_record]
    segments = [segment, {**segment, "blockId": "tool:call-a~2"}]
    if duplicated_carrier == "round":
        rounds.append({**round_record, "toolContent": "[other compacted]"})

    normalized = projection_with_stable_segments({
        "segments": segments,
        "toolRounds": rounds,
    }, status="running")

    assert [
        item["result"]["content"]
        for item in normalized["segments"]
        if item.get("type") == "tool_use"
    ] == ["original", "original"]


def test_segment_less_synthetic_and_idless_rounds_are_not_gaps():
    """Inbox-inject rows and rounds without a toolCallId never become
    segments — they must not trigger a perpetual re-assembly loop."""
    from lib.turn_projection_segments import projection_with_stable_segments

    segments = [{
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "grep_search",
        "input": {},
        "result": {"content": "hit", "status": "done"},
    }]
    normalized = projection_with_stable_segments({
        "content": "",
        "thinking": "",
        "segments": segments,
        "toolRounds": [
            {"toolCallId": "call-a", "toolName": "grep_search",
             "status": "done"},
            {"roundNum": 9000001, "_inboxInject": True,
             "status": "done"},
            {"roundNum": 3, "toolName": "image_gen",
             "status": "searching"},
        ],
    }, status="running")

    tool_segments = [
        item for item in normalized["segments"] if item["type"] == "tool_use"
    ]
    assert [item["id"] for item in tool_segments] == ["call-a"]

def test_public_normalizer_repairs_nested_turns_only():
    from lib.turn_projection_segments import public_value_with_stable_segments

    document = {
        "turns": [{
            "turnId": "turn-a",
            "projectionRevision": 2,
            "actor": "human",
            "status": "completed",
            "projection": {"content": "hello"},
        }],
        "unrelated": {"projection": {"content": "leave me"}},
    }
    normalized = public_value_with_stable_segments(document)
    assert normalized["turns"][0]["projection"]["segments"][0][
        "blockId"
    ] == "text:terminal"
    assert normalized["unrelated"] == document["unrelated"]


def test_frontend_selector_contains_no_pre_segment_projection_fallback():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "frontend/src/conversation/presentation/conversation-view-model.ts").read_text(
        encoding="utf-8"
    )
    assert "legacyProjectionBlocks" not in source
    assert "legacy-projection" not in source


def test_legacy_visible_metadata_maps_to_typed_projection_sidecars():
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments({
        "role": "user",
        "_turnId": "duplicate-turn-id",
        "_msgId": "duplicate-message-id",
        "content": "brain kickoff",
        "_initiator": "brain",
        "_brainDispatch": True,
        "_brainEpic": {
            "epicId": "epic-a",
            "epicTitle": "Ship the typed surface",
            "originatorConv": "conv-source",
            "originatorTitle": "Architecture",
            "route": "creator",
            "method": "posted",
            "answered": False,
            "uncontracted": "drop",
        },
        "_boardTaskId": "epic-a",
        "_ctx": {"model": "gpt-test", "tools": [{"label": "Code"}]},
        "_translateDone": False,
        "_translateTaskId": "translate-a",
        "_translateModel": "translator-a",
        "unknownMessageOverlay": "drop",
    }, actor="human")

    assert normalized["origin"] == {
        "blockId": "origin",
        "initiator": "brain",
        "brain": {
            "epicId": "epic-a",
            "epicTitle": "Ship the typed surface",
            "originatorConv": "conv-source",
            "originatorTitle": "Architecture",
            "route": "creator",
            "method": "posted",
            "answered": False,
        },
        "boardTaskId": "epic-a",
    }
    assert normalized["contextSnapshot"] == {
        "blockId": "turn-context",
        "snapshot": {"model": "gpt-test", "tools": [{"label": "Code"}]},
    }
    assert normalized["translation"] == {
        "status": "pending",
        "taskId": "translate-a",
        "model": "translator-a",
    }
    assert "role" not in normalized
    assert "_turnId" not in normalized
    assert "_brainEpic" not in normalized
    assert "unknownMessageOverlay" not in normalized


def test_legacy_compaction_maps_to_one_typed_block():
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments({
        "content": "## Context compacted\n\nsummary",
        "_isCompactionSummary": True,
        "_compactionArchiveId": "archive-a",
        "_estimatedPromptTokens": 1200,
        "_compactions": [{
            "convId": "conv-a",
            "trigger": "manual",
            "ts": 42,
            "tokensBefore": 9000,
            "tokensAfter": 1200,
            "msgsBefore": 12,
            "msgsAfter": 3,
            "reductionPct": 87,
        }],
    })

    assert normalized["compaction"] == {
        "blockId": "compaction",
        "archiveId": "archive-a",
        "conversationId": "conv-a",
        "trigger": "manual",
        "timestamp": 42,
        "tokensBefore": 9000,
        "tokensAfter": 1200,
        "messagesBefore": 12,
        "messagesAfter": 3,
        "reductionPercent": 87,
        "estimatedPromptTokens": 1200,
    }


def test_legacy_image_generation_maps_to_one_typed_block():
    from lib.turn_projection_segments import projection_with_stable_segments

    normalized = projection_with_stable_segments({
        "content": "generated",
        "_isImageGen": True,
        "_igResults": [{
            "ok": True,
            "prompt": "lighthouse",
            "model": "image-model",
            "provider_id": "provider-a",
            "aspect_ratio": "16:9",
            "resolution": "2K",
            "image_url": "/generated/a.png",
            "remote_image_url": "https://images.example/a.png",
            "file_size": 2048,
            "elapsed": "2.5",
            "response_text": "at dusk",
        }],
    })

    assert normalized["imageGeneration"] == {
        "blockId": "image-generation",
        "mode": "batch",
        "status": "completed",
        "results": [{
            "ok": True,
            "prompt": "lighthouse",
            "model": "image-model",
            "providerId": "provider-a",
            "aspectRatio": "16:9",
            "resolution": "2K",
            "imageUrl": "/generated/a.png",
            "remoteImageUrl": "https://images.example/a.png",
            "fileSize": 2048,
            "elapsedSeconds": 2.5,
            "responseText": "at dusk",
        }],
    }
    assert normalized["segments"][-1]["blockId"] == "text:terminal"


def test_generated_contract_rejects_unknown_projection_fields():
    from lib.conversation_sync.validation import decode

    with pytest.raises(ValueError, match="unknownMessageOverlay"):
        decode("TurnProjection", {
            "content": "hello",
            "segments": [{
                "type": "text",
                "blockId": "text:terminal",
                "text": "hello",
            }],
            "unknownMessageOverlay": True,
        })


def test_completed_tool_segments_reference_unique_rounds_without_mutation():
    import orjson

    from lib.turn_projection_segments import (
        projection_with_reference_tool_segments,
    )

    large_args = {"prompt": "a" * 12_000}
    large_result = "r" * 48_000
    private_responses = [{
        "type": "reasoning",
        "encrypted_content": "opaque" * 6_000,
    }]
    private_anthropic = [{
        "type": "thinking",
        "thinking": "private" * 1_000,
    }]
    quota = {"primary": {"remaining_percent": 42}}
    cache_break = {"reason": "prefix_changed"}
    source = {
        "content": "done",
        "toolRounds": [{
            "toolCallId": "call-a",
            "toolName": "research",
            "toolArgs": large_args,
            "toolContent": large_result,
            "status": "done",
            "_responsesItems": private_responses,
            "_anthropicContentBlocks": private_anthropic,
        }],
        "segments": [{
            "type": "tool_use",
            "blockId": "tool:call-a",
            "id": "call-a",
            "name": "research",
            "input": large_args,
            "result": {"content": large_result, "status": "done"},
        }],
        "apiRounds": [{
            "round": 1,
            "tag": "R1",
            "cacheBreak": cache_break,
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 200,
                "cache_read_tokens": 800,
                "cache_write_tokens": 0,
                "total_tokens": 1_200,
                "trace_id": "trace-a",
                "_dispatch": {
                    "key": "slot-a",
                    "key_tail": "tail",
                    "model": "model-a",
                    "provider_id": "provider-a",
                    "latency_ms": 8_000,
                    "stream_started_at_unix_ns": 123,
                },
                "_subscription_quota": quota,
                "_codex_cache": {"evidence": "cache" * 4_000},
                "_network_route": {"routeId": "private-route"},
                "_transport_bytes_received": 123_456,
            },
            "cost": {
                "costCny": 1.25,
                "pricingSource": "model_table",
                "pricingSnapshot": {"evidence": "pricing" * 2_000},
            },
        }],
    }
    before = deepcopy(source)

    referenced = projection_with_reference_tool_segments(
        source,
        status="completed",
    )

    assert source == before
    assert referenced is not source
    assert referenced["toolRounds"][0]["toolContent"] is large_result
    assert "_responsesItems" not in referenced["toolRounds"][0]
    assert "_anthropicContentBlocks" not in referenced["toolRounds"][0]
    assert source["toolRounds"][0]["_responsesItems"] is private_responses
    assert source["toolRounds"][0][
        "_anthropicContentBlocks"
    ] is private_anthropic
    assert referenced["segments"] == [{
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "research",
        "result": {},
        "roundRef": "call-a",
    }]
    browser_api_round = referenced["apiRounds"][0]
    assert browser_api_round["cacheBreak"] is cache_break
    assert browser_api_round["usage"] == {
        "prompt_tokens": 1_000,
        "completion_tokens": 200,
        "cache_read_tokens": 800,
        "cache_write_tokens": 0,
        "total_tokens": 1_200,
        "trace_id": "trace-a",
        "_dispatch": {
            "key": "slot-a",
            "key_tail": "tail",
            "model": "model-a",
            "provider_id": "provider-a",
        },
        "_subscription_quota": quota,
    }
    assert browser_api_round["usage"]["_subscription_quota"] is quota
    assert browser_api_round["cost"] == {
        "costCny": 1.25,
    }
    assert "_codex_cache" in source["apiRounds"][0]["usage"]
    assert "pricingSnapshot" in source["apiRounds"][0]["cost"]
    full_bytes = len(orjson.dumps(source))
    reference_bytes = len(orjson.dumps(referenced))
    assert full_bytes - reference_bytes > 100_000
    assert reference_bytes < full_bytes * 0.40
    assert len(orjson.dumps(referenced["apiRounds"])) < (
        len(orjson.dumps(source["apiRounds"])) * 0.25
    )


def test_reference_browser_omits_round_cost_without_cny_authority():
    from lib.turn_projection_segments import (
        projection_with_reference_tool_segments,
    )

    source = {
        "apiRounds": [{
            "round": 1,
            "cost": {
                "costUsd": 0.25,
                "pricingSource": "model_table",
            },
        }],
    }

    referenced = projection_with_reference_tool_segments(
        source,
        status="completed",
    )

    assert "cost" not in referenced["apiRounds"][0]
    assert source["apiRounds"][0]["cost"] == {
        "costUsd": 0.25,
        "pricingSource": "model_table",
    }


def test_tool_segment_references_fail_closed_to_full_payloads():
    from lib.turn_projection_segments import (
        projection_with_reference_tool_segments,
    )

    segment = {
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "search",
        "input": {"q": "safe"},
        "result": {"content": "hit", "status": "done"},
    }
    projection = {
        "segments": [segment],
        "toolRounds": [
            {"toolCallId": "call-a", "toolName": "search"},
            {"toolCallId": "call-a", "toolName": "search"},
        ],
    }

    assert projection_with_reference_tool_segments(
        projection, status="running",
    ) is projection
    assert projection_with_reference_tool_segments(
        projection, status="completed",
    ) is projection

    mismatched = {
        "segments": [segment],
        "toolRounds": [{"toolCallId": "call-a", "toolName": "write"}],
    }
    assert projection_with_reference_tool_segments(
        mismatched, status="completed",
    ) is mismatched


def test_refs_snapshot_uses_durable_uploads_and_lazily_externalizes_inline_images():
    import base64
    import orjson

    from lib.turn_projection_segments import (
        snapshot_with_reference_tool_segments,
    )
    from lib.conversation_sync.turn_images import turn_image_owner_scope

    raw = b"\x89PNG\r\n\x1a\n" + (b"legacy-image" * 100)
    encoded = base64.b64encode(raw).decode("ascii")
    source = {
        "conversationId": "conv image",
        "turns": [
            {
                "turnId": "turn image",
                "status": "completed",
                "projectionRevision": 7,
                "projection": {
                    "content": "done",
                    "images": [{
                        "base64": encoded,
                        "preview": f"data:image/png;base64,{encoded}",
                        "mediaType": "image/png",
                        "sizeKB": 2,
                        "url": "/api/images/legacy.png",
                    }],
                },
            },
            {
                "turnId": "turn-running",
                "status": "running",
                "projectionRevision": 3,
                "projection": {
                    "images": [{"base64": encoded, "mediaType": "image/png"}],
                },
            },
            {
                "turnId": "turn-small",
                "status": "completed",
                "projectionRevision": 2,
                "projection": {
                    "images": [{
                        "base64": base64.b64encode(
                            b"\x89PNG\r\n\x1a\nsmall"
                        ).decode("ascii"),
                        "mediaType": "image/png",
                    }],
                },
            },
            {
                "turnId": "turn-inline-only",
                "status": "completed",
                "projectionRevision": 8,
                "projection": {
                    "images": [{
                        "base64": encoded,
                        "mediaType": "image/png",
                    }],
                },
            },
            {
                "turnId": "turn-url-only",
                "status": "completed",
                "projectionRevision": 9,
                "projection": {
                    "images": [{
                        "preview": (
                            "/api/v3/conversations/conv%20image/turns/"
                            "turn-url-only/images/0?projectionRevision=1"
                            "&ownerScope=stale"
                        ),
                        "mediaType": "image/jpeg",
                        "url": (
                            "/proxy/15000/api/images/"
                            "already-uploaded.jpg"
                        ),
                    }],
                },
            },
        ],
    }
    original = deepcopy(source)

    owner_scope = turn_image_owner_scope(9, "conv image")
    referenced = snapshot_with_reference_tool_segments(
        source, owner_cache_scope=owner_scope,
    )

    lazy_image = referenced["turns"][0]["projection"]["images"][0]
    assert "base64" not in lazy_image
    assert lazy_image["preview"] == "/api/images/legacy.png"
    assert lazy_image["mediaType"] == "image/png"
    assert lazy_image["url"] == "/api/images/legacy.png"
    assert referenced["turns"][1]["projection"]["images"][0][
        "base64"
    ] == encoded
    assert "base64" in referenced["turns"][2]["projection"]["images"][0]

    inline_image = referenced["turns"][3]["projection"]["images"][0]
    assert "base64" not in inline_image
    assert inline_image["preview"] == (
        "/api/v3/conversations/conv%20image/turns/turn-inline-only/images/0"
        f"?projectionRevision=8&ownerScope={owner_scope}"
    )

    uploaded_image = referenced["turns"][4]["projection"]["images"][0]
    assert uploaded_image["url"] == "/api/images/already-uploaded.jpg"
    assert uploaded_image["preview"] == "/api/images/already-uploaded.jpg"
    assert len(orjson.dumps(referenced)) < len(orjson.dumps(source)) * 0.55
    assert source == original
