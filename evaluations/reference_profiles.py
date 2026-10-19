"""Published reference settings; comparison explains drift and never certifies scores.

GLM-5.3 values come from the creator's Terminal-Bench 2.1 footnote. Missing
settings stay unknown. In particular, the model card does not specify the
attempt count or hardware, so the local 89x5 rule is not attributed to Z.ai.
"""
from __future__ import annotations
import re

from rootless_vm.harness_profiles import harness_profiles


GLM53_TB21 = {
    "id": "glm-5.3-tb21",
    "source_url": "https://huggingface.co/zai-org/GLM-5.3#footnotes",
    "reported_score_percent": 88.2,
    "agent": "claude-code",
    "agent_version": "2.1.207",
    "agent_timeout_seconds": 21600,
    "request_parameters": {"temperature": 1.0, "top_p": 1.0, "max_tokens": 65536},
}


def compare_glm53_reference(config: dict, report: dict) -> dict:
    agents = config.get("agents") or []
    if len(agents) != 1 or not isinstance(agents[0], dict):
        raise ValueError("reference comparison requires exactly one registered agent")
    agent = agents[0]
    kwargs = agent.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        raise ValueError("reference agent kwargs must be an object")
    known_agents = {name for profile in harness_profiles()
                    for name in (profile.profile_id, profile.harbor_agent, profile.agent_name)}
    name = agent.get("name")
    observed_agent = name if isinstance(name, str) and name in known_agents else None
    version = kwargs.get("version")
    observed_version = version if isinstance(version, str) and re.fullmatch(
        r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version) else None
    deadline = agent.get("override_timeout_sec", kwargs.get("timeout_sec"))
    observed_deadline = deadline if type(deadline) in (int, float) and 1 <= deadline <= 86400 else None
    dimensions = {}
    for key, expected, actual in (
        ("agent", GLM53_TB21["agent"], observed_agent),
        ("agent_version", GLM53_TB21["agent_version"], observed_version),
        ("agent_timeout_seconds", 21600, observed_deadline),
    ):
        dimensions[key] = {"expected": expected, "observed": actual,
                           "status": "unknown" if actual is None else
                           "match" if expected == actual else "mismatch"}
    for key, expected in GLM53_TB21["request_parameters"].items():
        rows = report.get("observed_request_parameters", [])
        counts = {"match": 0, "mismatch": 0, "unknown": 0}
        for row in rows:
            params = row["parameters"]
            actual = params.get(key)
            if key == "max_tokens" and actual is None:
                actual = params.get("max_completion_tokens")
            status = "unknown" if actual is None else "match" if actual == expected else "mismatch"
            counts[status] += row["count"]
        status = "mismatch" if counts["mismatch"] else "unknown" \
            if counts["unknown"] or not rows else "match"
        dimensions[key] = {"expected": expected, "request_counts": counts, "status": status}
    return {"profile": GLM53_TB21, "dimensions": dimensions,
            "reference_reproduced": False,
            "note": "Configuration observations alone cannot reproduce a published score."}
