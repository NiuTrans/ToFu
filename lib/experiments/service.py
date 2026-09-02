"""Deterministic experiment assignment and guarded strategy application.

This module is the runtime boundary between an immutable experiment spec and a
request.  It owns identity-aware bucketing and exposure state; strategy
plugins only inspect conflicts and produce a detached request configuration.
No route, task manager, or storage backend is imported here.
"""

from __future__ import annotations

import copy
import hashlib
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from lib.log import get_logger

from .contracts import validate_resolved_spec
from .registry import ExperimentPluginError, ExperimentRegistry, registry


logger = get_logger(__name__)


def _required_identity(value: Any, field: str) -> str:
    result = str(value if value is not None else "").strip()
    if not result:
        raise ValueError(f"{field} is required for experiment assignment")
    return result


def _subject_digest(*, owner_id: Any, assignment_unit: str,
                    unit_id: Any) -> str:
    owner = _required_identity(owner_id, "owner_id")
    unit = _required_identity(unit_id, "unit_id")
    material = f"{owner}\x00{assignment_unit}\x00{unit}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _bucket(experiment_id: str, lane: str, subject_digest: str) -> int:
    material = f"{experiment_id}\x00{lane}\x00{subject_digest}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 10_000


def _base_assignment(spec: Mapping[str, Any], subject_digest: str) -> dict[str, Any]:
    return {
        "contractVersion": str(spec["contractVersion"]),
        "experimentId": str(spec["experimentId"]),
        "specDigest": str(spec["specDigest"]),
        "assignmentUnit": str(spec["assignmentUnit"]),
        "assignmentAlgorithm": str(spec["assignmentAlgorithm"]),
        "subjectDigest": subject_digest,
        "status": "not_enrolled",
        "exposureStatus": "not_applicable",
    }


def _assign_resolved(
    resolved: Mapping[str, Any], *, owner_id: Any, unit_id: Any
) -> dict[str, Any]:
    subject_digest = _subject_digest(
        owner_id=owner_id,
        assignment_unit=str(resolved["assignmentUnit"]),
        unit_id=unit_id,
    )
    assignment = _base_assignment(resolved, subject_digest)
    enrollment_bucket = _bucket(
        str(resolved["experimentId"]), "enrollment", subject_digest
    )
    assignment["enrollmentBucket"] = enrollment_bucket
    if enrollment_bucket >= int(resolved["enrollmentBps"]):
        return assignment

    arm_bucket = _bucket(str(resolved["experimentId"]), "arm", subject_digest)
    allocation_end = 0
    selected: Mapping[str, Any] | None = None
    for arm in resolved["arms"]:
        allocation_end += int(arm["allocationBps"])
        if arm_bucket < allocation_end:
            selected = arm
            break
    if selected is None:  # Defensive: spec validation already requires 10_000 bps.
        raise ValueError("experiment arm allocation did not cover the assignment")

    strategy = copy.deepcopy(dict(selected["strategy"]))
    assignment.update({
        "status": "assigned",
        "exposureStatus": "pending",
        "arm": str(selected["id"]),
        "armBucket": arm_bucket,
        "strategy": strategy,
        # Kept as an explicit snapshot for auditability and the v1 cost facade.
        "policy": copy.deepcopy(dict(strategy["config"])),
    })
    return assignment


def assign_experiment(
    spec: Mapping[str, Any], *, owner_id: Any, unit_id: Any
) -> dict[str, Any]:
    """Return a deterministic assignment without applying its strategy.

    The owner is part of the bucketing subject so identical conversation/task
    identifiers from different principals cannot share an assignment.  Only a
    one-way subject digest is retained in telemetry.
    """
    return _assign_resolved(
        validate_resolved_spec(spec), owner_id=owner_id, unit_id=unit_id
    )


def _provider_matches(strategy: Mapping[str, Any], provider: Any) -> bool:
    return (
        str(strategy.get("pluginVersion") or "") == provider.version
        and str(strategy.get("implementationDigest") or "")
        == provider.implementation_digest
    )


def _require_provider_identity(reference: Mapping[str, Any], provider: Any) -> None:
    if not _provider_matches(reference, provider):
        raise ExperimentPluginError(
            "installed experiment capability differs from resolved specification"
        )


def _apply_assignment(
    assignment: dict[str, Any], request_config: Mapping[str, Any],
    provider_resolver: Callable[[Mapping[str, Any]], Any],
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    if assignment["status"] != "assigned":
        return request_config, assignment

    strategy = assignment["strategy"]
    try:
        provider = provider_resolver(strategy)
        _require_provider_identity(strategy, provider)
        conflict_reason = provider.conflict(request_config)
        if conflict_reason:
            assignment.update({
                "status": "excluded",
                "exposureStatus": "not_applied",
                "reason": str(conflict_reason),
            })
            return request_config, assignment
        updated = provider.apply(
            copy.deepcopy(dict(request_config)),
            copy.deepcopy(dict(strategy["config"])),
        )
        if not isinstance(updated, Mapping):
            raise ExperimentPluginError(
                "experiment strategy apply callback returned no object"
            )
    except Exception as exc:
        assignment.update({
            "status": "application_failed",
            "exposureStatus": "failed",
            "reason": "strategy_application_failed",
        })
        logger.error(
            "[Experiments] strategy application failed experiment=%s arm=%s: %s",
            assignment["experimentId"], assignment.get("arm"), exc,
            exc_info=True,
        )
        return request_config, assignment

    assignment.update({
        "exposureStatus": "applied",
        "exposedAt": int(time.time() * 1000),
    })
    return dict(updated), assignment


def apply_experiment(
    spec: Mapping[str, Any], *, owner_id: Any, unit_id: Any,
    request_config: Mapping[str, Any],
    provider_registry: ExperimentRegistry | None = None,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Assign and apply one strategy, failing safe with explicit contamination.

    A missing/mismatched provider or a plugin exception never breaks a user
    request.  The original request object is returned and the exposure record
    is marked ``application_failed`` so analysis refuses the contaminated run.
    """
    if not isinstance(request_config, Mapping):
        raise TypeError("request_config must be an object")
    assignment = assign_experiment(spec, owner_id=owner_id, unit_id=unit_id)
    providers = provider_registry or registry()
    return _apply_assignment(
        assignment, request_config,
        lambda strategy: providers.require_strategy(
            str(strategy["pluginId"]), str(strategy["strategyId"]),
            str(strategy["pluginVersion"]),
        ),
    )


def compile_experiment_application(
    spec: Mapping[str, Any], *,
    provider_registry: ExperimentRegistry | None = None,
) -> Callable[..., tuple[Mapping[str, Any], dict[str, Any]]]:
    """Compile a verified, reusable assignment/application hot-path plan."""
    resolved = validate_resolved_spec(spec)
    providers = provider_registry or registry()
    strategy_plan: dict[tuple[str, str, str], Any] = {}
    for arm in resolved["arms"]:
        reference = arm["strategy"]
        key = (
            str(reference["pluginId"]), str(reference["strategyId"]),
            str(reference["pluginVersion"]),
        )
        provider = providers.require_strategy(*key)
        _require_provider_identity(reference, provider)
        strategy_plan[key] = provider

    def apply(
        *, owner_id: Any, unit_id: Any, request_config: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        if not isinstance(request_config, Mapping):
            raise TypeError("request_config must be an object")
        assignment = _assign_resolved(
            resolved, owner_id=owner_id, unit_id=unit_id
        )
        return _apply_assignment(
            assignment, request_config,
            lambda reference: strategy_plan[
                (str(reference["pluginId"]), str(reference["strategyId"]),
                 str(reference["pluginVersion"]))
            ],
        )

    return apply


def compile_metric_extractor(
    spec: Mapping[str, Any], *,
    provider_registry: ExperimentRegistry | None = None,
) -> Callable[[Mapping[str, Any]], dict[str, float | bool | None]]:
    """Compile and pin a reusable metric extraction plan for one report scan.

    Spec hashing, registry lookup, and provider identity checks happen once.
    The returned pure callable still validates every plugin value, but avoids
    repeating locks and whole-document digest work for every persisted outcome.
    """
    resolved = validate_resolved_spec(spec)
    providers = provider_registry or registry()
    plan: list[tuple[str, Any]] = []
    for reference in resolved["metrics"]:
        provider = providers.require_metric(
            str(reference["pluginId"]), str(reference["metricId"]),
            str(reference["pluginVersion"]),
        )
        _require_provider_identity(reference, provider)
        plan.append((
            f"{reference['pluginId']}/{reference['metricId']}", provider
        ))

    def extract(outcome: Mapping[str, Any]) -> dict[str, float | bool | None]:
        if not isinstance(outcome, Mapping):
            raise TypeError("experiment outcome must be an object")
        result: dict[str, float | bool | None] = {}
        for key, provider in plan:
            value = provider.extract(outcome)
            if value is not None and not isinstance(value, (bool, int, float)):
                raise ExperimentPluginError(
                    "experiment metric extractor returned a non-numeric value"
                )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value)
                if not math.isfinite(value):
                    raise ExperimentPluginError(
                        "experiment metric extractor returned a non-finite value"
                    )
            result[key] = value
        return result

    return extract


def extract_metric_values(
    spec: Mapping[str, Any], outcome: Mapping[str, Any], *,
    provider_registry: ExperimentRegistry | None = None,
) -> dict[str, float | bool | None]:
    """Extract all resolved metrics from one outcome through plugin providers."""
    return compile_metric_extractor(
        spec, provider_registry=provider_registry
    )(outcome)


def analyze_experiment(
    spec: Mapping[str, Any], payload: Mapping[str, Any], *,
    provider_registry: ExperimentRegistry | None = None,
) -> dict[str, Any]:
    """Run the analyzer frozen into a spec after verifying its implementation."""
    if not isinstance(payload, Mapping):
        raise TypeError("experiment analyzer payload must be an object")
    resolved = validate_resolved_spec(spec)
    reference = resolved["analyzer"]
    providers = provider_registry or registry()
    provider = providers.require_analyzer(
        str(reference["pluginId"]), str(reference["analyzerId"]),
        str(reference["pluginVersion"]),
    )
    _require_provider_identity(reference, provider)
    result = provider.analyze({**copy.deepcopy(dict(payload)), "spec": resolved})
    if not isinstance(result, Mapping):
        raise ExperimentPluginError("experiment analyzer returned no object")
    return copy.deepcopy(dict(result))


__all__ = [
    "analyze_experiment",
    "apply_experiment",
    "assign_experiment",
    "compile_experiment_application",
    "compile_metric_extractor",
    "extract_metric_values",
]
