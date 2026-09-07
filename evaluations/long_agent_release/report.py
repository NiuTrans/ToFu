"""Derive paired pilot diagnostics and the conjunctive release decision.

All values come from revalidated immutable task records.  A diagnostic pilot
can never produce a release claim; only the exact 1,845-row frozen matrix is
passed to ``acceptance_decision_v2``.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from lib.benchmark_contract import (
    BenchmarkContractError,
    acceptance_decision_v2,
    paired_quality_interval,
    validate_release_task_matrix_v2,
)
from lib.cost import normalize_usage
from lib.orchestration_adoption import orchestration_adoption_summary

from .run_store import (
    ReleaseRunError,
    audit_release_pair,
    load_release_manifest,
    load_release_task_records,
)


RELEASE_REPORT_CONTRACT = "tofu-long-agent-release-report/v1"
_REQUIRED_JUDGES = ("claude-opus-5", "glm-5.3")
_JUDGED_FAMILIES = frozenset({"frozen_research", "long_writing"})
_CRITICAL_INCIDENT_CODES = frozenset({
    "authorization_violation",
    "data_damage",
    "false_completion",
    "permission_violation",
    "security_violation",
})


class ReleaseReportError(ValueError):
    """Paired artifacts are insufficient or contradictory."""


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReleaseReportError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ReleaseReportError(f"{label} must be finite and non-negative")
    return result


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ReleaseReportError("latency percentile requires observations")
    # Preregistered nearest-rank percentile: ceil(p*n), one-indexed.
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _tokens(row: dict[str, Any]) -> int:
    for field in (
        "tokenCount", "tokens", "selectedTokens", "visibleTokens",
        "schemaTokens", "resultTokens", "contextTokens",
    ):
        value = row.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _tool_name(row: dict[str, Any]) -> str:
    return str(row.get("toolName") or row.get("name") or "")


def _incident_code(row: dict[str, Any]) -> str:
    return str(row.get("code") or row.get("category") or row.get("type") or (
        "unclassified_incident"
    )).lower()


def _critical_incidents(records: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    counts: Counter[str] = Counter()
    critical = 0
    for record in records:
        for incident in record.get("incidents") or []:
            code = _incident_code(incident)
            counts[code] += 1
            if incident.get("critical") is True \
                    or str(incident.get("severity") or "").lower() == "critical" \
                    or code in _CRITICAL_INCIDENT_CODES:
                critical += 1
    return critical, dict(sorted(counts.items()))


def _judge_name(row: dict[str, Any]) -> str:
    return str(row.get("judge") or row.get("name") or row.get("model") or "")


def _judge_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [record for record in records if record.get("family") in _JUDGED_FAMILIES]
    per_judge: dict[str, dict[str, Any]] = {}
    order_counts: Counter[str] = Counter()
    errors: list[str] = []
    for judge_name in _REQUIRED_JUDGES:
        passed = 0
        valid = 0
        for record in judged:
            rows = [
                row for row in record.get("judges") or []
                if _judge_name(row) == judge_name
            ]
            if len(rows) != 1:
                errors.append(
                    f"{record.get('taskId')}:{judge_name}:expected-one-row"
                )
                continue
            row = rows[0]
            order = str(row.get("order") or "").upper().replace("/", "")
            row_valid = (
                isinstance(row.get("passed"), bool)
                and row.get("blind") is True
                and order in {"AB", "BA"}
            )
            if not row_valid:
                errors.append(
                    f"{record.get('taskId')}:{judge_name}:invalid-blind-row"
                )
                continue
            valid += 1
            order_counts[order] += 1
            passed += int(row["passed"])
        per_judge[judge_name] = {
            "expectedTasks": len(judged),
            "validBlindRows": valid,
            "passedTasks": passed,
            "allPass": bool(judged) and valid == len(judged) and passed == len(judged),
        }
    return {
        "judgedTasks": len(judged),
        "requiredJudges": list(_REQUIRED_JUDGES),
        "perJudge": per_judge,
        "orderCounts": dict(sorted(order_counts.items())),
        "errors": errors[:50],
        "allPass": bool(judged) and not errors and all(
            row["allPass"] for row in per_judge.values()
        ),
    }


def _arm_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        raise ReleaseReportError("arm summary requires task records")
    usage_totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "reasoningTokens": 0,
    }
    raw_latency: list[float] = []
    formal_latency: list[float] = []
    ttft: list[float] = []
    model: list[float] = []
    tool: list[float] = []
    cost = 0.0
    rounds = 0
    compactions = 0
    evidence_retention_failures = 0
    context_rows = 0
    context_tokens = 0
    schema_rows = 0
    schema_tokens = 0
    result_rows = 0
    result_tokens = 0
    search_calls = 0
    execute_calls = 0
    search_tasks = 0
    adopted_search_tasks = 0
    retry_codes: Counter[str] = Counter()
    oracle_failures = 0
    for record in records:
        oracle_failures += int(record["oracle"]["passed"] is False)
        task_searches = 0
        task_executes = 0
        for round_row in record["rounds"]:
            rounds += 1
            usage = normalize_usage(round_row.get("usage") or {})
            usage_totals["inputTokens"] += usage["input"]
            usage_totals["outputTokens"] += usage["output"]
            usage_totals["cacheReadTokens"] += usage["cache_read"]
            usage_totals["cacheWriteTokens"] += usage["cache_write"]
            usage_totals["reasoningTokens"] += usage["thinking"]
        for retry in record.get("retries") or []:
            retry_codes[str(retry.get("code") or "unknown")] += 1
        for compaction in record.get("compactions") or []:
            compactions += 1
            if compaction.get("evidenceRetained") is False \
                    or compaction.get("validationStatus") == "rejected":
                evidence_retention_failures += 1
        context_rows += len(record["contextBlocks"])
        context_tokens += sum(_tokens(row) for row in record["contextBlocks"])
        schema_rows += len(record["toolSchemas"])
        schema_tokens += sum(_tokens(row) for row in record["toolSchemas"])
        result_rows += len(record["toolResults"])
        result_tokens += sum(_tokens(row) for row in record["toolResults"])
        for row in record["toolResults"]:
            name = _tool_name(row)
            if name == "search_tools":
                search_calls += 1
                task_searches += 1
            elif name == "execute_tools":
                execute_calls += 1
                task_executes += 1
        if task_searches:
            search_tasks += 1
            adopted_search_tasks += int(task_executes > 0)
        latency = record["latency"]
        raw_latency.append(_finite(latency.get("oracleReadyMs"), "oracleReadyMs"))
        formal_latency.append(_finite(
            latency.get("codexFavoredCorrectedWallMs"),
            "codexFavoredCorrectedWallMs",
        ))
        ttft.append(_finite(latency.get("ttftMs"), "ttftMs"))
        model.append(_finite(latency.get("modelMs"), "modelMs"))
        tool.append(_finite(latency.get("toolMs"), "toolMs"))
        cost += _finite(record["cost"].get("agentCostUsd"), "agentCostUsd")
    successes = len(records) - oracle_failures
    critical, incident_counts = _critical_incidents(records)
    return {
        "tasks": len(records),
        "successes": successes,
        "successRate": successes / len(records),
        "agentCostUsd": cost,
        "agentCostPerSuccessUsd": cost / successes if successes else None,
        "latency": {
            "percentileMethod": "nearest_rank",
            "rawOracleReadyP50Ms": _percentile(raw_latency, 0.50),
            "rawOracleReadyP90Ms": _percentile(raw_latency, 0.90),
            "formalCodexFavoredP50Ms": _percentile(formal_latency, 0.50),
            "formalCodexFavoredP90Ms": _percentile(formal_latency, 0.90),
            "ttftP50Ms": _percentile(ttft, 0.50),
            "ttftP90Ms": _percentile(ttft, 0.90),
            "modelTotalMs": sum(model),
            "toolTotalMs": sum(tool),
        },
        "usage": {**usage_totals, "rounds": rounds},
        "context": {"blocks": context_rows, "tokens": context_tokens},
        "toolSchemas": {"rows": schema_rows, "tokens": schema_tokens},
        "toolResults": {"rows": result_rows, "tokens": result_tokens},
        "compactions": {
            "count": compactions,
            "evidenceRetentionFailures": evidence_retention_failures,
        },
        "toolSearch": {
            "searchCalls": search_calls,
            "executeGatewayCalls": execute_calls,
            "tasksWithSearch": search_tasks,
            "tasksWithSearchThenExecute": adopted_search_tasks,
            "taskAdoptionRate": (
                adopted_search_tasks / search_tasks if search_tasks else None
            ),
        },
        "failures": {
            "oracleFailures": oracle_failures,
            "infrastructureRetries": sum(retry_codes.values()),
            "retryCodes": dict(sorted(retry_codes.items())),
            "criticalIncidents": critical,
            "incidentCodes": incident_counts,
        },
    }


def _family_vectors(
    *, tasks: list[dict[str, Any]], candidate: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
) -> tuple[dict[str, list[bool]], dict[str, list[bool]], dict[str, Any]]:
    candidate_by_id = {str(row["taskId"]): row for row in candidate}
    baseline_by_id = {str(row["taskId"]): row for row in baseline}
    candidate_vectors: dict[str, list[bool]] = {}
    baseline_vectors: dict[str, list[bool]] = {}
    diagnostics: dict[str, Any] = {}
    for task in tasks:
        task_id = str(task["taskId"])
        family = str(task["family"])
        try:
            candidate_value = bool(candidate_by_id[task_id]["oracle"]["passed"])
            baseline_value = bool(baseline_by_id[task_id]["oracle"]["passed"])
        except KeyError as exc:
            raise ReleaseReportError(
                f"paired task record is missing: {task_id}"
            ) from exc
        candidate_vectors.setdefault(family, []).append(candidate_value)
        baseline_vectors.setdefault(family, []).append(baseline_value)
    for family in candidate_vectors:
        candidate_values = candidate_vectors[family]
        baseline_values = baseline_vectors[family]
        candidate_rate = sum(candidate_values) / len(candidate_values)
        baseline_rate = sum(baseline_values) / len(baseline_values)
        diagnostics[family] = {
            "pairs": len(candidate_values),
            "candidateRate": candidate_rate,
            "baselineRate": baseline_rate,
            "regression": baseline_rate - candidate_rate,
        }
    return candidate_vectors, baseline_vectors, diagnostics


def build_release_pair_report(
    *, baseline_manifest: dict[str, Any], candidate_manifest: dict[str, Any],
    baseline_records: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]],
    baseline_attempt_ledger: dict[str, Any] | None = None,
    candidate_attempt_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic report from already revalidated paired inputs."""

    tasks = [dict(row) for row in baseline_manifest.get("tasks") or []]
    if tasks != candidate_manifest.get("tasks"):
        raise ReleaseReportError("paired task tables differ")
    candidate_vectors, baseline_vectors, families = _family_vectors(
        tasks=tasks, candidate=candidate_records, baseline=baseline_records,
    )
    candidate_all = [value for family in candidate_vectors.values() for value in family]
    baseline_all = [value for family in baseline_vectors.values() for value in family]
    quality = paired_quality_interval(
        candidate_all, baseline_all, confidence=0.95,
        noninferiority_margin=0.03,
    )
    baseline_summary = _arm_summary(baseline_records)
    candidate_summary = _arm_summary(candidate_records)
    judges = _judge_summary(candidate_records)
    adoption = orchestration_adoption_summary(candidate_records)
    ledger_rows = [baseline_attempt_ledger, candidate_attempt_ledger]
    ledger_complete = all(
        isinstance(row, dict) and row.get("valid") is True
        and row.get("complete") is True
        for row in ledger_rows
    )
    if ledger_complete:
        infra_failures = sum(int(row["failedAttempts"]) for row in ledger_rows)
        attempts = sum(int(row["totalAttempts"]) for row in ledger_rows)
        attempt_evidence_source = "immutable_pre_dispatch_attempt_ledger"
    else:
        infra_failures = (
            baseline_summary["failures"]["infrastructureRetries"]
            + candidate_summary["failures"]["infrastructureRetries"]
        )
        attempts = len(tasks) * 2 + infra_failures
        attempt_evidence_source = "task_retry_rows_diagnostic_only"
    infrastructure_failure_rate = infra_failures / attempts if attempts else 0.0
    full_matrix = True
    try:
        validate_release_task_matrix_v2(tasks)
    except BenchmarkContractError:
        full_matrix = False
    if full_matrix:
        decision = acceptance_decision_v2(
            candidate_by_family=candidate_vectors,
            baseline_by_family=baseline_vectors,
            task_table=tasks,
            candidate_agent_cost_usd=candidate_summary["agentCostUsd"],
            baseline_agent_cost_usd=baseline_summary["agentCostUsd"],
            candidate_p90_oracle_ready_ms=(
                candidate_summary["latency"]["formalCodexFavoredP90Ms"]
            ),
            baseline_p90_oracle_ready_ms=(
                baseline_summary["latency"]["formalCodexFavoredP90Ms"]
            ),
            candidate_critical_incidents=(
                candidate_summary["failures"]["criticalIncidents"]
            ),
            judge_passes={
                name: judges["perJudge"][name]["allPass"]
                for name in _REQUIRED_JUDGES
            },
            infrastructure_failure_rate=infrastructure_failure_rate,
            maximum_infrastructure_failure_rate=float(
                candidate_manifest["limits"][
                    "maximumInfrastructureFailureRate"
                ]
            ),
            candidate_orchestration_adoption=adoption,
        )
        decision["gates"]["completeImmutableAttemptLedger"] = ledger_complete
        decision["releaseEligible"] = bool(
            decision["releaseEligible"] and ledger_complete)
        if not decision["releaseEligible"]:
            decision["claim"] = (
                "not demonstrated; inspect failed gates and families"
            )
    else:
        decision = {
            "contractVersion": "tofu-benchmark/v2",
            "releaseEligible": False,
            "claim": "not demonstrated; full frozen 1,845-task matrix required",
            "quality": quality,
            "families": families,
            "gates": {"fullFrozenMatrix": False},
        }
    return {
        "contractVersion": RELEASE_REPORT_CONTRACT,
        "pairId": baseline_manifest.get("pairId"),
        "baselineRunId": baseline_manifest.get("runId"),
        "candidateRunId": candidate_manifest.get("runId"),
        "fullFrozenMatrix": full_matrix,
        "taskCount": len(tasks),
        "formalLatencyBasis": (
            "candidate oracle-ready raw wall; Codex baseline raw wall minus "
            "proxy pure translation CPU"
        ),
        "baseline": baseline_summary,
        "candidate": candidate_summary,
        "pairedQuality": quality,
        "families": families,
        "judges": judges,
        "orchestrationAdoption": adoption,
        "infrastructure": {
            "evidenceSource": attempt_evidence_source,
            "completeImmutableAttemptLedger": ledger_complete,
            "failedAttempts": infra_failures,
            "totalAttempts": attempts,
            "failureRate": infrastructure_failure_rate,
            "maximumPreregisteredRate": candidate_manifest["limits"][
                "maximumInfrastructureFailureRate"
            ],
        },
        "releaseDecision": decision,
        "claim": decision["claim"],
    }


def analyze_release_pair(
    *, baseline_root: Any, candidate_root: Any,
) -> dict[str, Any]:
    """Re-audit finalized stores, then derive their paired report."""

    pair = audit_release_pair(
        baseline_root=baseline_root,
        candidate_root=candidate_root,
        require_complete=True,
    )
    if not pair["valid"] or not pair["pairReady"]:
        raise ReleaseReportError(
            "paired run stores are not complete and valid: " + str(pair["errors"])
        )
    if not pair["baseline"]["finalized"] or not pair["candidate"]["finalized"]:
        raise ReleaseReportError(
            "paired runs must be finalized before report generation"
        )
    try:
        baseline_manifest = load_release_manifest(baseline_root)
        candidate_manifest = load_release_manifest(candidate_root)
        baseline_records = load_release_task_records(baseline_root)
        candidate_records = load_release_task_records(candidate_root)
    except ReleaseRunError as exc:
        raise ReleaseReportError("release evidence changed during report") from exc
    return build_release_pair_report(
        baseline_manifest=baseline_manifest,
        candidate_manifest=candidate_manifest,
        baseline_records=baseline_records,
        candidate_records=candidate_records,
        baseline_attempt_ledger=pair["baseline"]["attemptLedger"],
        candidate_attempt_ledger=pair["candidate"]["attemptLedger"],
    )


__all__ = [
    "RELEASE_REPORT_CONTRACT",
    "ReleaseReportError",
    "analyze_release_pair",
    "build_release_pair_report",
]
