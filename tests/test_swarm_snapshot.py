"""Pure contracts for durable Swarm projection data.

The integration write to Conversation Sync is covered by
test_swarm_snapshot_turn_native.py. This module keeps only projection
ordering, scoping, and model-shaping rules; it owns no database fixture.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_sparse_summary_items_projection_unwraps_to_the_spawn_handle():
    """Persisted toolContent is the SPARSE model projection, not the V2
    envelope: _model_projection (lib/tools/result_envelope.py) intentionally
    drops contractVersion from the {summary, items} kind. Readers gated on
    the marker recovered zero agent ids, so no spawn round matched and the
    reloaded panel rendered 子智能体明细未被持久化 even though the handle
    was on disk (conv mtgvz7gyrf3pg2)."""
    import json

    from lib.swarm.snapshot import _round_handle_ids

    handle = {
        "agents": [{"id": "fdca8160", "role": "analyst", "objective": "x"}],
        "status": "async_launched",
        "swarm_id": "75fbdf9a",
    }
    sparse = json.dumps({
        "items": [handle],
        "summary": "Launched 1 agent(s) in the background.",
    })
    assert _round_handle_ids({
        "toolName": "spawn_agents", "toolContent": sparse}) == {"fdca8160"}

    # The full V2 envelope and the bare handle keep matching.
    v2 = json.dumps({
        "contractVersion": "tofu.tool-result/v2",
        "status": "ok",
        "items": [handle],
        "summary": "Launched 1 agent(s).",
    })
    assert _round_handle_ids({
        "toolName": "spawn_agents", "toolContent": v2}) == {"fdca8160"}
    assert _round_handle_ids({
        "toolName": "spawn_agents",
        "toolContent": json.dumps(handle)}) == {"fdca8160"}

    # Negative controls: a foreign envelope contract and a bare payload with
    # an unrelated items field are NOT unwrapped.
    assert _round_handle_ids({
        "toolName": "spawn_agents",
        "toolContent": json.dumps({
            "contractVersion": "other/v1", "items": [handle]})}) == set()
    assert _round_handle_ids({
        "toolName": "spawn_agents",
        "toolContent": json.dumps({"items": ["a", "b"]})}) == set()


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
    tools, calls, omitted = _snapshot_tool_timeline(log)
    assert tools == ["read_files", "grep_search"]
    assert omitted == 0
    assert [call["argsBrief"] for call in calls] == [
        "a.py",
        "needle",
        "b.py",
    ]

    oversized = [
        {"round": index, "tool": "read_files", "args_brief": str(index)}
        for index in range(_SNAPSHOT_TOOLCALLS_CAP + 3)
    ]
    _, bounded, omitted = _snapshot_tool_timeline(oversized)
    assert len(bounded) == _SNAPSHOT_TOOLCALLS_CAP
    assert omitted == 3
    assert bounded[-1]["argsBrief"] == str(_SNAPSHOT_TOOLCALLS_CAP + 2)


def test_snapshot_tool_timeline_has_a_hard_byte_budget_and_honest_elision():
    import json

    from lib.swarm.master import (
        _SNAPSHOT_TOOL_TIMELINE_BYTES,
        _snapshot_tool_timeline,
    )

    exact_acceptance_preview = "x" * 2000
    _, one_call, omitted = _snapshot_tool_timeline([{
        "round": 1,
        "tool": "fetch_url",
        "args_brief": "https://example.test",
        "preview": exact_acceptance_preview,
    }])
    assert omitted == 0
    assert one_call[0]["preview"] == exact_acceptance_preview
    assert one_call[0]["previewTruncated"] is False

    oversized = [{
        "round": index,
        "tool": "read_files",
        "args_brief": f"file-{index}",
        "preview": "🧪" * 5000,
    } for index in range(60)]
    _, bounded, omitted = _snapshot_tool_timeline(oversized)
    serialized_bytes = len(json.dumps(
        bounded, ensure_ascii=True, separators=(",", ":")))
    assert serialized_bytes <= _SNAPSHOT_TOOL_TIMELINE_BYTES
    assert omitted >= 30
    assert bounded[-1]["preview"] == "🧪" * 2000
    assert bounded[-1]["previewTruncated"] is True
    assert bounded[-1]["previewFullChars"] == 5000


def test_spawn_settlement_compensates_when_fast_agent_finished_before_handle(
        monkeypatch):
    import json

    from lib.swarm import snapshot as snapshot_module

    settled = {
        "agents": [{
            "id": "fast-agent",
            "role": "researcher",
            "objective": "finish before handle settlement",
            "status": "done",
            "preview": "complete execution detail",
            "toolCalls": [{"tool": "read_files", "argsBrief": "a.py"}],
            "tokens": 11,
        }],
        "settled": True,
        "totalTokens": 11,
        "agentCount": 1,
        "doneCount": 1,
        "version": 100001,
    }

    class _FastSettledSession:
        def _build_agent_snapshot(self):
            return settled

    monkeypatch.setattr(
        "lib.swarm.integration._state._get_session",
        lambda _key: _FastSettledSession(),
    )
    round_entry = {
        "toolName": "spawn_agents",
        "toolContent": json.dumps({
            "status": "async_launched",
            "agents": [{"id": "fast-agent"}],
        }),
    }

    assert snapshot_module.reconcile_spawn_round_from_active_session(
        {"id": "spawn-task", "convId": "spawn-conv"}, round_entry,
    ) is True
    assert round_entry["_swarmSnapshot"] == settled
    assert round_entry["_swarmSnapshot"]["agents"][0]["toolCalls"] == [
        {"tool": "read_files", "argsBrief": "a.py"},
    ]

    # Settlement/recovery may replay the compensation. Equal snapshots are a
    # no-op and can never duplicate agent details or regress their version.
    assert snapshot_module.reconcile_spawn_round_from_active_session(
        {"id": "spawn-task", "convId": "spawn-conv"}, round_entry,
    ) is False
    assert len(round_entry["_swarmSnapshot"]["agents"]) == 1


def test_tool_settlement_runs_spawn_compensation_after_handle_is_stamped(
        monkeypatch):
    import json

    from lib.tasks_pkg.tool_dispatch._pipeline import _settle_tool_result

    observed = []

    def _reconcile(task, round_entry):
        observed.append((task, dict(round_entry)))
        round_entry["_swarmSnapshot"] = {
            "agents": [{"id": "fast-agent", "status": "done"}],
            "settled": True,
            "version": 100001,
        }
        return True

    monkeypatch.setattr(
        "lib.swarm.snapshot.reconcile_spawn_round_from_active_session",
        _reconcile,
    )
    handle = json.dumps({
        "status": "async_launched",
        "agents": [{"id": "fast-agent"}],
    })
    task = {
        "id": "spawn-task",
        "convId": "spawn-conv",
        "_userId": 1,
        "config": {},
    }
    round_entry = {"toolName": "spawn_agents", "toolCallId": "call-1"}

    result = _settle_tool_result(
        task,
        "spawn_agents",
        "call-1",
        {"agents": [{"id": "fast-agent"}]},
        0,
        round_entry,
        handle,
        idempotent_tools=frozenset(),
        cache={},
        tid="spawn-ta",
        round_num=1,
        settled_results={},
    )

    assert json.loads(result) == json.loads(handle)
    assert len(observed) == 1
    assert json.loads(observed[0][1]["toolContent"]) == json.loads(handle)
    assert round_entry["_swarmSnapshot"]["version"] == 100001


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
