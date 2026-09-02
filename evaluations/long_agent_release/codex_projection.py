"""Strict Codex CLI JSONL and proxy-metrics projection into benchmark v2.

The raw Codex event stream remains the trajectory authority.  The isolated
Responses-to-Kimi proxy supplies per-model-call usage and timing that Codex's
single aggregate ``turn.completed`` event cannot expose.  This module checks
the two views against one another before building a manifest-bound v2 task.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import validate_proxy_metrics
from lib.benchmark_contract import (
    BenchmarkContractError,
    build_task_record_v2,
    public_price_cost_from_usage,
    validate_record,
)


class CodexProjectionError(ValueError):
    """The trial lacks exact, internally consistent Codex evidence."""


_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
}
_KNOWN_FALLBACK_WARNING = (
    "Model metadata for `kimi-k3` not found. Defaulting to fallback metadata"
)


def _jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise CodexProjectionError(
                        f"{label} line {line_number} must be a JSON object"
                    )
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexProjectionError(f"{label} is not valid UTF-8 JSONL") from exc
    if not rows:
        raise CodexProjectionError(f"{label} must contain at least one event")
    return rows


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise CodexProjectionError(f"{label} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodexProjectionError(
            f"{label} must be a non-negative integer"
        ) from exc
    if result < 0:
        raise CodexProjectionError(f"{label} must be a non-negative integer")
    return result


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _round_usage(raw: dict[str, Any], round_index: int) -> dict[str, int]:
    if "input_tokens" not in raw or "output_tokens" not in raw:
        raise CodexProjectionError(
            f"proxy round {round_index} is missing provider usage"
        )
    details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    if not isinstance(details, dict) or not isinstance(output_details, dict):
        raise CodexProjectionError(
            f"proxy round {round_index} usage details are invalid"
        )
    prompt = _non_negative_integer(raw.get("input_tokens"), "input_tokens")
    cached = _non_negative_integer(
        details.get("cached_tokens", 0), "cached_input_tokens"
    )
    output = _non_negative_integer(raw.get("output_tokens"), "output_tokens")
    reasoning = _non_negative_integer(
        output_details.get("reasoning_tokens", 0), "reasoning_output_tokens"
    )
    if cached > prompt or reasoning > output:
        raise CodexProjectionError(
            f"proxy round {round_index} usage subsets exceed their totals"
        )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + output,
    }


def _aggregate_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )
    result = {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}
    result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def _codex_turn_usage(raw: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": _non_negative_integer(
            raw.get("input_tokens", 0), "Codex turn input_tokens"
        ),
        "completion_tokens": _non_negative_integer(
            raw.get("output_tokens", 0), "Codex turn output_tokens"
        ),
        "cache_read_tokens": _non_negative_integer(
            raw.get("cached_input_tokens", 0), "Codex turn cached_input_tokens"
        ),
        "cache_write_tokens": _non_negative_integer(
            raw.get("cache_write_input_tokens", 0),
            "Codex turn cache_write_input_tokens",
        ),
        "reasoning_tokens": _non_negative_integer(
            raw.get("reasoning_output_tokens", 0),
            "Codex turn reasoning_output_tokens",
        ),
        "total_tokens": (
            _non_negative_integer(raw.get("input_tokens", 0), "input_tokens")
            + _non_negative_integer(raw.get("output_tokens", 0), "output_tokens")
        ),
    }


def project_codex_trial(
    *,
    raw_trajectory: Path,
    proxy_metrics: Path,
) -> dict[str, Any]:
    """Reconcile one tagged proxy trace with the pinned Codex JSONL stream."""

    events = _jsonl(raw_trajectory, "Codex raw trajectory")
    metrics_rows = _jsonl(proxy_metrics, "Codex proxy metrics")
    translations = [
        row for row in metrics_rows
        if row.get("event") == "responsesTranslation"
    ]
    if not translations:
        raise CodexProjectionError("Codex proxy metrics contain no model requests")
    report = validate_proxy_metrics(
        str(proxy_metrics),
        expected_request_count=len(translations),
        require_trial_token=True,
    )
    if not report["valid"]:
        raise CodexProjectionError("Codex proxy metrics invalidate the trial")

    rounds: list[dict[str, Any]] = []
    context_blocks: list[dict[str, Any]] = []
    tool_schemas: list[dict[str, Any]] = []
    usages: list[dict[str, int]] = []
    for round_index, row in enumerate(translations, 1):
        usage_raw = row.get("usage")
        if not isinstance(usage_raw, dict):
            raise CodexProjectionError(
                f"proxy round {round_index} usage must be an object"
            )
        usage = _round_usage(usage_raw, round_index)
        usages.append(usage)
        request_bytes = _non_negative_integer(
            row.get("requestBytes"), f"proxy round {round_index} requestBytes"
        )
        schema_bytes = _non_negative_integer(
            row.get("toolSchemaBytes"),
            f"proxy round {round_index} toolSchemaBytes",
        )
        tool_count = _non_negative_integer(
            row.get("toolCount"), f"proxy round {round_index} toolCount"
        )
        schema_digest = str(row.get("toolSchemaDigest") or "")
        if len(schema_digest) != 64 or any(
            character not in "0123456789abcdef" for character in schema_digest
        ):
            raise CodexProjectionError(
                f"proxy round {round_index} tool schema digest is invalid"
            )
        rounds.append({
            "round": round_index,
            "status": str(row.get("status") or ""),
            "requestDigest": str(row.get("requestDigest") or ""),
            "usage": usage,
            "requestBytes": request_bytes,
            "modelWallMs": _non_negative_integer(
                row.get("upstreamWallNs"), "upstreamWallNs") / 1_000_000,
            "proxyWallMs": _non_negative_integer(
                row.get("rawWallNs"), "rawWallNs") / 1_000_000,
        })
        context_blocks.append({
            "round": round_index,
            "kind": "codex_request",
            "tokenCount": usage["prompt_tokens"],
            "cacheReadTokens": usage["cache_read_tokens"],
            "visibleBytes": request_bytes,
            "requestDigest": str(row.get("requestDigest") or ""),
        })
        tool_schemas.append({
            "round": round_index,
            "count": tool_count,
            "visibleBytes": schema_bytes,
            "sha256": schema_digest,
        })

    turn_completed = [
        row for row in events if row.get("type") == "turn.completed"
    ]
    if len(turn_completed) != 1 \
            or not isinstance(turn_completed[0].get("usage"), dict):
        raise CodexProjectionError(
            "Codex raw trajectory requires exactly one settled turn.completed usage"
        )
    proxy_usage = _aggregate_usage(usages)
    codex_usage = _codex_turn_usage(turn_completed[0]["usage"])
    if proxy_usage != codex_usage:
        raise CodexProjectionError(
            "Codex aggregate usage does not match per-request proxy usage"
        )

    tool_results: list[dict[str, Any]] = []
    call_graph: list[dict[str, Any]] = []
    compactions: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    final_output = ""
    tool_wall_ms = 0.0
    for event_index, event in enumerate(events, 1):
        kind = str(event.get("type") or "")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if kind == "item.completed" and item_type == "agent_message":
            final_output = str(item.get("text") or "")
        if kind == "item.completed" and item_type in _TOOL_ITEM_TYPES:
            item_id = str(item.get("id") or f"item-{event_index}")
            duration = item.get("duration_ms", item.get("durationMs", 0))
            try:
                duration_ms = max(0.0, float(duration or 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise CodexProjectionError("Codex tool duration is invalid") from exc
            if not math.isfinite(duration_ms):
                raise CodexProjectionError("Codex tool duration is invalid")
            tool_wall_ms += duration_ms
            canonical_item = {
                key: value for key, value in item.items()
                if key not in {"aggregated_output", "output"}
            }
            output_value = item.get("aggregated_output", item.get("output", ""))
            output_text = (
                output_value if isinstance(output_value, str)
                else json.dumps(output_value, ensure_ascii=False, sort_keys=True)
            )
            tool_results.append({
                "callId": item_id,
                "toolName": item_type,
                "status": str(item.get("status") or "completed"),
                "durationMs": duration_ms,
                "visibleBytes": len(output_text.encode("utf-8")),
                "resultDigest": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                "itemDigest": _sha256(canonical_item),
                "paidCostUsd": 0,
            })
            call_graph.append({
                "callId": item_id,
                "toolName": item_type,
                "eventIndex": event_index,
            })
        if "compact" in kind.lower() or "compact" in item_type.lower():
            compactions.append({
                "kind": "codex_local",
                "eventIndex": event_index,
                "eventDigest": _sha256(event),
            })
        if kind == "item.completed" and item_type == "error":
            message = str(item.get("message") or "")
            if _KNOWN_FALLBACK_WARNING not in message:
                incidents.append({
                    "severity": "error",
                    "code": "codex_item_error",
                    "eventIndex": event_index,
                    "messageDigest": hashlib.sha256(message.encode()).hexdigest(),
                })
        if kind in {"turn.failed", "error"}:
            incidents.append({
                "severity": "error",
                "code": kind.replace(".", "_"),
                "eventIndex": event_index,
                "eventDigest": _sha256(event),
            })

    first_byte_values = [
        _non_negative_integer(
            row.get("firstUpstreamByteAtUnixNs"),
            "firstUpstreamByteAtUnixNs",
        )
        for row in translations
    ]
    if any(value <= 0 for value in first_byte_values):
        raise CodexProjectionError("proxy metrics are missing first-byte timestamps")
    return {
        "trialToken": report["trialToken"],
        "rounds": rounds,
        "contextBlocks": context_blocks,
        "toolSchemas": tool_schemas,
        "toolResults": tool_results,
        "compactions": compactions,
        "callGraph": call_graph,
        "incidents": incidents,
        "finalOutput": final_output,
        "finalOutputDigest": hashlib.sha256(final_output.encode("utf-8")).hexdigest(),
        "aggregateUsage": proxy_usage,
        "timing": {
            "firstUpstreamByteAtUnixNs": min(first_byte_values),
            "modelMs": sum(
                _non_negative_integer(row.get("upstreamWallNs"), "upstreamWallNs")
                for row in translations
            ) / 1_000_000,
            "toolMs": tool_wall_ms,
            "translationCpuMs": report["translationCpuNs"] / 1_000_000,
            "proxyCpuMs": report["proxyCpuNs"] / 1_000_000,
        },
    }


def build_codex_release_task_record(
    *,
    manifest: dict[str, Any],
    task_id: str,
    projection: dict[str, Any],
    oracle: dict[str, Any],
    artifacts: list[dict[str, Any]],
    task_started_at_unix_ns: int,
    oracle_ready_ms: float,
    queue_ms: float = 0.0,
    retries: list[dict[str, Any]] | None = None,
    judges: list[dict[str, Any]] | None = None,
    incidents: list[dict[str, Any]] | None = None,
    environment: dict[str, Any] | None = None,
    completed_at_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Build one exact, manifest-bound baseline record after its oracle settles."""

    try:
        validate_record(manifest)
    except BenchmarkContractError as exc:
        raise CodexProjectionError("Codex projection manifest is invalid") from exc
    if manifest.get("recordType") != "manifest" \
            or manifest.get("comparisonRole") != "baseline":
        raise CodexProjectionError("Codex projection requires a baseline manifest")
    task_rows = {
        str(row.get("taskId") or ""): row for row in manifest.get("tasks") or []
    }
    task = task_rows.get(str(task_id))
    if task is None:
        raise CodexProjectionError("Codex projection task is not in the manifest")
    if not isinstance(oracle.get("passed"), bool):
        raise CodexProjectionError("Codex task oracle must be resolved")

    raw_wall_ms = float(oracle_ready_ms)
    if not math.isfinite(raw_wall_ms) or raw_wall_ms < 0:
        raise CodexProjectionError("oracle_ready_ms must be finite and non-negative")
    start_ns = _non_negative_integer(
        task_started_at_unix_ns, "task_started_at_unix_ns"
    )
    first_byte_ns = _non_negative_integer(
        projection.get("timing", {}).get("firstUpstreamByteAtUnixNs"),
        "firstUpstreamByteAtUnixNs",
    )
    if first_byte_ns < start_ns:
        raise CodexProjectionError("first model byte predates task start")
    ttft_ms = (first_byte_ns - start_ns) / 1_000_000
    queue_value = float(queue_ms)
    if not math.isfinite(queue_value) or queue_value < 0:
        raise CodexProjectionError("queue_ms must be finite and non-negative")

    model_cost = sum(
        public_price_cost_from_usage(row["usage"], manifest["priceCard"])["costUsd"]
        for row in projection["rounds"]
    )
    retry_rows = list(retries or [])
    retry_model_cost = sum(
        public_price_cost_from_usage(usage, manifest["priceCard"])["costUsd"]
        for retry in retry_rows
        for usage in retry.get("modelUsages") or []
    )
    retry_paid_tool_cost = sum(
        float(retry.get("paidToolCostUsd", 0)) for retry in retry_rows
    )
    model_cost += retry_model_cost
    translation_cpu = float(projection["timing"]["translationCpuMs"])
    proxy_cpu = float(projection["timing"]["proxyCpuMs"])
    cost = {
        "modelCostUsd": model_cost,
        # Local-compaction model requests are already represented as exact
        # proxy rounds. Keep a zero separate component to avoid double billing.
        "compactionCostUsd": 0,
        "paidToolCostUsd": retry_paid_tool_cost,
        "agentCostUsd": model_cost + retry_paid_tool_cost,
    }
    latency = {
        "rawWallMs": raw_wall_ms,
        "oracleReadyMs": raw_wall_ms,
        "queueMs": queue_value,
        "queueMeasurement": "provider_unavailable" if queue_value == 0 else "observed",
        "ttftMs": ttft_ms,
        "modelMs": float(projection["timing"]["modelMs"]),
        "toolMs": float(projection["timing"]["toolMs"]),
        "translationCpuMs": translation_cpu,
        "proxyCpuMs": proxy_cpu,
        "codexFavoredCorrectedWallMs": max(0.0, raw_wall_ms - translation_cpu),
    }
    merged_incidents = list(projection.get("incidents") or [])
    merged_incidents.extend(incidents or [])
    return build_task_record_v2(
        run_id=manifest["runId"],
        dataset=str(task["dataset"]),
        family=str(task["family"]),
        task_id=str(task_id),
        agent=manifest["agent"],
        provider_face=manifest["providerFace"],
        provider_slot_id=manifest["providerSlotId"],
        thinking=manifest["thinking"],
        experiment_arm=manifest["experimentArm"],
        oracle=oracle,
        rounds=list(projection["rounds"]),
        context_blocks=list(projection["contextBlocks"]),
        tool_schemas=list(projection["toolSchemas"]),
        tool_results=list(projection["toolResults"]),
        compactions=list(projection["compactions"]),
        call_graph=list(projection["callGraph"]),
        retries=retry_rows,
        cost=cost,
        latency=latency,
        incidents=merged_incidents,
        judges=list(judges or []),
        final_output_digest=str(projection["finalOutputDigest"]),
        environment=environment,
        artifacts=artifacts,
        completed_at=completed_at_unix_ms,
    )


__all__ = [
    "CodexProjectionError",
    "build_codex_release_task_record",
    "project_codex_trial",
]
