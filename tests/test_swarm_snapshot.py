"""Pure contracts for durable Swarm projection data.

The integration write to Conversation Sync is covered by
test_swarm_snapshot_turn_native.py. This module keeps only projection
ordering, scoping, and model-shaping rules; it owns no database fixture.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_snapshot_versions_never_regress_a_settled_round():
    from lib.swarm.snapshot import stamp_round

    round_projection = {"toolName": "spawn_agents", "_swarm": True}
    settled = {
        "agents": [{"id": "a", "status": "done"}],
        "settled": True,
        "version": 100001,
    }
    late_partial = {
        "agents": [{"id": "a", "status": "running"}],
        "settled": False,
        "version": 0,
    }

    assert stamp_round(round_projection, settled) is True
    assert stamp_round(round_projection, late_partial) is False
    assert round_projection["_swarmSnapshot"] == settled


def test_filter_snapshot_scopes_counts_and_agents_to_one_wave():
    from lib.swarm.snapshot import filter_snapshot

    combined = {
        "agents": [
            {"id": "w1a", "status": "done", "tokens": 10},
            {"id": "w1b", "status": "done", "tokens": 20},
            {"id": "w2a", "status": "running", "tokens": 0},
        ],
        "settled": True,
        "version": 100002,
    }

    wave = filter_snapshot(combined, {"w1a", "w1b"})
    assert {agent["id"] for agent in wave["agents"]} == {"w1a", "w1b"}
    assert wave["doneCount"] == 2
    assert wave["totalTokens"] == 30
    assert wave["version"] == 100002


def test_aborted_swarm_never_projects_running_agents_as_settled():
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec

    master = MasterOrchestrator(
        task_id="t-abort",
        conv_id="cv-abort",
        user_id=1,
        specs=[SubTaskSpec(id="a", role="general", objective="work")],
    )
    master._aborted = True
    master._terminated = True

    snapshot = master._build_agent_snapshot()
    assert snapshot["settled"] is True
    assert snapshot["agents"][0]["status"] == "aborted"


def test_snapshot_tool_timeline_is_deduplicated_and_bounded():
    from lib.swarm.master import (
        _SNAPSHOT_TOOLCALLS_CAP,
        _snapshot_tool_timeline,
    )

    log = [
        {"round": 1, "tool": "read_files", "args_brief": "a.py"},
        {"round": 1, "tool": "grep_search", "args_brief": "needle"},
        {"round": 2, "tool": "read_files", "args_brief": "b.py"},
        {"round": 2, "tool": None},
        "invalid",
    ]
    tools, calls = _snapshot_tool_timeline(log)
    assert tools == ["read_files", "grep_search"]
    assert [call["argsBrief"] for call in calls] == [
        "a.py",
        "needle",
        "b.py",
    ]

    oversized = [
        {"round": index, "tool": "read_files", "args_brief": str(index)}
        for index in range(_SNAPSHOT_TOOLCALLS_CAP + 3)
    ]
    _, bounded = _snapshot_tool_timeline(oversized)
    assert len(bounded) == _SNAPSHOT_TOOLCALLS_CAP
    assert bounded[-1]["argsBrief"] == str(_SNAPSHOT_TOOLCALLS_CAP + 2)


def test_snapshot_writes_are_throttled_but_settlement_is_forced(monkeypatch):
    import lib.swarm.master as master_module
    import lib.swarm.snapshot as snapshot_module
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec

    master = MasterOrchestrator(
        task_id="t-throttle",
        conv_id="cv-throttle",
        user_id=1,
        specs=[SubTaskSpec(id="a", role="general", objective="work")],
    )
    calls = []
    monkeypatch.setattr(master_module, "_SNAPSHOT_CAS_MIN_INTERVAL_S", 1000.0)
    monkeypatch.setattr(
        snapshot_module,
        "persist_snapshot_to_conversation",
        lambda conv_id, agent_ids, snapshot, *, user_id: calls.append(
            (conv_id, tuple(agent_ids), user_id)
        )
        or True,
    )

    master._persist_agent_snapshot()
    master._persist_agent_snapshot()
    master._persist_agent_snapshot(force=True)

    assert calls == [
        ("cv-throttle", ("a",), 1),
        ("cv-throttle", ("a",), 1),
    ]
