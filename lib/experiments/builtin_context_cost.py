"""Built-in context-cost strategy, metric, and analyzer plugin.

The plugin owns the two bounded request policies and their analysis plan.  The
legacy settings adapter builds immutable specifications that reference these
providers; core experiment code never names MCP or compaction request fields.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .context_cost_metrics import metric_providers
from .context_cost_strategy import (
    CONTROL_POLICY,
    OPTIMIZED_POLICY,
    strategy_providers,
)
from .decision import analyze_context_cost
from .registry import (
    AnalyzerProvider,
    ExperimentPlugin,
    implementation_digest,
)


PLUGIN_ID = "tofu.context-cost"
PLUGIN_VERSION = "1.1.0"
_ANALYZER_DIGEST = implementation_digest(Path(__file__).with_name("decision.py"))


def plugin() -> ExperimentPlugin:
    """Return the immutable built-in capability bundle."""
    return ExperimentPlugin(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        description="Context exposure and economic working-set experiment",
        strategies=strategy_providers(PLUGIN_ID, PLUGIN_VERSION),
        metrics=metric_providers(PLUGIN_ID, PLUGIN_VERSION),
        analyzers=(
            AnalyzerProvider(
                PLUGIN_ID, "clustered-bootstrap-v1", PLUGIN_VERSION,
                _ANALYZER_DIGEST,
                "Conversation-clustered fixed-horizon promotion analysis",
                analyze_context_cost,
            ),
        ),
    )


def context_cost_spec(
    *, experiment_id: str, traffic_percent: int, treatment_percent: int,
    minimum_sample_size: int,
) -> dict[str, Any]:
    """Resolve the maintained two-arm specification through the registry."""
    from .contracts import resolve_experiment_spec

    treatment_bps = int(treatment_percent) * 100
    positive_allocations = [
        allocation for allocation in (10_000 - treatment_bps, treatment_bps)
        if allocation > 0
    ]
    # Twice the expectation required for the smallest arm makes an underfilled
    # fixed cohort unlikely while keeping the stopping point immutable.
    assignment_horizon = math.ceil(
        int(minimum_sample_size) * 20_000 / min(positive_allocations)
    )
    raw = {
        "experimentId": experiment_id,
        "assignmentUnit": "conversation",
        "enrollmentBps": int(traffic_percent) * 100,
        "arms": [
            {
                "id": "control",
                "allocationBps": 10_000 - treatment_bps,
                "strategy": {
                    "pluginId": PLUGIN_ID,
                    "strategyId": "control",
                    "pluginVersion": PLUGIN_VERSION,
                    "config": dict(CONTROL_POLICY),
                },
            },
            {
                "id": "optimized",
                "allocationBps": treatment_bps,
                "strategy": {
                    "pluginId": PLUGIN_ID,
                    "strategyId": "optimized",
                    "pluginVersion": PLUGIN_VERSION,
                    "config": dict(OPTIMIZED_POLICY),
                },
            },
        ],
        "metrics": [
            {"pluginId": PLUGIN_ID, "metricId": "cost.usd",
             "pluginVersion": PLUGIN_VERSION},
            {"pluginId": PLUGIN_ID, "metricId": "quality.oracle_passed",
             "pluginVersion": PLUGIN_VERSION},
            {"pluginId": PLUGIN_ID, "metricId": "latency.ms",
             "pluginVersion": PLUGIN_VERSION},
            {"pluginId": PLUGIN_ID,
             "metricId": "health.terminal_without_error",
             "pluginVersion": PLUGIN_VERSION},
        ],
        "primaryMetric": {"pluginId": PLUGIN_ID, "metricId": "cost.usd"},
        "analyzer": {
            "pluginId": PLUGIN_ID,
            "analyzerId": "clustered-bootstrap-v1",
            "pluginVersion": PLUGIN_VERSION,
        },
        "analysis": {
            "minimumSampleSizePerArm": int(minimum_sample_size),
            "maximumAssignmentUnits": assignment_horizon,
            "minimumPricingCoverage": 1.0,
            "confidence": 0.95,
            "srmAlpha": 0.001,
            "qualityNoninferiorityMargin": 0.05,
            "maximumLatencyRegressionRatio": 1.2,
            "stoppingRule": "fixed_horizon",
        },
    }
    return resolve_experiment_spec(raw)


__all__ = [
    "CONTROL_POLICY",
    "OPTIMIZED_POLICY",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "context_cost_spec",
    "plugin",
]
