"""Project one tagged OpenCode trial into reconciled benchmark evidence.

The proxy metrics are the exact usage authority; the guest's
``--format=json`` event stream must reconcile with them exactly:

- sum(step_finish.input + step_finish.cache.read) == sum(proxy prompt_tokens)
- sum(step_finish.output + step_finish.reasoning) == sum(proxy completion_tokens)

GLM chat-completions usage folds cached prompt tokens into ``prompt_tokens``
and reasoning into ``completion_tokens``; OpenCode reports them separately.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import CodexContractError
from evaluations.opencode_proxy.metrics import validate_opencode_proxy_metrics


class OpenCodeProjectionError(ValueError):
    """The trial lacks exact, internally consistent OpenCode evidence."""


def _jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OpenCodeProjectionError(f"{label} is unreadable") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OpenCodeProjectionError(
                f"{label} line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise OpenCodeProjectionError(
                f"{label} line {line_number} must be an object"
            )
        rows.append(value)
    return rows


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpenCodeProjectionError(f"{label} must be a non-negative integer")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_opencode_trial(
    *,
    raw_events: Path,
    proxy_metrics: Path,
    trial_token: str,
) -> dict[str, Any]:
    """Reconcile one tagged proxy trace with the OpenCode event stream."""

    try:
        metrics = validate_opencode_proxy_metrics(
            str(proxy_metrics), expected_trial_token=trial_token
        )
    except CodexContractError as exc:
        raise OpenCodeProjectionError(str(exc)) from exc
    if not metrics["valid"]:
        raise OpenCodeProjectionError(
            "opencode proxy metrics are incomplete or violated the contract"
        )
    events = _jsonl(raw_events, "OpenCode raw events")
    if not events:
        raise OpenCodeProjectionError("OpenCode produced no events")

    cli_totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    tool_results: list[dict[str, Any]] = []
    text_parts: list[str] = []
    step_finish_count = 0
    for event in events:
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "step-finish":
            step_finish_count += 1
            tokens = part.get("tokens")
            if not isinstance(tokens, dict):
                raise OpenCodeProjectionError(
                    "step-finish event is missing token usage"
                )
            cli_totals["input"] += _non_negative_integer(
                tokens.get("input", 0), "step-finish input tokens")
            cli_totals["output"] += _non_negative_integer(
                tokens.get("output", 0), "step-finish output tokens")
            cli_totals["reasoning"] += _non_negative_integer(
                tokens.get("reasoning", 0), "step-finish reasoning tokens")
            cache = tokens.get("cache")
            if not isinstance(cache, dict):
                cache = {}
            cli_totals["cache_read"] += _non_negative_integer(
                cache.get("read", 0), "step-finish cache read tokens")
        elif part_type == "tool":
            state = part.get("state")
            if not isinstance(state, dict):
                state = {}
            call_id = str(part.get("callID") or "")
            tool_name = str(part.get("tool") or "")
            if not call_id or not tool_name:
                raise OpenCodeProjectionError(
                    "tool event is missing callID or tool name"
                )
            arguments = json.dumps(
                state.get("input"), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, default=str,
            )
            output = state.get("output")
            output_text = output if isinstance(output, str) else json.dumps(
                output, ensure_ascii=False, default=str)
            tool_results.append({
                "callId": call_id,
                "toolName": tool_name,
                "itemDigest": _digest(arguments),
                "resultDigest": _digest(output_text),
                "visibleBytes": len(output_text.encode("utf-8")),
                "status": str(state.get("status") or "unknown"),
            })
        elif part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)

    if not step_finish_count:
        raise OpenCodeProjectionError("OpenCode produced no step-finish usage")

    proxy_usage = metrics["usage"]
    proxy_prompt = int(proxy_usage["prompt_tokens"])
    proxy_completion = int(proxy_usage["completion_tokens"])
    cli_prompt = cli_totals["input"] + cli_totals["cache_read"]
    cli_completion = cli_totals["output"] + cli_totals["reasoning"]
    if cli_prompt != proxy_prompt or cli_completion != proxy_completion:
        raise OpenCodeProjectionError(
            "OpenCode CLI usage does not reconcile with tagged proxy metrics: "
            f"prompt cli={cli_prompt} proxy={proxy_prompt}, "
            f"completion cli={cli_completion} proxy={proxy_completion}"
        )

    final_output = text_parts[-1] if text_parts else ""
    aggregate = {
        "prompt_tokens": proxy_prompt,
        "completion_tokens": proxy_completion,
        "cache_read_tokens": int(proxy_usage["cache_read_tokens"]),
        "reasoning_tokens": int(proxy_usage["reasoning_tokens"]),
        "total_tokens": proxy_prompt + proxy_completion,
    }
    rounds = [
        {"index": index}
        # Accounted completions only: tolerated usage-free upstream failures
        # produced no CLI step and are not trajectory rounds.
        for index in range(int(metrics.get("accountedChatCompletions")
                               or metrics["chatCompletions"]))
    ]
    return {
        "toolResults": tool_results,
        "rounds": rounds,
        "stepFinishCount": step_finish_count,
        "aggregateUsage": aggregate,
        "finalOutput": final_output,
        "finalOutputDigest": _digest(final_output),
        "timing": {
            "modelMs": int(metrics["upstreamWallNs"]) / 1_000_000,
            "translationCpuMs": 0.0,
            "proxyCpuMs": int(metrics["proxyCpuNs"]) / 1_000_000,
        },
        "model": metrics["model"],
    }


__all__ = ["OpenCodeProjectionError", "project_opencode_trial"]
