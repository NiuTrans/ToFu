"""Orchestration v2 must distinguish wire projection from real adoption."""

from __future__ import annotations

import pytest

from lib.benchmark_contract import BenchmarkContractError, build_task_record_v2
from lib.orchestration_adoption import (
    orchestration_adoption_summary,
    public_orchestration_decisions,
    reconcile_response_orchestration,
    record_orchestration_execution,
    record_orchestration_projection,
)


pytestmark = pytest.mark.unit


def _raw_decision(shape: str, *, round_number: int = 1) -> dict:
    return {
        "policyVersion": "tool-orchestration/v2",
        "compositionMode": shape,
        "shape": shape,
        "round": round_number,
        "programmaticCalling": (
            "on" if shape == "ptc_bounded_reduction" else "off"),
        "programmaticReason": (
            "bounded_read_only_reduction"
            if shape == "ptc_bounded_reduction" else "disabled"),
        "programmaticTier": (
            "program" if shape == "ptc_bounded_reduction" else ""),
        "multiAgent": (
            "read_only"
            if shape == "independent_read_only_agents" else "off"),
        "multiAgentReason": (
            "independent_complex_workstreams"
            if shape == "independent_read_only_agents" else "disabled"),
        "expectedSavings": {"basis": {
            "direct_execution": "simple_task",
            "ptc_bounded_reduction": "eligible_read_fanout",
            "independent_read_only_agents": "independent_workstreams",
            "verified_loop": "mutation_plus_verification",
        }[shape]},
        "projectionEvidence": [],
        "adoptionEvidence": [],
    }


def _public_decision(shape: str, *, evidence: str = "") -> dict:
    raw = _raw_decision(shape)
    task = {"_toolOrchestrationDecisions": [raw]}
    if evidence == "model":
        task["apiRounds"] = [{"round": 1, "usage": {"inputTokens": 1}}]
    elif evidence == "program":
        record_orchestration_execution(
            task, lane="programmatic", kind="program_run",
            backend="local_toolscript", call_id="program-1",
            status="completed", child_call_count=2, round_index=0)
    elif evidence == "agent":
        record_orchestration_execution(
            task, lane="multi_agent", kind="agent_wave",
            backend="local_swarm", status="launched", agent_count=2,
            round_index=0)
    return public_orchestration_decisions(task)[0]


def _benchmark_record(*, decisions: list[dict]) -> dict:
    return build_task_record_v2(
        run_id="orchestration-run", dataset="orchestration-pilot",
        family="integrated_multi_tool", task_id="task-1",
        agent={"name": "tofu", "version": "test",
               "commitSha256": "a" * 64},
        provider_face="meituan-chat", provider_slot_id="kimi-slot-fixture",
        thinking="high",
        experiment_arm="orchestration_v2",
        oracle={"passed": True, "type": "exact"},
        rounds=[{"round": 1, "usage": {"inputTokens": 10}}],
        context_blocks=[], tool_schemas=[], tool_results=[], compactions=[],
        call_graph=[], retries=[], cost={"agentCostUsd": 0.01},
        latency={"rawWallMs": 100, "oracleReadyMs": 100,
                 "queueMs": 0, "ttftMs": 10, "modelMs": 80, "toolMs": 10,
                 "translationCpuMs": 10,
                 "proxyCpuMs": 20,
                 "codexFavoredCorrectedWallMs": 90},
        orchestration_decisions=decisions,
    )


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_wire_projection_is_not_actual_program_adoption():
    raw = _raw_decision("ptc_bounded_reduction")
    task = {"_toolOrchestrationDecisions": [raw]}

    record_orchestration_projection(
        raw, lane="programmatic", backend="local")
    projected = public_orchestration_decisions(task)[0]

    assert projected["projectionEvidence"][0]["kind"] == "wire_projection"
    assert projected["adoptionEvidence"] == []
    assert projected["adoptionStatus"] == "not_adopted"
    assert projected["actualShape"] == "not_adopted"

    record_orchestration_execution(
        task, lane="programmatic", kind="program_run",
        backend="local_toolscript", call_id="program-actual",
        status="completed", child_call_count=3, round_index=0)
    adopted = public_orchestration_decisions(task)[0]
    assert adopted["adoptionStatus"] == "adopted"
    assert adopted["actualShape"] == "ptc_bounded_reduction"
    assert adopted["adoptionEvidence"][0]["callId"] == "program-actual"


def test_provider_request_projection_uses_projection_evidence_only():
    from lib.llm._sse_core import prepare_request

    raw = _raw_decision("ptc_bounded_reduction")
    catalog = [_tool("read_files")]
    prepare_request({
        "model": "kimi-k3",
        "messages": [{"role": "user", "content": "read and aggregate"}],
        "tools": catalog,
        "_executable_tool_catalog": catalog,
        "_tool_wire_catalog": catalog,
        "_programmatic_tool_calling": "on",
        "_programmatic_tier": "program",
        "_programmatic_eligible_tools": ["read_files"],
        "_multi_agent_mode": "off",
        "_tool_orchestration_policy_version": "tool-orchestration/v2",
        "_tool_orchestration_decision_sink": raw,
    }, api_key="secret", base_url="https://example.test/v1",
       api_protocol="openai")

    assert raw["programmaticBackend"] == "local"
    assert raw["projectionEvidence"] == [{
        "kind": "wire_projection", "lane": "programmatic",
        "backend": "local", "round": 1,
    }]
    assert raw["adoptionEvidence"] == []


def test_program_projection_and_native_agent_item_are_actual_trajectories():
    from lib.tasks_pkg.orchestrator._programmatic import project_program_run

    program_task = {
        "_toolOrchestrationDecisions": [
            _raw_decision("ptc_bounded_reduction")],
        "toolRounds": [],
    }
    project_program_run(program_task, {
        "callId": "program-native", "source": "openai_ptc",
        "status": "completed", "result": {"count": 2},
        "childCalls": [{"id": "child-1", "name": "read_files"}],
        "limits": {},
    }, llm_round=0, terminal=True)
    program = public_orchestration_decisions(program_task)[0]
    assert program["adoptionStatus"] == "adopted"
    assert program["adoptionEvidence"][0]["kind"] == "program_run"

    agent_task = {
        "_toolOrchestrationDecisions": [
            _raw_decision("independent_read_only_agents")],
    }
    reconcile_response_orchestration(agent_task, {
        "_responses_items": [{
            "type": "multi_agent_call", "id": "agent-call-1",
            "status": "completed",
        }],
    }, round_index=0)
    agent = public_orchestration_decisions(agent_task)[0]
    assert agent["adoptionStatus"] == "adopted"
    assert {row["kind"] for row in agent["adoptionEvidence"]} == {
        "model_round", "native_multi_agent_call"}

    malformed_agent_task = {
        "_toolOrchestrationDecisions": [
            _raw_decision("independent_read_only_agents")],
    }
    reconcile_response_orchestration(malformed_agent_task, {
        "_responses_items": [{"type": "multi_agent_call", "id": ""}],
    }, round_index=0)
    malformed = public_orchestration_decisions(malformed_agent_task)[0]
    assert malformed["adoptionStatus"] == "not_adopted"
    assert malformed["adoptionEvidence"] == [{
        "kind": "model_round", "lane": "runner", "backend": "model",
        "status": "completed", "round": 1,
    }]


def test_equal_native_agent_items_remain_distinct_response_occurrences():
    task = {
        "_toolOrchestrationDecisions": [
            _raw_decision("independent_read_only_agents")],
    }
    repeated_item = {
        "type": "multi_agent_call", "id": "recycled-agent-call",
        "status": "completed",
    }

    reconcile_response_orchestration(task, {
        "_responses_items": [dict(repeated_item), dict(repeated_item)],
    }, round_index=0)

    native = [
        row for row in public_orchestration_decisions(task)[0][
            "adoptionEvidence"]
        if row["kind"] == "native_multi_agent_call"
    ]
    assert [row["outputPosition"] for row in native] == [0, 1]
    assert [row["callId"] for row in native] == [
        "recycled-agent-call", "recycled-agent-call",
    ]


def test_successful_local_read_only_wave_records_actual_agent_adoption(
        monkeypatch, tmp_path):
    import json

    import lib.swarm.integration._tools as swarm_tools

    class FakeMaster:
        def __init__(self, **_kwargs):
            pass

        def run_in_background(self):
            return None

    monkeypatch.setattr(swarm_tools, "MasterOrchestrator", FakeMaster)
    monkeypatch.setattr(swarm_tools, "_set_session", lambda *_a, **_k: None)
    monkeypatch.setattr(
        swarm_tools, "_resolve_output_dir", lambda _task_id: tmp_path)
    from lib.swarm import persistence
    monkeypatch.setattr(persistence, "save_session", lambda *_a, **_k: None)

    decision = _raw_decision("independent_read_only_agents")
    decision["maxConcurrentAgents"] = 2
    task = {
        "id": "task", "convId": "conv", "_userId": 17,
        "_toolOrchestration": decision,
        "_toolOrchestrationDecisions": [decision],
    }
    handle = json.loads(swarm_tools._handle_spawn_agents(
        {"agents": [{"id": "audit", "objective": "inspect"}]},
        task_id="task", task=task, cfg={}, all_tools=[_tool("read_files")],
        model="kimi-k3", thinking_enabled=False, project_path="",
        abort_check=None, on_event=None))

    assert handle["status"] == "async_launched"
    public = public_orchestration_decisions(task)[0]
    assert public["adoptionStatus"] == "adopted"
    assert public["adoptionEvidence"] == [{
        "kind": "agent_wave", "lane": "multi_agent",
        "backend": "local_swarm", "status": "launched",
        "agentCount": 1, "round": 1,
    }]


def test_result_metadata_retains_reason_savings_and_actual_status():
    from lib.tasks_pkg.manager import build_result_meta

    raw = _raw_decision("ptc_bounded_reduction")
    record_orchestration_projection(
        raw, lane="programmatic", backend="local")
    meta = build_result_meta({"_toolOrchestrationDecisions": [raw]})
    decision = meta["toolOrchestrationDecisions"][0]

    assert decision["shape"] == "ptc_bounded_reduction"
    assert decision["expectedSavings"]["basis"] == "eligible_read_fanout"
    assert decision["programmaticReason"] == "bounded_read_only_reduction"
    assert decision["projectionEvidence"]
    assert decision["adoptionEvidence"] == []
    assert decision["adoptionStatus"] == "not_adopted"


def test_benchmark_arm_requires_valid_decisions_without_false_claims():
    direct = _public_decision("direct_execution", evidence="model")
    record = _benchmark_record(decisions=[direct])
    assert record["orchestrationDecisions"][0]["adoptionStatus"] == "adopted"

    with pytest.raises(BenchmarkContractError, match="requires orchestration"):
        _benchmark_record(decisions=[])

    projection_only = _raw_decision("ptc_bounded_reduction")
    record_orchestration_projection(
        projection_only, lane="programmatic", backend="local")
    projection_public = public_orchestration_decisions({
        "_toolOrchestrationDecisions": [projection_only],
    })[0]
    # An honest not-adopted decision remains valid evidence; promotion will
    # require a non-zero actual trajectory count across the frozen run.
    assert _benchmark_record(
        decisions=[projection_public])["orchestrationDecisions"]

    false_claim = dict(projection_public)
    false_claim["adoptionStatus"] = "adopted"
    false_claim["actualShape"] = "ptc_bounded_reduction"
    with pytest.raises(BenchmarkContractError, match="contradicts evidence"):
        _benchmark_record(decisions=[false_claim])

    wire_as_actual = dict(projection_public)
    wire_as_actual["adoptionEvidence"] = [{
        "kind": "wire_projection", "lane": "programmatic",
        "backend": "local",
    }]
    with pytest.raises(BenchmarkContractError, match="wire projection"):
        _benchmark_record(decisions=[wire_as_actual])


def test_release_summary_counts_only_real_program_and_agent_trajectories():
    program = _public_decision("ptc_bounded_reduction", evidence="program")
    agent = _public_decision(
        "independent_read_only_agents", evidence="agent")
    summary = orchestration_adoption_summary([
        {"recordType": "task", "taskId": "p",
         "orchestrationDecisions": [program]},
        {"recordType": "task", "taskId": "a",
         "orchestrationDecisions": [agent]},
    ])

    assert summary["taskRecords"] == 2
    assert summary["tasksWithV2Decisions"] == 2
    assert summary["v2Decisions"] == 2
    assert summary["programTrajectories"] == 1
    assert summary["agentTrajectories"] == 1
    assert summary["falseAdoptionClaims"] == 0
