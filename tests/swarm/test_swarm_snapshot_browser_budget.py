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
                "callId": f"occurrence-{index}",
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
    assert calls[-1]["callId"] == "occurrence-39"
    assert calls[-1]["preview"] == "🧪" * 2_000
    assert calls[-1]["previewFullChars"] == 5_000
    assert calls[-1]["previewTruncated"] is True
    assert all("backendEvidence" not in call for call in calls)
    assert len(orjson.dumps(projected)) < len(orjson.dumps(source)) * 0.20


def _wide_snapshot(*, settled: bool, agents: int = 13) -> dict:
    return {
        "agents": [{
            "id": f"agent-{agent}",
            "status": "done",
            "preview": f"final answer {agent}",
            "toolCalls": [{
                "toolName": "read_files",
                "callId": f"occurrence-{agent}-{index}",
                "argsBrief": f"file-{index}.py",
                "status": "done",
                "preview": "x" * 1_500,
                "error": "",
            } for index in range(30)],
        } for agent in range(agents)],
        "settled": settled,
        "version": (100000 if settled else 0) + agents,
    }


def _timeline_bytes(calls) -> int:
    return len(json.dumps(calls, ensure_ascii=True, separators=(",", ":")))


def test_settled_wide_swarm_shares_one_timeline_budget_across_agents():
    from lib.swarm.presentation_budget import (
        SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES,
        SWARM_TOOL_TIMELINE_JSON_BYTES,
        swarm_snapshot_for_browser,
    )

    source = _wide_snapshot(settled=True)
    before = deepcopy(source)
    projected = swarm_snapshot_for_browser(source)

    assert source == before
    per_agent = SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES // 13
    timelines = [agent["toolCalls"] for agent in projected["agents"]]
    assert all(_timeline_bytes(calls) <= per_agent for calls in timelines)
    assert sum(map(_timeline_bytes, timelines)) <= SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES
    for index, (agent, calls) in enumerate(zip(projected["agents"], timelines)):
        # Final answers stay complete; recent evidence wins and every elision
        # is explicit.
        assert agent["preview"] == before["agents"][index]["preview"]
        assert calls[-1]["preview"] == "x" * 1_500
        assert calls[-1]["callId"] == f"occurrence-{index}-29"
        stripped = [call for call in calls if call["preview"] == ""]
        assert stripped and all(
            call["previewTruncated"] is True and call["previewFullChars"] == 1_500
            for call in stripped
        )
        assert len(calls) + agent.get("toolCallsOmitted", 0) == 30

    # A narrow settled swarm keeps the per-agent ceiling, and an unsettled
    # one can still receive durable progress, so it is never re-budgeted.
    narrow = swarm_snapshot_for_browser(_wide_snapshot(settled=True, agents=2))
    unsettled = swarm_snapshot_for_browser(_wide_snapshot(settled=False))
    for snapshot in (narrow, unsettled):
        for agent in snapshot["agents"]:
            assert _timeline_bytes(agent["toolCalls"]) <= SWARM_TOOL_TIMELINE_JSON_BYTES
            assert _timeline_bytes(agent["toolCalls"]) > per_agent


def _oracle_fit(calls, omitted, budget):
    """The documented elision order, re-encoding the whole list at every step."""
    for call in calls:
        for field in ("preview", "error", "argsBrief"):
            if _timeline_bytes(calls) <= budget:
                return calls, omitted
            text = call.get(field)
            if text:
                full_key = f"{field}FullChars"
                call[full_key] = max(call.get(full_key) or 0, len(text))
                call[f"{field}Truncated"] = True
                call[field] = ""
    while calls and _timeline_bytes(calls) > budget:
        calls.pop(0)
        omitted += 1
    return calls, omitted


def test_timeline_fitting_matches_the_naive_sequential_oracle(monkeypatch):
    """Both fitting paths return exactly what re-encoding at every step returns.

    Short arguments make a strip grow the list, so sizes are not monotonic;
    budgets near the fully stripped size exercise the bulk path's fallback.
    """
    import random

    from lib.swarm import presentation_budget as budget_module

    fully_stripped = budget_module._fully_stripped
    paths = []
    for name in ("_fully_stripped", "_strip_detail"):
        original = getattr(budget_module, name)

        def counted(*args, _name=name, _original=original):
            paths[-1].add(_name)
            return _original(*args)

        monkeypatch.setattr(budget_module, name, counted)
    rng = random.Random("swarm-timeline-oracle")
    texts = ("", "x", "ok", "工具" * 40, "🧪" * 700, "line\n\"q\"\\" * 90, "a" * 2_500)
    for _ in range(400):
        rows = []
        for index in range(rng.randint(0, 30)):
            source = {
                "toolName": rng.choice(("read_files", "run_command", "名" * 9)),
                "argsBrief": rng.choice(texts[:4]),
                "status": rng.choice(("done", "failed")),
                "preview": rng.choice(texts),
                "error": rng.choice(("", "", "boom" * rng.randint(1, 300))),
                "callId": f"call-{index}",
            }
            if rng.random() < 0.3:
                source["previewFullChars"] = rng.randint(0, 9_000)
            if rng.random() < 0.3:
                source["elapsed"] = rng.random() * 100
            if rng.random() < 0.2:
                source["inspectorRef"] = {"taskId": "task", "turn": "t", "roundNum": 2}
            rows.append(budget_module._bounded_browser_tool_call(source))
        omitted = rng.randint(0, 3)
        stripped_size = _timeline_bytes(fully_stripped(deepcopy(rows))[0])
        budget = rng.choice((
            64, 512, 4_096, 9_000, 32_768,
            max(2, stripped_size + rng.randint(-120, 120)),
        ))
        paths.append(set())
        expected = _oracle_fit(deepcopy(rows), omitted, budget)
        assert budget_module._fit_timeline_byte_budget(
            deepcopy(rows), omitted, budget,
        ) == expected
    exercised = {frozenset(path) for path in paths}
    assert frozenset({"_fully_stripped"}) in exercised
    assert frozenset({"_fully_stripped", "_strip_detail"}) in exercised
    assert frozenset({"_strip_detail"}) in exercised


def test_current_bounded_swarm_snapshot_keeps_object_identity():
    from lib.swarm.presentation_budget import swarm_snapshot_for_browser

    snapshot = {
        "agents": [{
            "id": "agent-a",
            "status": "done",
            "toolCalls": [{
                "toolName": "read_files",
                "callId": "occurrence-current",
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
    # Terminal views omit server-only replay; Continue reads it from storage.
    assert "_responsesItems" not in interrupted_round
    assert projection["toolRounds"][0]["_responsesItems"] is private_replay
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
