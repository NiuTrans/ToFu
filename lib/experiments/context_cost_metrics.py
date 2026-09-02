"""Context-cost outcome metrics and their independently pinned digest.

Entry point: :func:`metric_providers`.  Extractors are pure and storage-agnostic;
the generic experiment runtime validates their numeric outputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .registry import MetricProvider, implementation_digest


_IMPLEMENTATION_DIGEST = implementation_digest(Path(__file__))


def _nested(outcome: Mapping[str, Any], section: str, field: str) -> Any:
    value = outcome.get(section)
    return value.get(field) if isinstance(value, Mapping) else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def metric_providers(plugin_id: str, version: str) -> tuple[MetricProvider, ...]:
    """Build the outcome metric providers for one plugin version."""
    return (
        MetricProvider(
            plugin_id, "cost.usd", version, _IMPLEMENTATION_DIGEST,
            "Provider-priced USD cost per assignment unit", "usd", "decrease",
            lambda outcome: _number(_nested(outcome, "metrics", "costUsd")),
        ),
        MetricProvider(
            plugin_id, "quality.oracle_passed", version, _IMPLEMENTATION_DIGEST,
            "Semantic task oracle", "boolean", "increase",
            lambda outcome: _nested(outcome, "quality", "oraclePassed"),
        ),
        MetricProvider(
            plugin_id, "latency.ms", version, _IMPLEMENTATION_DIGEST,
            "End-to-end task latency", "milliseconds", "guardrail",
            lambda outcome: _number(outcome.get("latencyMs")),
        ),
        MetricProvider(
            plugin_id, "health.terminal_without_error", version,
            _IMPLEMENTATION_DIGEST, "Operational completion proxy", "boolean",
            "guardrail", lambda outcome: _nested(
                outcome, "quality", "terminalWithoutError"
            ),
        ),
    )


__all__ = ["metric_providers"]
