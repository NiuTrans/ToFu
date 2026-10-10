"""Context-cost request strategies and their independently pinned digest.

Entry point: :func:`strategy_providers`.  This module knows request policy
fields but has no dependency on experiment specs, analyzers, routes, or stores.
Keeping it separate means an analysis-plan edit cannot invalidate a persisted
strategy implementation fingerprint.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .registry import StrategyProvider, implementation_digest


CONTROL_POLICY = {"mcpToolExposure": "inline", "workingSetTokens": 0}
OPTIMIZED_POLICY = {"mcpToolExposure": "auto", "workingSetTokens": 128_000}
_IMPLEMENTATION_DIGEST = implementation_digest(Path(__file__))


def _fixed_policy(expected: Mapping[str, Any]):
    def resolve(raw: Mapping[str, Any]) -> dict[str, Any]:
        supplied = dict(raw)
        if supplied and supplied != dict(expected):
            raise ValueError("context-cost arm policy is fixed by its plugin version")
        return dict(expected)

    return resolve


def _request_conflict(config: Mapping[str, Any]) -> str | None:
    if "mcpToolExposure" in config:
        return "request_override"
    compaction = config.get("compaction")
    if isinstance(compaction, Mapping) and "workingSetTokens" in compaction:
        return "request_override"
    return None


def _apply_policy(config: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    updated = dict(config)
    updated["mcpToolExposure"] = str(policy["mcpToolExposure"])
    compaction = dict(config.get("compaction") or {})
    compaction["workingSetTokens"] = int(policy["workingSetTokens"])
    updated["compaction"] = compaction
    return updated


def strategy_providers(plugin_id: str, version: str) -> tuple[StrategyProvider, ...]:
    """Build the two fixed strategy providers for one plugin version."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mcpToolExposure": {"enum": ["inline", "auto"]},
            "workingSetTokens": {"type": "integer", "minimum": 0},
        },
    }
    return (
        StrategyProvider(
            plugin_id, "control", version, _IMPLEMENTATION_DIGEST,
            "Inline MCP schemas and window-safety-only compaction",
            schema, _fixed_policy(CONTROL_POLICY), _request_conflict, _apply_policy,
        ),
        StrategyProvider(
            plugin_id, "optimized", version, _IMPLEMENTATION_DIGEST,
            "On-demand MCP disclosure and a 128K economic working set",
            schema, _fixed_policy(OPTIMIZED_POLICY), _request_conflict, _apply_policy,
        ),
    )


__all__ = ["CONTROL_POLICY", "OPTIMIZED_POLICY", "strategy_providers"]
