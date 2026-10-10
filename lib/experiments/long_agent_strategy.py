"""Fixed request-local strategies for long-agent single-factor experiments."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from .registry import StrategyProvider, implementation_digest


CONTROL_POLICY = {
    "promptProfile": "full",
    "schemaBudgetTokens": 0,
    "resultEnvelope": "legacy",
    "contextBudgetTokens": 0,
    "compactionStrategy": "fixed",
    "orchestrationPolicy": "v1",
}


def _candidate(**changes: Any) -> dict[str, Any]:
    return {**CONTROL_POLICY, **changes}


LONG_AGENT_POLICIES = {
    "control": CONTROL_POLICY,
    "prompt_lean_kimi": _candidate(promptProfile="lean"),
    "tool_surface_v2": _candidate(schemaBudgetTokens=4_000),
    "tool_result_v2": _candidate(resultEnvelope="v2"),
    "context_budget_64k_v2": _candidate(contextBudgetTokens=64_000),
    "context_budget_v2": _candidate(contextBudgetTokens=96_000),
    "context_budget_128k_v2": _candidate(contextBudgetTokens=128_000),
    "adaptive_compaction_v2": _candidate(compactionStrategy="adaptive"),
    "orchestration_v2": _candidate(orchestrationPolicy="v2"),
    "prompt_ablate_url": _candidate(promptProfile="lean_no_url"),
    "prompt_ablate_safety": _candidate(promptProfile="lean_no_safety"),
    "prompt_ablate_tools": _candidate(promptProfile="lean_no_tools"),
    "prompt_ablate_output": _candidate(promptProfile="lean_no_output"),
    "prompt_ablate_autonomy": _candidate(promptProfile="lean_no_autonomy"),
    "combined_v2": {
        "promptProfile": "lean",
        "schemaBudgetTokens": 4_000,
        "resultEnvelope": "v2",
        "contextBudgetTokens": 96_000,
        "compactionStrategy": "adaptive",
        "orchestrationPolicy": "v2",
    },
}
_IMPLEMENTATION_DIGEST = implementation_digest(Path(__file__))


def _resolve_fixed(expected: Mapping[str, Any]):
    def resolve(raw: Mapping[str, Any]) -> dict[str, Any]:
        supplied = dict(raw)
        if supplied and supplied != dict(expected):
            raise ValueError("long-agent arm policy is fixed by plugin version")
        return deepcopy(dict(expected))
    return resolve


def _conflict(config: Mapping[str, Any]) -> str | None:
    nested_fields = (
        ("responses", "promptProfile"),
        ("tools", "schemaBudgetTokens"),
        ("tools", "resultEnvelope"),
        ("context", "globalBudgetTokens"),
        ("compaction", "strategy"),
        ("orchestration", "policy"),
    )
    for owner, field in nested_fields:
        value = config.get(owner)
        if isinstance(value, Mapping) and field in value:
            return "request_override"
        if f"{owner}.{field}" in config:
            return "request_override"
    return None


def _apply(config: Mapping[str, Any], policy: Mapping[str, Any]
           ) -> dict[str, Any]:
    updated = deepcopy(dict(config))
    responses = dict(updated.get("responses") or {})
    tools = dict(updated.get("tools") or {})
    context = dict(updated.get("context") or {})
    compaction = dict(updated.get("compaction") or {})
    orchestration = dict(updated.get("orchestration") or {})
    responses["promptProfile"] = policy["promptProfile"]
    tools["schemaBudgetTokens"] = int(policy["schemaBudgetTokens"])
    tools["resultEnvelope"] = policy["resultEnvelope"]
    context["globalBudgetTokens"] = int(policy["contextBudgetTokens"])
    compaction["strategy"] = policy["compactionStrategy"]
    orchestration["policy"] = policy["orchestrationPolicy"]
    updated.update({
        "responses": responses,
        "tools": tools,
        "context": context,
        "compaction": compaction,
        "orchestration": orchestration,
    })
    return updated


def strategy_providers(plugin_id: str, version: str
                       ) -> tuple[StrategyProvider, ...]:
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return tuple(
        StrategyProvider(
            plugin_id, strategy_id, version, _IMPLEMENTATION_DIGEST,
            f"Fixed long-agent strategy: {strategy_id}", schema,
            _resolve_fixed(policy), _conflict, _apply,
        )
        for strategy_id, policy in LONG_AGENT_POLICIES.items()
    )


__all__ = ["CONTROL_POLICY", "LONG_AGENT_POLICIES", "strategy_providers"]
