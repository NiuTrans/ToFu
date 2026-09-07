"""Request-local browser budgets for historical durable Swarm snapshots."""

from __future__ import annotations

from copy import deepcopy
import json

import orjson
import pytest


pytestmark = pytest.mark.unit


def _legacy_snapshot(call_count: int = 40) -> dict:
    return {
        "agents": [{
            "id": "agent-a",
            "role": "researcher",
            "objective": "collect evidence",
            "status": "done",
            "preview": "full final answer",
            "toolCalls": [{
                "toolName": "fetch_url",
                "argsBrief": f"source-{index}",
                "status": "done",
                "preview": "🧪" * 5_000,
                "error": "",
                "backendEvidence": "not a browser field",
            } for index in range(call_count)],
        }],
        "settled": True,
        "version": 100001,
    }


def test_historical_swarm_timeline_is_bounded_honest_and_non_mutating():
    from lib.swarm.presentation_budget import (
        SWARM_TOOL_TIMELINE_JSON_BYTES,
        SWARM_TOOL_TIMELINE_ROW_LIMIT,
        swarm_snapshot_for_browser,
    )

    source = _legacy_snapshot()
    before = deepcopy(source)
    projected = swarm_snapshot_for_browser(source)

    assert source == before
    assert projected is not source
    assert projected["agents"][0]["preview"] == "full final answer"
    calls = projected["agents"][0]["toolCalls"]
    assert len(calls) <= SWARM_TOOL_TIMELINE_ROW_LIMIT
    assert len(json.dumps(
        calls,
        ensure_ascii=True,
        separators=(",", ":"),
    )) <= SWARM_TOOL_TIMELINE_JSON_BYTES
    assert projected["agents"][0]["toolCallsOmitted"] >= 10
    assert calls[-1]["argsBrief"] == "source-39"
    assert calls[-1]["preview"] == "🧪" * 2_000
    assert calls[-1]["previewFullChars"] == 5_000
    assert calls[-1]["previewTruncated"] is True
    assert all("backendEvidence" not in call for call in calls)
    assert len(orjson.dumps(projected)) < len(orjson.dumps(source)) * 0.20


def test_current_bounded_swarm_snapshot_keeps_object_identity():
    from lib.swarm.presentation_budget import swarm_snapshot_for_browser

    snapshot = {
        "agents": [{
            "id": "agent-a",
            "status": "done",
            "toolCalls": [{
                "toolName": "read_files",
                "argsBrief": "a.py",
                "status": "done",
                "preview": "ok",
                "previewFullChars": 2,
                "previewTruncated": False,
                "error": "",
            }],
        }],
        "settled": True,
        "version": 100001,
    }

    assert swarm_snapshot_for_browser(snapshot) is snapshot


def test_terminal_reference_view_bounds_swarm_without_touching_recovery_fields():
    from lib.turn_projection_segments import (
        projection_with_reference_tool_segments,
    )

    snapshot = _legacy_snapshot()
    private_replay = [{"type": "reasoning", "encrypted_content": "opaque"}]
    segment = {
        "type": "tool_use",
        "blockId": "tool:spawn-a",
        "id": "spawn-a",
        "name": "spawn_agents",
        "input": {"tasks": []},
        "result": {"content": "spawned", "status": "done"},
    }
    projection = {
        "content": "interrupted",
        "toolRounds": [{
            "toolCallId": "spawn-a",
            "toolName": "spawn_agents",
            "_swarm": True,
            "_swarmSnapshot": snapshot,
            "_responsesItems": private_replay,
            "status": "done",
            "toolArgs": {"tasks": []},
            "toolContent": "spawned",
            "_partialOutput": "transient live output" * 1_000,
            "_partialOutputTotalChars": 21_000,
        }],
        "segments": [segment],
    }
    before = deepcopy(projection)

    interrupted = projection_with_reference_tool_segments(
        projection,
        status="interrupted",
    )
    assert projection == before
    assert interrupted is not projection
    assert interrupted["segments"] is not projection["segments"]
    assert interrupted["segments"][0]["roundRef"] == "spawn-a"
    assert "input" not in interrupted["segments"][0]
    interrupted_round = interrupted["toolRounds"][0]
    assert interrupted_round["_responsesItems"] is private_replay
    assert "_partialOutput" not in interrupted_round
    assert "_partialOutputTotalChars" not in interrupted_round
    assert interrupted_round["_swarmSnapshot"] is not snapshot
    assert interrupted_round["_swarmSnapshot"]["agents"][0][
        "toolCallsOmitted"
    ] >= 10

    assert projection_with_reference_tool_segments(
        projection,
        status="running",
    ) is projection
    assert "_partialOutput" in projection["toolRounds"][0]

    completed = projection_with_reference_tool_segments(
        projection,
        status="completed",
    )
    assert "_responsesItems" not in completed["toolRounds"][0]
    assert completed["segments"][0]["roundRef"] == "spawn-a"

    mismatched = deepcopy(projection)
    mismatched["toolRounds"][0]["toolArgs"] = {"tasks": ["different"]}
    fail_closed = projection_with_reference_tool_segments(
        mismatched,
        status="interrupted",
    )
    assert "roundRef" not in fail_closed["segments"][0]
    assert fail_closed["segments"][0]["input"] == {"tasks": []}
