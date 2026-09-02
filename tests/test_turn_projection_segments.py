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
