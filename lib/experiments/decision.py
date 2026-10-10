"""Deterministic cluster-level inference for the built-in cost experiment.

The analyzer consumes one value per assignment unit, never raw turns.  It
refuses promotion when the source is truncated, fingerprints mix, exposure is
unverified, pricing is incomplete, the sample ratio is implausible, semantic
quality is not non-inferior, or the cost interval crosses zero.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any


def _finite_values(values: Any) -> list[float]:
    result: list[float] = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return result
    for value in values:
        if isinstance(value, bool):
            result.append(float(value))
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires observations")
    position = max(0.0, min(1.0, probability)) * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return (sorted_values[lower] * (1.0 - fraction)
            + sorted_values[upper] * fraction)


def bootstrap_mean_difference(
    control: Sequence[float],
    candidate: Sequence[float],
    *,
    confidence: float,
    seed: str,
    iterations: int = 2_000,
) -> dict[str, Any] | None:
    """Return a deterministic assignment-unit bootstrap for candidate-control."""
    left = _finite_values(control)
    right = _finite_values(candidate)
    if not left or not right:
        return None
    estimate = fmean(right) - fmean(left)
    random_seed = int.from_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big"
    )
    generator = random.Random(random_seed)
    samples: list[float] = []
    for _ in range(max(200, min(10_000, int(iterations)))):
        control_mean = sum(generator.choice(left) for _ in left) / len(left)
        candidate_mean = sum(generator.choice(right) for _ in right) / len(right)
        samples.append(candidate_mean - control_mean)
    samples.sort()
    alpha = 1.0 - confidence
    return {
        "estimate": round(estimate, 9),
        "lower": round(_percentile(samples, alpha / 2.0), 9),
        "upper": round(_percentile(samples, 1.0 - alpha / 2.0), 9),
        "confidence": confidence,
        "method": "assignment_unit_percentile_bootstrap",
        "controlUnits": len(left),
        "candidateUnits": len(right),
        "iterations": len(samples),
    }


def sample_ratio_mismatch(
    observed: Mapping[str, int], expected_bps: Mapping[str, int], *, alpha: float
) -> dict[str, Any]:
    """Return the chi-square SRM diagnostic for one multinomial allocation."""
    names = [name for name, bps in expected_bps.items() if bps > 0]
    total = sum(max(0, int(observed.get(name, 0))) for name in names)
    if total <= 0 or len(names) < 2:
        return {
            "tested": False,
            "mismatch": False,
            "pValue": None,
            "alpha": alpha,
            "observed": {name: int(observed.get(name, 0)) for name in names},
        }
    denominator = sum(expected_bps[name] for name in names)
    chi_square = 0.0
    expected_counts: dict[str, float] = {}
    for name in names:
        expected = total * expected_bps[name] / denominator
        expected_counts[name] = expected
        if expected > 0:
            chi_square += (int(observed.get(name, 0)) - expected) ** 2 / expected
    # Built-in v1 has exactly two arms (one degree of freedom).
    p_value = math.erfc(math.sqrt(max(0.0, chi_square) / 2.0))
    tested = bool(
        len(names) == 2 and all(value >= 5.0 for value in expected_counts.values())
    )
    return {
        "tested": tested,
        "mismatch": bool(tested and p_value < alpha),
        "pValue": round(p_value, 9),
        "alpha": alpha,
        "chiSquare": round(chi_square, 6),
        "observed": {name: int(observed.get(name, 0)) for name in names},
        "expected": {name: round(value, 3)
                     for name, value in expected_counts.items()},
    }


def _p90(values: Sequence[float]) -> float | None:
    rows = sorted(_finite_values(values))
    if not rows:
        return None
    return rows[max(0, math.ceil(0.9 * len(rows)) - 1)]


def analyze_context_cost(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Analyze the two-arm context-cost experiment without silent fallbacks."""
    spec = payload.get("spec")
    arms = payload.get("arms")
    if not isinstance(spec, Mapping) or not isinstance(arms, Mapping):
        raise ValueError("context-cost analyzer requires spec and arm observations")
    analysis = spec.get("analysis")
    if not isinstance(analysis, Mapping):
        raise ValueError("context-cost spec has no analysis plan")
    control = arms.get("control")
    candidate = arms.get("optimized")
    if not isinstance(control, Mapping) or not isinstance(candidate, Mapping):
        raise ValueError("context-cost analyzer requires control and optimized arms")

    minimum = int(analysis["minimumSampleSizePerArm"])
    confidence = float(analysis["confidence"])
    coverage_floor = float(analysis["minimumPricingCoverage"])
    margin = float(analysis["qualityNoninferiorityMargin"])
    latency_ceiling = float(analysis["maximumLatencyRegressionRatio"])
    fixed_horizon = int(analysis["maximumAssignmentUnits"])
    digest = str(spec.get("specDigest") or "")

    assigned = {
        "control": max(0, int(control.get("assignedUnits") or 0)),
        "optimized": max(0, int(candidate.get("assignedUnits") or 0)),
    }
    allocations = {
        str(item.get("id")): int(item.get("allocationBps") or 0)
        for item in spec.get("arms") or [] if isinstance(item, Mapping)
    }
    srm = sample_ratio_mismatch(
        assigned, allocations, alpha=float(analysis["srmAlpha"])
    )
    control_costs = _finite_values(control.get("fullyPricedCosts"))
    candidate_costs = _finite_values(candidate.get("fullyPricedCosts"))
    control_quality = _finite_values(control.get("qualityByUnit"))
    candidate_quality = _finite_values(candidate.get("qualityByUnit"))
    cost_interval = bootstrap_mean_difference(
        control_costs, candidate_costs, confidence=confidence,
        seed=f"{digest}:cost",
    )
    quality_interval = bootstrap_mean_difference(
        control_quality, candidate_quality, confidence=confidence,
        seed=f"{digest}:quality",
    )
    control_p90 = _p90(control.get("latencyByUnit") or [])
    candidate_p90 = _p90(candidate.get("latencyByUnit") or [])
    latency_ratio = (
        candidate_p90 / control_p90
        if candidate_p90 is not None and control_p90 not in (None, 0)
        else None
    )

    invalid_reasons: list[str] = []
    analysis_start_verified = bool(payload.get("analysisStartVerified"))
    if not analysis_start_verified:
        invalid_reasons.append("analysis_start_unknown")
    analysis_seal_verified = bool(payload.get("analysisSealVerified"))
    if bool(payload.get("analysisClosed")) and not analysis_seal_verified:
        invalid_reasons.append("analysis_seal_unknown")
    if bool(payload.get("truncated")):
        invalid_reasons.append("truncated_source")
    if int(payload.get("invalidRows") or 0) > 0:
        invalid_reasons.append("invalid_outcomes")
    observed_digests = {
        str(value) for value in payload.get("observedSpecDigests") or [] if value
    }
    if int(payload.get("unversionedOutcomes") or 0) > 0:
        invalid_reasons.append("unversioned_outcomes")
    if observed_digests and observed_digests != {digest}:
        invalid_reasons.append("mixed_spec_digests")
    if int(payload.get("unverifiedExposures") or 0) > 0:
        invalid_reasons.append("unverified_exposures")
    if int(payload.get("pendingExposures") or 0) > 0:
        invalid_reasons.append("pending_exposures")
    if int(payload.get("crossArmUnits") or 0) > 0:
        invalid_reasons.append("cross_arm_assignment")
    if int(payload.get("metricExtractionErrors") or 0) > 0:
        invalid_reasons.append("metric_extraction_failed")
    if srm["mismatch"]:
        invalid_reasons.append("sample_ratio_mismatch")

    sample_ready = all(value >= minimum for value in assigned.values())
    pricing_coverage = {
        "control": float(control.get("pricingCoverage") or 0.0),
        "optimized": float(candidate.get("pricingCoverage") or 0.0),
    }
    pricing_ready = all(value >= coverage_floor
                        for value in pricing_coverage.values())
    quality_ready = (
        len(control_quality) >= minimum and len(candidate_quality) >= minimum
    )
    latency_ready = control_p90 is not None and candidate_p90 is not None
    srm_ready = bool(srm["tested"])
    analysis_closed = bool(payload.get("analysisClosed"))
    fixed_horizon_reached = bool(payload.get("fixedHorizonReached"))
    quality_noninferior = bool(
        quality_interval is not None and quality_interval["lower"] >= -margin
    )
    cost_reduction_established = bool(
        cost_interval is not None and cost_interval["upper"] < 0
    )
    latency_guardrail_passed = bool(
        latency_ratio is not None and latency_ratio <= latency_ceiling
    )

    blockers = list(invalid_reasons)
    if not analysis_closed:
        blockers.append("experiment_still_enrolling")
    if not fixed_horizon_reached:
        blockers.append("fixed_horizon_not_reached")
    if not sample_ready:
        blockers.append("insufficient_assigned_units")
    if not pricing_ready:
        blockers.append("incomplete_pricing")
    if not quality_ready:
        blockers.append("quality_not_measured")
    if not latency_ready:
        blockers.append("latency_not_measured")
    if sample_ready and not srm_ready:
        blockers.append("sample_ratio_diagnostic_not_ready")
    if quality_ready and not quality_noninferior:
        blockers.append("quality_noninferiority_not_established")
    if sample_ready and pricing_ready and not cost_reduction_established:
        blockers.append("cost_reduction_not_established")
    if latency_ready and not latency_guardrail_passed:
        blockers.append("latency_guardrail_failed")

    data_valid = not invalid_reasons
    decision_eligible = bool(
        data_valid and analysis_closed and fixed_horizon_reached
        and sample_ready and pricing_ready
        and quality_ready and latency_ready and srm_ready
    )
    promotion_eligible = bool(
        decision_eligible and quality_noninferior
        and cost_reduction_established and latency_guardrail_passed
    )
    if invalid_reasons:
        status = "invalid_data"
    elif (not analysis_closed or not fixed_horizon_reached
          or not sample_ready or not srm_ready):
        status = "collecting"
    elif not (pricing_ready and quality_ready and latency_ready):
        status = "missing_required_metrics"
    elif promotion_eligible:
        status = "promote"
    else:
        status = "do_not_promote"

    return {
        "contractVersion": "tofu.experiment-decision/v1",
        "status": status,
        "dataValid": data_valid,
        "sampleReady": sample_ready,
        "pricingReady": pricing_ready,
        "qualityReady": quality_ready,
        "latencyReady": latency_ready,
        "srmReady": srm_ready,
        "analysisClosed": analysis_closed,
        "analysisStartVerified": analysis_start_verified,
        "analysisSealVerified": analysis_seal_verified,
        "fixedHorizonReached": fixed_horizon_reached,
        "maximumAssignmentUnits": fixed_horizon,
        "decisionEligible": decision_eligible,
        "promotionEligible": promotion_eligible,
        "blockers": blockers,
        "srm": srm,
        "pricingCoverage": pricing_coverage,
        "costDifferenceUsd": cost_interval,
        "qualityDifference": quality_interval,
        "qualityNoninferiorityMargin": margin,
        "qualityNoninferior": quality_noninferior,
        "costReductionEstablished": cost_reduction_established,
        "latencyP90": {
            "controlMs": control_p90,
            "optimizedMs": candidate_p90,
            "ratio": round(latency_ratio, 6) if latency_ratio is not None else None,
            "maximumRegressionRatio": latency_ceiling,
            "guardrailPassed": latency_guardrail_passed,
        },
    }


__all__ = [
    "analyze_context_cost",
    "bootstrap_mean_difference",
    "sample_ratio_mismatch",
]
