"""Built-in long-agent experiment strategies and maintained pilot spec."""

from __future__ import annotations

from typing import Any

from .long_agent_strategy import LONG_AGENT_POLICIES, strategy_providers
from .registry import ExperimentPlugin


PLUGIN_ID = "tofu.long-agent-v2"
PLUGIN_VERSION = "2.0.0"
COMBINED_REQUIRED_WINNERS = frozenset({
    "prompt_lean_kimi", "tool_surface_v2", "tool_result_v2",
    "context_budget_v2", "adaptive_compaction_v2", "orchestration_v2",
})
PROMPT_ABLATION_STRATEGIES = frozenset({
    "prompt_ablate_url", "prompt_ablate_safety", "prompt_ablate_tools",
    "prompt_ablate_output", "prompt_ablate_autonomy",
})


def plugin() -> ExperimentPlugin:
    return ExperimentPlugin(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        description="Long-agent context, tools, prompt, and orchestration arms",
        strategies=strategy_providers(PLUGIN_ID, PLUGIN_VERSION),
    )


def long_agent_spec(*, experiment_id: str, candidate_strategy: str,
                    enrollment_percent: int = 10,
                    minimum_sample_size: int = 20,
                    independently_winning: set[str] | None = None,
                    ) -> dict[str, Any]:
    """Build one pre-registered 50/50 single-factor pilot."""
    if candidate_strategy not in LONG_AGENT_POLICIES \
            or candidate_strategy == "control":
        raise ValueError("candidate_strategy must name a long-agent candidate")
    if not 1 <= int(enrollment_percent) <= 100:
        raise ValueError("enrollment_percent must be between 1 and 100")
    if int(minimum_sample_size) < 2:
        raise ValueError("minimum_sample_size must be at least 2 per arm")
    if candidate_strategy == "combined_v2" and not (
            COMBINED_REQUIRED_WINNERS
            <= frozenset(independently_winning or ())):
        missing = sorted(COMBINED_REQUIRED_WINNERS - frozenset(
            independently_winning or ()))
        raise ValueError(
            "combined_v2 requires independently winning mechanisms: "
            + ", ".join(missing))
    from .contracts import resolve_experiment_spec
    horizon = max(4, int(minimum_sample_size) * 2)
    baseline_strategy = (
        "prompt_lean_kimi"
        if candidate_strategy in PROMPT_ABLATION_STRATEGIES else "control")
    raw = {
        "experimentId": experiment_id,
        "assignmentUnit": "conversation",
        "enrollmentBps": int(enrollment_percent) * 100,
        "arms": [
            {
                "id": "control", "allocationBps": 5_000,
                "strategy": {
                    "pluginId": PLUGIN_ID, "strategyId": baseline_strategy,
                    "pluginVersion": PLUGIN_VERSION,
                    "config": dict(LONG_AGENT_POLICIES[baseline_strategy]),
                },
            },
            {
                "id": candidate_strategy, "allocationBps": 5_000,
                "strategy": {
                    "pluginId": PLUGIN_ID,
                    "strategyId": candidate_strategy,
                    "pluginVersion": PLUGIN_VERSION,
                    "config": dict(LONG_AGENT_POLICIES[candidate_strategy]),
                },
            },
        ],
        "metrics": [
            {"pluginId": "tofu.context-cost", "metricId": "cost.usd",
             "pluginVersion": "1.1.0"},
            {"pluginId": "tofu.context-cost",
             "metricId": "quality.oracle_passed", "pluginVersion": "1.1.0"},
            {"pluginId": "tofu.context-cost", "metricId": "latency.ms",
             "pluginVersion": "1.1.0"},
            {"pluginId": "tofu.context-cost",
             "metricId": "health.terminal_without_error",
             "pluginVersion": "1.1.0"},
        ],
        "primaryMetric": {
            "pluginId": "tofu.context-cost", "metricId": "cost.usd"},
        "analyzer": {
            "pluginId": "tofu.context-cost",
            "analyzerId": "clustered-bootstrap-v1",
            "pluginVersion": "1.1.0",
        },
        "analysis": {
            "minimumSampleSizePerArm": int(minimum_sample_size),
            "maximumAssignmentUnits": horizon,
            "minimumPricingCoverage": 1.0,
            "confidence": 0.95,
            "srmAlpha": 0.001,
            "qualityNoninferiorityMargin": 0.03,
            "maximumLatencyRegressionRatio": 0.85,
            "stoppingRule": "fixed_horizon",
        },
    }
    return resolve_experiment_spec(raw)


__all__ = [
    "COMBINED_REQUIRED_WINNERS", "PLUGIN_ID", "PLUGIN_VERSION",
    "PROMPT_ABLATION_STRATEGIES", "long_agent_spec", "plugin",
]
