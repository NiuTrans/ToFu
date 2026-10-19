"""Task-schema admission for the pinned Harbor execution adapter.

An old parser must not silently drop separate-verifier fields or collect hooks.
This necessary check does not replace backend preflight and oracle execution.
"""
from __future__ import annotations

from evaluations.catalog import BenchmarkDefinition


def require_harbor_capabilities(definition: BenchmarkDefinition) -> None:
    if not definition.required_capabilities:
        return
    from harbor.models.task.config import TaskConfig
    parsed = TaskConfig.model_validate({
        "schema_version": "1.3", "task": {"name": "tofu-evaluation/capability-probe"},
        "artifacts": ["/model.patch"],
        "verifier": {"environment_mode": "separate",
                     "environment": {"cpus": 1, "memory_mb": 512, "storage_mb": 1024},
                     "collect": [{"command": "true", "timeout_sec": 1}]},
    })
    verifier = parsed.model_dump(mode="json").get("verifier", {})
    if "separate-verifier" in definition.required_capabilities \
            and verifier.get("environment_mode") != "separate":
        raise ValueError("Harbor does not retain the separate verifier contract")
    if "verifier-collect" in definition.required_capabilities \
            and not any(row.get("command") == "true" for row in verifier.get("collect", [])):
        raise ValueError("Harbor does not retain verifier collect hooks")
