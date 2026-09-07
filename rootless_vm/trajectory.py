"""ATIF trajectory production and privacy-safe collection helpers.

Host-dispatched harnesses record a small redacted audit stream while running.
This module projects that stream into Harbor's Agent Trajectory Interchange
Format (ATIF-v1.7) and sanitizes trajectories copied into analysis bundles.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


_CREDENTIAL_URL_RE = re.compile(r"(?i)(https?://)[^/@\s:'\"]+:[^/@\s'\"]+@")
_PROXY_AUTH_RE = re.compile(
    r"(?i)(proxy-authorization\s*:\s*(?:basic\s+)?)([^\s'\"\\]+)"
)
_BEARER_AUTH_RE = re.compile(
    r"(?i)(\bauthorization\s*:\s*bearer\s+)([A-Za-z0-9._~+/-]{12,})"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(auth|password|passwd|token|secret|api[_-]?key)="
    r"([A-Za-z0-9_+./:-]{12,})"
)
_ROOTLESS_TOKEN_RE = re.compile(r"(?i)\brootless:[0-9a-f]{32,}\b")
_OPENAI_STYLE_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"
)
_SECRET_KEY_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _usage_integer(usage: dict[str, Any], *names: str) -> int | None:
    observed_numeric_value = False
    for name in names:
        value = usage.get(name)
        if value is None:
            continue
        try:
            parsed = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
        observed_numeric_value = True
        # Some OpenAI-compatible responses populate the inactive alias with
        # zero (input/output_tokens) and the real value in the legacy alias
        # (prompt/completion_tokens). Keep looking until a positive value is
        # found, while preserving an explicit all-zero usage report.
        if parsed > 0:
            return parsed
    return 0 if observed_numeric_value else None


def _tool_call(call: dict[str, Any], fallback_id: str) -> dict[str, Any] | None:
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_arguments = function.get("arguments", {})
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        try:
            decoded = json.loads(raw_arguments or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {"_invalid_json": _safe_text(raw_arguments)}
        arguments = decoded if isinstance(decoded, dict) else {"value": decoded}
    return {
        "tool_call_id": str(call.get("id") or fallback_id),
        "function_name": name,
        "arguments": arguments,
    }


def host_audit_to_atif(
    transcript: list[dict[str, Any]],
    *,
    instruction: str,
    system_prompt: str,
    agent_name: str,
    agent_version: str,
    model_name: str,
    tool_definitions: list[dict[str, Any]],
    session_id: str | None,
    credential_boundary: str = "host-only",
    harness_profile: str | None = None,
) -> dict[str, Any]:
    """Project a host audit transcript into privacy-safe ATIF-v1.7."""

    steps: list[dict[str, Any]] = [
        {"step_id": 1, "source": "system", "message": system_prompt},
        {"step_id": 2, "source": "user", "message": instruction},
    ]
    total_prompt = 0
    total_completion = 0
    assistant_rows = [
        row for row in transcript if isinstance(row.get("assistant"), dict)
    ]
    for assistant_index, row in enumerate(assistant_rows, start=1):
        assistant = row["assistant"]
        round_index = row.get("round")
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        prompt_tokens = _usage_integer(usage, "input_tokens", "prompt_tokens")
        completion_tokens = _usage_integer(usage, "output_tokens", "completion_tokens")
        total_prompt += prompt_tokens or 0
        total_completion += completion_tokens or 0
        calls = []
        for call_index, candidate in enumerate(assistant.get("tool_calls") or []):
            if not isinstance(candidate, dict):
                continue
            converted = _tool_call(candidate, f"round-{round_index}-call-{call_index}")
            if converted is not None:
                calls.append(converted)
        call_ids = {call["tool_call_id"] for call in calls}
        observations = []
        for candidate in transcript:
            if candidate.get("round") != round_index or "result" not in candidate:
                continue
            call_id = str(candidate.get("tool_call_id") or "")
            if call_id not in call_ids:
                continue
            observation: dict[str, Any] = {
                "source_call_id": call_id,
                "content": _safe_text(candidate.get("result")),
            }
            timeout = candidate.get("effective_timeout_sec")
            if timeout is not None:
                observation["extra"] = {"effective_timeout_sec": timeout}
            observations.append(observation)
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": "agent",
            "model_name": model_name,
            "message": _safe_text(assistant.get("content")),
            "llm_call_count": 1,
        }
        if calls:
            step["tool_calls"] = calls
        if observations:
            step["observation"] = {"results": observations}
        metrics: dict[str, Any] = {}
        if prompt_tokens is not None:
            metrics["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            metrics["completion_tokens"] = completion_tokens
        metric_extra: dict[str, Any] = {}
        dispatch = usage.get("_dispatch")
        if isinstance(dispatch, dict):
            metric_extra["dispatch"] = copy.deepcopy(dispatch)
        harness_request = usage.get("_harness_request")
        if isinstance(harness_request, dict):
            metric_extra["harness_request"] = copy.deepcopy(harness_request)
        if metric_extra:
            metrics["extra"] = metric_extra
        if metrics:
            step["metrics"] = metrics
        reasoning = assistant.get("reasoning_content")
        if isinstance(reasoning, dict) and reasoning.get("redacted") is True:
            step["extra"] = {"reasoning_content_redacted": copy.deepcopy(reasoning)}
        elif isinstance(reasoning, str) and reasoning:
            step["extra"] = {
                "reasoning_content_redacted": {
                    "redacted": True,
                    "characters": len(reasoning),
                    "sha256": hashlib.sha256(reasoning.encode()).hexdigest(),
                }
            }
        steps.append(step)

    agent_extra: dict[str, Any] = {"credential_boundary": credential_boundary}
    if harness_profile:
        agent_extra["harness_profile"] = harness_profile
    payload: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": agent_name,
            "version": agent_version,
            "model_name": model_name,
            "tool_definitions": copy.deepcopy(tool_definitions),
            "extra": agent_extra,
        },
        "steps": steps,
        "notes": (
            "Explicit model reasoning is intentionally redacted; provider routing "
            "and usage evidence remain in metrics.extra.dispatch."
        ),
        "final_metrics": {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_steps": len(steps),
        },
    }
    if session_id:
        payload["session_id"] = session_id
    return payload


def persist_host_atif(logs_dir: Path, **kwargs: Any) -> Path:
    path = logs_dir / "trajectory.json"
    _atomic_private_json(path, host_audit_to_atif(**kwargs))
    return path


def _redact_text(value: str) -> str:
    value = _CREDENTIAL_URL_RE.sub(r"\1<redacted>@", value)
    value = _PROXY_AUTH_RE.sub(r"\1<redacted>", value)
    value = _BEARER_AUTH_RE.sub(r"\1<redacted>", value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", value)
    value = _ROOTLESS_TOKEN_RE.sub("rootless:<redacted>", value)
    return _OPENAI_STYLE_SECRET_RE.sub("sk-<redacted>", value)


def sanitize_collected_trajectory(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy an ATIF trajectory while removing reasoning and credential values."""

    result = copy.deepcopy(payload)
    reasoning_step_ids = {
        step.get("step_id")
        for step in result.get("steps", [])
        if isinstance(step, dict)
        and isinstance(step.get("reasoning_content"), str)
        and step["reasoning_content"]
    }

    def visit(value: Any, key: str | None = None) -> Any:
        if key == "reasoning_content" and isinstance(value, str) and value:
            return None
        if isinstance(value, str):
            if key and any(marker in key.upper() for marker in _SECRET_KEY_MARKERS):
                return "<redacted>"
            return _redact_text(value)
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            return {item_key: visit(item, item_key) for item_key, item in value.items()}
        return value

    sanitized = visit(result)
    for step in sanitized.get("steps", []):
        if not isinstance(step, dict) or step.get("step_id") not in reasoning_step_ids:
            continue
        step.pop("reasoning_content", None)
        extra = step.setdefault("extra", {})
        if isinstance(extra, dict):
            extra.setdefault("reasoning_content_redacted", {"redacted": True})
    return sanitized


def validate_atif(payload: dict[str, Any]) -> None:
    """Perform dependency-free structural validation before collecting a trace."""

    schema = payload.get("schema_version")
    if not isinstance(schema, str) or not schema.startswith("ATIF-v1."):
        raise ValueError("trajectory is missing a supported ATIF schema_version")
    agent = payload.get("agent")
    if not isinstance(agent, dict) or not isinstance(agent.get("name"), str):
        raise ValueError("trajectory is missing agent identity")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("trajectory must contain at least one step")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or step.get("step_id") != index:
            raise ValueError("trajectory step ids must be sequential from 1")


def _normalize_usage_from_host_audit(
    payload: dict[str, Any], host_transcript: list[dict[str, Any]]
) -> dict[str, Any]:
    """Backfill collected-copy token metrics from the retained host audit."""

    result = copy.deepcopy(payload)
    agent_steps = [
        step
        for step in result.get("steps", [])
        if isinstance(step, dict) and step.get("source") == "agent"
    ]
    assistant_rows = [
        row
        for row in host_transcript
        if isinstance(row, dict) and isinstance(row.get("assistant"), dict)
    ]
    if not agent_steps or len(agent_steps) != len(assistant_rows):
        return result

    normalized = False
    total_prompt_tokens = 0
    total_completion_tokens = 0
    for step, row in zip(agent_steps, assistant_rows, strict=True):
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        prompt_tokens = _usage_integer(usage, "input_tokens", "prompt_tokens")
        completion_tokens = _usage_integer(
            usage, "output_tokens", "completion_tokens"
        )
        metrics = step.setdefault("metrics", {})
        if not isinstance(metrics, dict):
            return copy.deepcopy(payload)
        if prompt_tokens is not None:
            normalized = normalized or metrics.get("prompt_tokens") != prompt_tokens
            metrics["prompt_tokens"] = prompt_tokens
        retained_prompt_tokens = _usage_integer(metrics, "prompt_tokens")
        total_prompt_tokens += retained_prompt_tokens or 0
        if completion_tokens is not None:
            normalized = (
                normalized or metrics.get("completion_tokens") != completion_tokens
            )
            metrics["completion_tokens"] = completion_tokens
        retained_completion_tokens = _usage_integer(metrics, "completion_tokens")
        total_completion_tokens += retained_completion_tokens or 0

    final_metrics = result.setdefault("final_metrics", {})
    if not isinstance(final_metrics, dict):
        return copy.deepcopy(payload)
    normalized = (
        normalized
        or final_metrics.get("total_prompt_tokens") != total_prompt_tokens
        or final_metrics.get("total_completion_tokens") != total_completion_tokens
    )
    final_metrics["total_prompt_tokens"] = total_prompt_tokens
    final_metrics["total_completion_tokens"] = total_completion_tokens
    if normalized:
        agent = result.get("agent")
        if isinstance(agent, dict):
            extra = agent.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["usage_normalized_from_host_audit"] = True
    return result


def write_collected_trajectory(
    path: Path,
    payload: dict[str, Any],
    *,
    host_transcript: list[dict[str, Any]] | None = None,
) -> None:
    collected = (
        _normalize_usage_from_host_audit(payload, host_transcript)
        if host_transcript is not None
        else payload
    )
    sanitized = sanitize_collected_trajectory(collected)
    validate_atif(sanitized)
    _atomic_private_json(path, sanitized)
