"""Runtime evidence contract for explainable orchestration decisions.

Responsibility
--------------
Keep wire availability separate from actual adoption.  A projected PTC or
multi-agent backend proves only that the model could use that lane; a program
run or agent wave is required before reports may say that it was adopted.

Entry points
------------
``record_orchestration_projection`` records bounded request projection,
``record_orchestration_execution`` records a real runtime trajectory, and
``public_orchestration_decisions`` derives the persisted/benchmark projection
from the task-owned runtime state.

Dependencies
------------
This module is deliberately storage- and provider-neutral.  Callers retain
task ownership and persistence lifecycle; no filesystem or database details
cross this contract boundary.
"""

from __future__ import annotations

from typing import Any, Iterable


ORCHESTRATION_DECISION_VERSION = "tofu.orchestration-decision/v2"
ORCHESTRATION_POLICY_V2 = "tool-orchestration/v2"
ORCHESTRATION_SHAPES = frozenset({
    "direct_execution",
    "ptc_bounded_reduction",
    "independent_read_only_agents",
    "verified_loop",
})
_MAX_EVIDENCE_PER_DECISION = 32
_PROGRAM_EVIDENCE_KINDS = frozenset({"program_run"})
_AGENT_EVIDENCE_KINDS = frozenset({
    "agent_wave", "native_multi_agent_call",
})
_RUNNER_EVIDENCE_KINDS = frozenset({"model_round"})
_ACTUAL_EVIDENCE_KINDS = (
    _PROGRAM_EVIDENCE_KINDS
    | _AGENT_EVIDENCE_KINDS
    | _RUNNER_EVIDENCE_KINDS
)
_PUBLIC_DECISION_FIELDS = (
    "policyVersion", "compositionMode", "shape", "round",
    "programmaticCalling", "programmaticReason", "programmaticTier",
    "programmaticBackend", "multiAgent", "multiAgentReason",
    "multiAgentBackend", "maxConcurrentAgents", "expectedSavings",
)
_PUBLIC_EVIDENCE_FIELDS = (
    "kind", "lane", "backend", "callId", "status", "agentCount",
    "childCallCount", "round",
)


def _v2_decisions(task: dict[str, Any]) -> list[dict[str, Any]]:
    rows = task.get("_toolOrchestrationDecisions")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)
            and row.get("policyVersion") == ORCHESTRATION_POLICY_V2]


def _decision_for_lane(
    task: dict[str, Any], lane: str, *, round_index: int | None = None,
) -> dict[str, Any] | None:
    wanted_shape = {
        "programmatic": "ptc_bounded_reduction",
        "multi_agent": "independent_read_only_agents",
    }.get(str(lane))
    rows = _v2_decisions(task)
    if round_index is not None:
        target_round = int(round_index) + 1
        for row in reversed(rows):
            if int(row.get("round") or 0) == target_round \
                    and (not wanted_shape or row.get("shape") == wanted_shape):
                return row
    if wanted_shape:
        for row in reversed(rows):
            if row.get("shape") == wanted_shape:
                return row
    return rows[-1] if rows else None


def _bounded_public_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: evidence[key] for key in _PUBLIC_EVIDENCE_FIELDS
        if key in evidence and evidence[key] not in (None, "")
    }
    for field in ("agentCount", "childCallCount", "round"):
        if field in public:
            try:
                public[field] = max(0, int(public[field]))
            except (TypeError, ValueError):
                public.pop(field, None)
    if "callId" in public:
        public["callId"] = str(public["callId"])[:256]
    for field in ("kind", "lane", "backend", "status"):
        if field in public:
            public[field] = str(public[field])[:128]
    return public


def _append_unique(row: dict[str, Any], field: str,
                   evidence: dict[str, Any]) -> None:
    rows = row.setdefault(field, [])
    if not isinstance(rows, list):
        rows = []
        row[field] = rows
    public = _bounded_public_evidence(evidence)
    if not public or public in rows:
        return
    rows.append(public)
    del rows[:-_MAX_EVIDENCE_PER_DECISION]


def record_orchestration_projection(
    decision: dict[str, Any] | None, *, lane: str, backend: str,
) -> None:
    """Record that a lane reached the provider wire, never that it ran."""
    if not isinstance(decision, dict) \
            or decision.get("policyVersion") != ORCHESTRATION_POLICY_V2:
        return
    _append_unique(decision, "projectionEvidence", {
        "kind": "wire_projection", "lane": lane, "backend": backend,
        "round": decision.get("round"),
    })


def record_orchestration_execution(
    task: dict[str, Any], *, lane: str, kind: str, backend: str = "",
    call_id: str = "", status: str = "started", agent_count: int | None = None,
    child_call_count: int | None = None, round_index: int | None = None,
) -> None:
    """Attach one real program/agent/runner trajectory to its v2 decision."""
    if kind not in _ACTUAL_EVIDENCE_KINDS:
        raise ValueError(f"unsupported orchestration evidence kind: {kind}")
    if kind in ("program_run", "native_multi_agent_call") \
            and not str(call_id or ""):
        return
    if kind == "agent_wave":
        try:
            if int(agent_count or 0) <= 0:
                return
        except (TypeError, ValueError):
            return
    decision = _decision_for_lane(task, lane, round_index=round_index)
    if decision is None:
        return
    evidence: dict[str, Any] = {
        "kind": kind, "lane": lane, "backend": backend,
        "callId": call_id, "status": status,
        "round": (int(round_index) + 1 if round_index is not None
                  else decision.get("round")),
    }
    if agent_count is not None:
        evidence["agentCount"] = agent_count
    if child_call_count is not None:
        evidence["childCallCount"] = child_call_count
    _append_unique(decision, "adoptionEvidence", evidence)


def reconcile_response_orchestration(
    task: dict[str, Any], assistant_message: Any, *, round_index: int,
) -> None:
    """Record actual native multi-agent response items and the model round."""
    record_orchestration_execution(
        task, lane="runner", kind="model_round", backend="model",
        status="completed", round_index=round_index)
    if not isinstance(assistant_message, dict):
        return
    for item in assistant_message.get("_responses_items") or ():
        if not isinstance(item, dict) \
                or item.get("type") != "multi_agent_call":
            continue
        record_orchestration_execution(
            task, lane="multi_agent", kind="native_multi_agent_call",
            backend="native_openai",
            call_id=str(item.get("call_id") or item.get("id") or ""),
            status=str(item.get("status") or "started"),
            round_index=round_index)


def _actual_kinds(row: dict[str, Any]) -> set[str]:
    return {
        str(evidence.get("kind") or "")
        for evidence in row.get("adoptionEvidence") or ()
        if isinstance(evidence, dict)
        and evidence.get("kind") in _ACTUAL_EVIDENCE_KINDS
    }


def _shape_was_adopted(row: dict[str, Any]) -> bool:
    kinds = _actual_kinds(row)
    shape = row.get("shape")
    if shape == "ptc_bounded_reduction":
        return bool(kinds & _PROGRAM_EVIDENCE_KINDS)
    if shape == "independent_read_only_agents":
        return bool(kinds & _AGENT_EVIDENCE_KINDS)
    return bool(kinds & _RUNNER_EVIDENCE_KINDS)


def _copy_public_decision(row: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: row[key] for key in _PUBLIC_DECISION_FIELDS if key in row
    }
    public["contractVersion"] = ORCHESTRATION_DECISION_VERSION
    public["projectionEvidence"] = [
        _bounded_public_evidence(evidence)
        for evidence in row.get("projectionEvidence") or ()
        if isinstance(evidence, dict)
        and evidence.get("kind") == "wire_projection"
    ][-_MAX_EVIDENCE_PER_DECISION:]
    public["adoptionEvidence"] = [
        _bounded_public_evidence(evidence)
        for evidence in row.get("adoptionEvidence") or ()
        if isinstance(evidence, dict)
        and evidence.get("kind") in _ACTUAL_EVIDENCE_KINDS
    ][-_MAX_EVIDENCE_PER_DECISION:]
    return public


def _append_to_public(rows: list[dict[str, Any]], *, round_index: Any,
                      shape: str, evidence: dict[str, Any]) -> None:
    try:
        target = int(round_index) + 1
    except (TypeError, ValueError):
        target = 0
    candidates = [row for row in rows if row.get("shape") == shape]
    row = next((candidate for candidate in reversed(candidates)
                if target and int(candidate.get("round") or 0) == target),
               candidates[-1] if candidates else None)
    if row is not None:
        _append_unique(row, "adoptionEvidence", evidence)


def public_orchestration_decisions(
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the bounded, truth-preserving task result/benchmark projection."""
    raw_rows = task.get("_toolOrchestrationDecisions")
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in raw_rows[-64:]:
        if not isinstance(raw, dict):
            continue
        if raw.get("policyVersion") == ORCHESTRATION_POLICY_V2:
            rows.append(_copy_public_decision(raw))
        else:
            # Read-only compatibility for existing v1 task results.  V1 never
            # made an adoption claim, so preserve its compact decision shape
            # without retrofitting v2 status/evidence fields.
            rows.append({
                key: raw[key] for key in _PUBLIC_DECISION_FIELDS
                if key in raw
            })
    v2_rows = [row for row in rows
               if row.get("policyVersion") == ORCHESTRATION_POLICY_V2]
    if not v2_rows:
        return rows

    # Re-derive actual program evidence from the canonical run ledger.  This
    # makes terminal projection robust to process recovery between execution
    # and the request-local evidence append.
    for run in task.get("programRuns") or ():
        if not isinstance(run, dict) or not str(run.get("callId") or ""):
            continue
        source = str(run.get("source") or "openai_ptc")
        backend = ("local_toolscript" if source in (
            "execute_program", "local_toolscript") else "native_openai")
        _append_to_public(
            v2_rows, round_index=run.get("llmRound"),
            shape="ptc_bounded_reduction",
            evidence={
                "kind": "program_run", "lane": "programmatic",
                "backend": backend, "callId": run.get("callId"),
                "status": str(run.get("status") or "running"),
                "childCallCount": len(run.get("childCalls") or ()),
            })

    for api_round in task.get("apiRounds") or ():
        if not isinstance(api_round, dict):
            continue
        try:
            target = int(api_round.get("round") or 0)
        except (TypeError, ValueError):
            continue
        row = next((candidate for candidate in v2_rows
                    if int(candidate.get("round") or 0) == target), None)
        if row is not None:
            _append_unique(row, "adoptionEvidence", {
                "kind": "model_round", "lane": "runner",
                "backend": "model", "status": "completed", "round": target,
            })

    for row in v2_rows:
        # Never trust a mutable status latch: recompute the public claim from
        # the retained actual trajectory every time the task is projected.
        adopted = _shape_was_adopted(row)
        row["adoptionStatus"] = "adopted" if adopted else "not_adopted"
        row["actualShape"] = row.get("shape") if adopted else "not_adopted"
    return rows


def validate_public_orchestration_decision(row: Any) -> None:
    """Validate one persisted v2 decision and reject projection-as-adoption."""
    if not isinstance(row, dict):
        raise ValueError("orchestration decision must be an object")
    if row.get("contractVersion") != ORCHESTRATION_DECISION_VERSION \
            or row.get("policyVersion") != ORCHESTRATION_POLICY_V2:
        raise ValueError("orchestration decision version mismatch")
    if row.get("shape") not in ORCHESTRATION_SHAPES:
        raise ValueError("orchestration decision shape is invalid")
    if isinstance(row.get("round"), bool) \
            or not isinstance(row.get("round"), int) or row["round"] <= 0:
        raise ValueError("orchestration decision round must be positive")
    expected = row.get("expectedSavings")
    if not isinstance(expected, dict) or not str(expected.get("basis") or ""):
        raise ValueError("orchestration decision expectedSavings is required")
    for field in ("programmaticReason", "multiAgentReason"):
        if not str(row.get(field) or ""):
            raise ValueError(f"orchestration decision {field} is required")
    for field in ("projectionEvidence", "adoptionEvidence"):
        if not isinstance(row.get(field), list) \
                or any(not isinstance(value, dict) for value in row[field]):
            raise ValueError(f"orchestration decision {field} must be a list")
    if any(evidence.get("kind") not in _ACTUAL_EVIDENCE_KINDS
           for evidence in row["adoptionEvidence"]):
        raise ValueError("wire projection cannot prove orchestration adoption")
    for evidence in row["adoptionEvidence"]:
        kind = evidence.get("kind")
        if kind == "program_run" and not str(evidence.get("callId") or ""):
            raise ValueError("program adoption requires a real call id")
        if kind == "agent_wave" and int(evidence.get("agentCount") or 0) <= 0:
            raise ValueError("agent adoption requires a non-empty wave")
        if kind == "native_multi_agent_call" \
                and not str(evidence.get("callId") or ""):
            raise ValueError("native agent adoption requires a real call id")
    adopted = _shape_was_adopted(row)
    expected_status = "adopted" if adopted else "not_adopted"
    if row.get("adoptionStatus") != expected_status:
        raise ValueError("orchestration adoptionStatus contradicts evidence")
    expected_shape = row.get("shape") if adopted else "not_adopted"
    if row.get("actualShape") != expected_shape:
        raise ValueError("orchestration actualShape contradicts evidence")


def orchestration_adoption_summary(
    task_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate validated task evidence for a release/promotion gate."""
    tasks = 0
    tasks_with_v2_decisions = 0
    decisions = 0
    program_trajectories: set[tuple[str, str]] = set()
    agent_trajectories: set[tuple[str, str]] = set()
    adopted_shapes: dict[str, int] = {}
    for record in task_records:
        if not isinstance(record, dict) or record.get("recordType") != "task":
            continue
        tasks += 1
        task_id = str(record.get("taskId") or tasks)
        task_decisions = list(record.get("orchestrationDecisions") or ())
        if task_decisions:
            tasks_with_v2_decisions += 1
        for index, row in enumerate(task_decisions):
            validate_public_orchestration_decision(row)
            decisions += 1
            if row.get("adoptionStatus") == "adopted":
                shape = str(row.get("shape") or "")
                adopted_shapes[shape] = adopted_shapes.get(shape, 0) + 1
            for evidence_index, evidence in enumerate(
                    row.get("adoptionEvidence") or ()):
                kind = evidence.get("kind")
                identity = str(evidence.get("callId") or (
                    f"{index}:{evidence_index}"))
                if kind == "program_run":
                    program_trajectories.add((task_id, identity))
                elif kind in _AGENT_EVIDENCE_KINDS:
                    agent_trajectories.add((task_id, identity))
    return {
        "contractVersion": "tofu.orchestration-adoption-summary/v1",
        "taskRecords": tasks,
        "tasksWithV2Decisions": tasks_with_v2_decisions,
        "v2Decisions": decisions,
        "programTrajectories": len(program_trajectories),
        "agentTrajectories": len(agent_trajectories),
        "adoptedShapes": dict(sorted(adopted_shapes.items())),
        "falseAdoptionClaims": 0,
    }


__all__ = [
    "ORCHESTRATION_DECISION_VERSION", "ORCHESTRATION_POLICY_V2",
    "ORCHESTRATION_SHAPES", "orchestration_adoption_summary",
    "public_orchestration_decisions", "reconcile_response_orchestration",
    "record_orchestration_execution", "record_orchestration_projection",
    "validate_public_orchestration_decision",
]
