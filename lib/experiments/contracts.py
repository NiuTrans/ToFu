"""Canonical runtime projection of ``tofu.experiment/v1`` specifications.

The authored wire schema lives at ``contracts/experiments_v1.schema.json``.
This module resolves plugin references into an immutable document and verifies
its digest before assignment.  It depends only on the experiment registry and
standard-library JSON primitives.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_VERSION = "tofu.experiment/v1"
ASSIGNMENT_ALGORITHM = "sha256-owner-unit-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class ExperimentContractError(ValueError):
    """Raised when an experiment cannot be resolved without ambiguity."""


def canonical_json(value: Any) -> str:
    """Serialize a JSON value deterministically for durable fingerprints."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ExperimentContractError("experiment values must be finite JSON") from exc


def document_digest(value: Any) -> str:
    """Return the SHA-256 digest of one canonical JSON document."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ExperimentContractError(f"{field} must be a string identifier")
    result = value.strip()
    if not _IDENTIFIER.fullmatch(result):
        raise ExperimentContractError(f"{field} is not a valid identifier")
    return result


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ExperimentContractError(f"{field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    else:
        raise ExperimentContractError(f"{field} must be an integer")
    if result < minimum or result > maximum:
        raise ExperimentContractError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return result


def _finite_number(
    value: Any, field: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentContractError(f"{field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExperimentContractError(f"{field} must be a number") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ExperimentContractError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return result


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ExperimentContractError(
            f"{field} contains unsupported fields: {', '.join(unknown)}"
        )


def _analysis_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ExperimentContractError("analysis must be an object")
    _reject_unknown(raw, {
        "minimumSampleSizePerArm", "maximumAssignmentUnits",
        "minimumPricingCoverage", "confidence",
        "srmAlpha", "qualityNoninferiorityMargin",
        "maximumLatencyRegressionRatio", "stoppingRule",
    }, "analysis")
    minimum_sample_size = _integer(
            raw.get("minimumSampleSizePerArm", 20),
            "analysis.minimumSampleSizePerArm", minimum=2, maximum=100_000,
        )
    analysis = {
        "minimumSampleSizePerArm": minimum_sample_size,
        "maximumAssignmentUnits": _integer(
            raw.get("maximumAssignmentUnits", minimum_sample_size * 2),
            "analysis.maximumAssignmentUnits", minimum=4, maximum=10_000_000,
        ),
        "minimumPricingCoverage": _finite_number(
            raw.get("minimumPricingCoverage", 1.0),
            "analysis.minimumPricingCoverage", minimum=0.5, maximum=1.0,
        ),
        "confidence": _finite_number(
            raw.get("confidence", 0.95), "analysis.confidence",
            minimum=0.8, maximum=0.999,
        ),
        "srmAlpha": _finite_number(
            raw.get("srmAlpha", 0.001), "analysis.srmAlpha",
            minimum=0.000001, maximum=0.1,
        ),
        "qualityNoninferiorityMargin": _finite_number(
            raw.get("qualityNoninferiorityMargin", 0.05),
            "analysis.qualityNoninferiorityMargin", minimum=0.0, maximum=0.5,
        ),
        "maximumLatencyRegressionRatio": _finite_number(
            raw.get("maximumLatencyRegressionRatio", 1.2),
            "analysis.maximumLatencyRegressionRatio", minimum=0.1, maximum=10.0,
        ),
        "stoppingRule": str(raw.get("stoppingRule") or "fixed_horizon"),
    }
    if analysis["stoppingRule"] != "fixed_horizon":
        raise ExperimentContractError("only fixed_horizon stopping is supported in v1")
    return analysis


def _optional_plugin_version(raw: Mapping[str, Any], field: str) -> str | None:
    value = raw.get("pluginVersion")
    if value is None:
        return None
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ExperimentContractError(f"{field}.pluginVersion is invalid")
    return value


def _metric_reference(
    raw: Any, field: str, *, allow_version: bool = False
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ExperimentContractError(f"{field} must be an object")
    allowed = {"pluginId", "metricId"}
    if allow_version:
        allowed.add("pluginVersion")
    _reject_unknown(raw, allowed, field)
    reference = {
        "pluginId": _identifier(raw.get("pluginId"), f"{field}.pluginId"),
        "metricId": _identifier(raw.get("metricId"), f"{field}.metricId"),
    }
    if allow_version:
        version = _optional_plugin_version(raw, field)
        if version is not None:
            reference["pluginVersion"] = version
    return reference


def resolve_experiment_spec(
    raw: Mapping[str, Any], *, provider_registry: Any | None = None
) -> dict[str, Any]:
    """Resolve provider configs and return one immutable experiment document.

    ``provider_registry`` defaults to the process experiment registry.  Every
    referenced provider must exist at resolution time; optional discovery may
    fail softly, but an active specification never silently loses a strategy,
    metric, or analyzer.
    """
    if not isinstance(raw, Mapping):
        raise ExperimentContractError("experiment specification must be an object")
    _reject_unknown(raw, {
        "contractVersion", "experimentId", "assignmentUnit", "enrollmentBps",
        "arms", "metrics", "primaryMetric", "analyzer", "analysis",
    }, "experiment specification")
    if raw.get("contractVersion") not in (None, CONTRACT_VERSION):
        raise ExperimentContractError("unsupported experiment contractVersion")
    if provider_registry is None:
        from .registry import registry

        provider_registry = registry()

    experiment_id = _identifier(raw.get("experimentId"), "experimentId")
    assignment_unit = str(raw.get("assignmentUnit") or "conversation")
    if assignment_unit not in {"conversation", "task", "user"}:
        raise ExperimentContractError("assignmentUnit is unsupported")
    enrollment_bps = _integer(
        raw.get("enrollmentBps", 1000),
        "enrollmentBps",
        minimum=0,
        maximum=10_000,
    )
    raw_arms = raw.get("arms")
    if not isinstance(raw_arms, Sequence) or isinstance(raw_arms, (str, bytes)):
        raise ExperimentContractError("arms must be an array")
    if len(raw_arms) < 2 or len(raw_arms) > 16:
        raise ExperimentContractError("arms must contain between 2 and 16 entries")

    resolved_arms: list[dict[str, Any]] = []
    arm_ids: set[str] = set()
    allocation_total = 0
    for index, raw_arm in enumerate(raw_arms):
        field = f"arms[{index}]"
        if not isinstance(raw_arm, Mapping):
            raise ExperimentContractError(f"{field} must be an object")
        _reject_unknown(raw_arm, {"id", "allocationBps", "strategy"}, field)
        arm_id = _identifier(raw_arm.get("id"), f"{field}.id")
        if arm_id in arm_ids:
            raise ExperimentContractError("arm identifiers must be unique")
        arm_ids.add(arm_id)
        allocation = _integer(
            raw_arm.get("allocationBps"),
            f"{field}.allocationBps",
            minimum=0,
            maximum=10_000,
        )
        allocation_total += allocation
        strategy_ref = raw_arm.get("strategy")
        if not isinstance(strategy_ref, Mapping):
            raise ExperimentContractError(f"{field}.strategy must be an object")
        _reject_unknown(
            strategy_ref, {"pluginId", "strategyId", "pluginVersion", "config"},
            f"{field}.strategy",
        )
        plugin_id = _identifier(
            strategy_ref.get("pluginId"), f"{field}.strategy.pluginId"
        )
        strategy_id = _identifier(
            strategy_ref.get("strategyId"), f"{field}.strategy.strategyId"
        )
        provider = provider_registry.require_strategy(
            plugin_id, strategy_id,
            _optional_plugin_version(strategy_ref, f"{field}.strategy"),
        )
        resolved_config = provider.resolve_config(strategy_ref.get("config") or {})
        resolved_arms.append(
            {
                "id": arm_id,
                "allocationBps": allocation,
                "strategy": {
                    "pluginId": provider.plugin_id,
                    "strategyId": provider.strategy_id,
                    "pluginVersion": provider.version,
                    "implementationDigest": provider.implementation_digest,
                    "config": json.loads(canonical_json(resolved_config)),
                },
            }
        )
    if allocation_total != 10_000:
        raise ExperimentContractError("arm allocationBps values must total 10000")

    metrics_raw = raw.get("metrics")
    if not isinstance(metrics_raw, Sequence) or isinstance(
        metrics_raw, (str, bytes)
    ) or not metrics_raw:
        raise ExperimentContractError("metrics must be a non-empty array")
    resolved_metrics: list[dict[str, Any]] = []
    metric_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(metrics_raw):
        reference = _metric_reference(
            item, f"metrics[{index}]", allow_version=True
        )
        key = (reference["pluginId"], reference["metricId"])
        if key in metric_keys:
            raise ExperimentContractError("metric references must be unique")
        metric_keys.add(key)
        metric = provider_registry.require_metric(
            *key, reference.get("pluginVersion")
        )
        resolved_metrics.append(
            {
                "pluginId": reference["pluginId"],
                "metricId": reference["metricId"],
                "pluginVersion": metric.version,
                "implementationDigest": metric.implementation_digest,
                "unit": metric.unit,
                "direction": metric.direction,
            }
        )

    primary_metric = _metric_reference(raw.get("primaryMetric"), "primaryMetric")
    if (primary_metric["pluginId"], primary_metric["metricId"]) not in metric_keys:
        raise ExperimentContractError("primaryMetric must appear in metrics")

    analyzer_raw = raw.get("analyzer")
    if not isinstance(analyzer_raw, Mapping):
        raise ExperimentContractError("analyzer must be an object")
    _reject_unknown(
        analyzer_raw, {"pluginId", "analyzerId", "pluginVersion"}, "analyzer"
    )
    analyzer_plugin_id = _identifier(
        analyzer_raw.get("pluginId"), "analyzer.pluginId"
    )
    analyzer_id = _identifier(analyzer_raw.get("analyzerId"), "analyzer.analyzerId")
    analyzer = provider_registry.require_analyzer(
        analyzer_plugin_id, analyzer_id,
        _optional_plugin_version(analyzer_raw, "analyzer"),
    )

    analysis = _analysis_plan(raw.get("analysis"))
    positive_arm_count = sum(
        int(item["allocationBps"]) > 0 for item in resolved_arms
    )
    if analysis["maximumAssignmentUnits"] < (
        analysis["minimumSampleSizePerArm"] * positive_arm_count
    ):
        raise ExperimentContractError(
            "analysis.maximumAssignmentUnits cannot satisfy the per-arm minimum"
        )

    resolved = {
        "contractVersion": CONTRACT_VERSION,
        "experimentId": experiment_id,
        "assignmentUnit": assignment_unit,
        "assignmentAlgorithm": ASSIGNMENT_ALGORITHM,
        "enrollmentBps": enrollment_bps,
        "arms": resolved_arms,
        "metrics": resolved_metrics,
        "primaryMetric": primary_metric,
        "analyzer": {
            "pluginId": analyzer.plugin_id,
            "analyzerId": analyzer.analyzer_id,
            "pluginVersion": analyzer.version,
            "implementationDigest": analyzer.implementation_digest,
        },
        "analysis": analysis,
    }
    resolved["specDigest"] = document_digest(resolved)
    return resolved


def _require_exact_fields(
    raw: Mapping[str, Any], required: set[str], field: str
) -> None:
    _reject_unknown(raw, required, field)
    missing = sorted(required.difference(raw))
    if missing:
        raise ExperimentContractError(
            f"{field} is missing required fields: {', '.join(missing)}"
        )


def _validate_provider_identity(
    raw: Any, *, field: str, capability_field: str,
    extra_fields: set[str] | None = None,
) -> None:
    if not isinstance(raw, Mapping):
        raise ExperimentContractError(f"{field} must be an object")
    required = {
        "pluginId", capability_field, "pluginVersion", "implementationDigest",
        *(extra_fields or set()),
    }
    _require_exact_fields(raw, required, field)
    _identifier(raw["pluginId"], f"{field}.pluginId")
    _identifier(raw[capability_field], f"{field}.{capability_field}")
    version = raw["pluginVersion"]
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", version
    ):
        raise ExperimentContractError(f"{field}.pluginVersion is invalid")
    digest = raw["implementationDigest"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ExperimentContractError(
            f"{field}.implementationDigest must be SHA-256"
        )


def _validate_resolved_document(document: Mapping[str, Any]) -> None:
    root_fields = {
        "contractVersion", "experimentId", "assignmentUnit",
        "assignmentAlgorithm", "enrollmentBps", "arms", "metrics",
        "primaryMetric", "analyzer", "analysis",
    }
    _require_exact_fields(document, root_fields, "resolved specification")
    if document["contractVersion"] != CONTRACT_VERSION:
        raise ExperimentContractError("unsupported experiment contractVersion")
    _identifier(document["experimentId"], "experimentId")
    if document["assignmentUnit"] not in {"conversation", "task", "user"}:
        raise ExperimentContractError("assignmentUnit is unsupported")
    if document["assignmentAlgorithm"] != ASSIGNMENT_ALGORITHM:
        raise ExperimentContractError("assignmentAlgorithm is unsupported")
    _integer(document["enrollmentBps"], "enrollmentBps", minimum=0,
             maximum=10_000)

    arms = document["arms"]
    if not isinstance(arms, Sequence) or isinstance(arms, (str, bytes)):
        raise ExperimentContractError("arms must be an array")
    if len(arms) < 2 or len(arms) > 16:
        raise ExperimentContractError("arms must contain between 2 and 16 entries")
    arm_ids: set[str] = set()
    allocation_total = 0
    for index, arm in enumerate(arms):
        field = f"arms[{index}]"
        if not isinstance(arm, Mapping):
            raise ExperimentContractError(f"{field} must be an object")
        _require_exact_fields(arm, {"id", "allocationBps", "strategy"}, field)
        arm_id = _identifier(arm["id"], f"{field}.id")
        if arm_id in arm_ids:
            raise ExperimentContractError("arm identifiers must be unique")
        arm_ids.add(arm_id)
        allocation_total += _integer(
            arm["allocationBps"], f"{field}.allocationBps",
            minimum=0, maximum=10_000,
        )
        strategy = arm["strategy"]
        _validate_provider_identity(
            strategy, field=f"{field}.strategy", capability_field="strategyId",
            extra_fields={"config"},
        )
        if not isinstance(strategy["config"], Mapping):
            raise ExperimentContractError(f"{field}.strategy.config must be an object")
        canonical_json(strategy["config"])
    if allocation_total != 10_000:
        raise ExperimentContractError("arm allocationBps values must total 10000")

    metrics = document["metrics"]
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)) \
            or not metrics:
        raise ExperimentContractError("metrics must be a non-empty array")
    metric_keys: set[tuple[str, str]] = set()
    for index, metric in enumerate(metrics):
        field = f"metrics[{index}]"
        _validate_provider_identity(
            metric, field=field, capability_field="metricId",
            extra_fields={"unit", "direction"},
        )
        key = (str(metric["pluginId"]), str(metric["metricId"]))
        if key in metric_keys:
            raise ExperimentContractError("metric references must be unique")
        metric_keys.add(key)
        if not isinstance(metric["unit"], str) or not metric["unit"]:
            raise ExperimentContractError(f"{field}.unit is required")
        if metric["direction"] not in {"increase", "decrease", "guardrail"}:
            raise ExperimentContractError(f"{field}.direction is unsupported")

    primary = _metric_reference(document["primaryMetric"], "primaryMetric")
    if (primary["pluginId"], primary["metricId"]) not in metric_keys:
        raise ExperimentContractError("primaryMetric must appear in metrics")
    _validate_provider_identity(
        document["analyzer"], field="analyzer", capability_field="analyzerId"
    )
    analysis = document["analysis"]
    if not isinstance(analysis, Mapping):
        raise ExperimentContractError("analysis must be an object")
    _require_exact_fields(analysis, {
        "minimumSampleSizePerArm", "maximumAssignmentUnits",
        "minimumPricingCoverage", "confidence",
        "srmAlpha", "qualityNoninferiorityMargin",
        "maximumLatencyRegressionRatio", "stoppingRule",
    }, "analysis")
    normalized_analysis = _analysis_plan(analysis)
    positive_arm_count = sum(
        int(item["allocationBps"]) > 0 for item in arms
    )
    if normalized_analysis["maximumAssignmentUnits"] < (
        normalized_analysis["minimumSampleSizePerArm"] * positive_arm_count
    ):
        raise ExperimentContractError(
            "analysis.maximumAssignmentUnits cannot satisfy the per-arm minimum"
        )


def validate_resolved_spec(
    raw: Mapping[str, Any], *, expected_digest: str | None = None
) -> dict[str, Any]:
    """Verify a resolved specification without consulting installed plugins."""
    if not isinstance(raw, Mapping):
        raise ExperimentContractError("resolved specification must be an object")
    document = dict(raw)
    supplied_digest = str(document.pop("specDigest", ""))
    if not _DIGEST.fullmatch(supplied_digest):
        raise ExperimentContractError("specDigest must be a SHA-256 digest")
    _validate_resolved_document(document)
    computed = document_digest(document)
    if computed != supplied_digest:
        raise ExperimentContractError("resolved experiment specDigest mismatch")
    if expected_digest is not None and supplied_digest != expected_digest:
        raise ExperimentContractError("experiment specification changed under one ID")
    return {**json.loads(canonical_json(document)), "specDigest": supplied_digest}


__all__ = [
    "ASSIGNMENT_ALGORITHM",
    "CONTRACT_VERSION",
    "ExperimentContractError",
    "canonical_json",
    "document_digest",
    "resolve_experiment_spec",
    "validate_resolved_spec",
]
