"""Strict production-Tofu evidence projection into benchmark v2.

The native event log is the trajectory authority.  The public AgentRuntime
evidence projection supplies exact provider usage/context telemetry, while the
Harbor adapter audit records the guest-owned tool boundary before any runtime
result envelope is applied.  A task is projected only after all three views
agree on rounds, schemas, tool calls, output, and terminal state.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from evaluations.swebench.tofu_kimi_runtime import (
    tofu_kimi_prompt_contract_sha256,
)
from lib.benchmark_contract import (
    BenchmarkContractError,
    build_task_record_v2,
    public_price_cost_from_usage,
    validate_record,
)
from lib.cost import normalize_usage, split_input_tokens


class TofuProjectionError(ValueError):
    """A formal Tofu trial lacks exact, internally consistent evidence."""


_EVENT_CONTRACT = "tofu.harbor-runtime-event-observation/v1"
_RUNTIME_CONTRACT = "tofu.agent-runtime-evidence/v1"
_TOOL_AUDIT_CONTRACT = "tofu.harbor-custom-tool-audit/v1"
_TERMINAL_EVENTS = {"done", "error", "aborted"}


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TofuProjectionError(
                f"JSON evidence contains duplicate key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise TofuProjectionError(
        f"JSON evidence contains non-finite number {value}")


def _strict_load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TofuProjectionError(
            f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TofuProjectionError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise TofuProjectionError(f"{label} must contain a JSON object")
    return value


def _strict_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise TofuProjectionError(
            f"{label} must be a regular non-symlink file")
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(
                    line,
                    object_pairs_hook=_object_pairs,
                    parse_constant=_invalid_constant,
                )
                if not isinstance(value, dict):
                    raise TofuProjectionError(
                        f"{label} line {line_number} is not an object")
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TofuProjectionError(f"{label} is not strict JSONL") from exc
    if not rows:
        raise TofuProjectionError(f"{label} must contain at least one row")
    return rows


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TofuProjectionError("evidence is not finite canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value)


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TofuProjectionError(f"{label} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TofuProjectionError(
            f"{label} must be a non-negative integer") from exc
    if result < 0 or isinstance(value, float) and not value.is_integer():
        raise TofuProjectionError(f"{label} must be a non-negative integer")
    return result


def _non_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise TofuProjectionError(f"{label} must be a non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TofuProjectionError(
            f"{label} must be a non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise TofuProjectionError(f"{label} must be a non-negative number")
    return result


def _validate_raw_usage_numbers(value: dict[str, Any], label: str) -> None:
    numeric_keys = {
        "prompt_tokens", "input_tokens", "completion_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "cached_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "reasoning_tokens", "thinking_tokens", "total_tokens",
        "stream_elapsed_ms",
    }
    for key in numeric_keys:
        if key in value:
            _non_negative_integer(value[key], f"{label}.{key}")
    for parent, child in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        details = value.get(parent)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise TofuProjectionError(f"{label}.{parent} must be an object")
        if child in details:
            _non_negative_integer(
                details[child], f"{label}.{parent}.{child}")


def _canonical_usage(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise TofuProjectionError(f"{label} must be an object")
    _validate_raw_usage_numbers(value, label)
    normalized = normalize_usage(value)
    if any(amount < 0 for amount in normalized.values()):
        raise TofuProjectionError(f"{label} contains negative token usage")
    uncached, total_input = split_input_tokens(value)
    if uncached < 0 or total_input < 0:
        raise TofuProjectionError(f"{label} has an invalid cache convention")
    cache_read = int(normalized["cache_read"])
    cache_write = int(normalized["cache_write"])
    output = int(normalized["output"])
    reasoning = int(normalized["thinking"])
    if reasoning > output and output > 0:
        raise TofuProjectionError(f"{label} reasoning exceeds output tokens")
    return {
        "prompt_tokens": int(total_input),
        "completion_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "total_tokens": int(total_input) + output,
    }


def _aggregate_usage(rows: list[dict[str, int]]) -> dict[str, int]:
    keys = (
        "prompt_tokens", "completion_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens",
    )
    result = {key: sum(row[key] for row in rows) for key in keys}
    result["total_tokens"] = (
        result["prompt_tokens"] + result["completion_tokens"])
    return result


def _add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return _aggregate_usage([left, right])


def _prompt_profile(
    value: Any, *, requested_profile: str, round_number: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TofuProjectionError(
            f"context round {round_number} lacks prompt-profile evidence")
    digest = value.get("sha256")
    resolved = str(value.get("resolvedProfile") or "")
    valid = (
        value.get("contractVersion") == "tofu.prompt-profile/v1"
        and value.get("status") == "applied"
        and value.get("requestedProfile") == requested_profile
        and resolved
        and value.get("effectiveProfile") == resolved
        and value.get("model") == "kimi-k3"
        and _is_sha256(digest)
        and _non_negative_integer(
            value.get("charCount"), "prompt profile charCount") > 0
        and _non_negative_integer(
            value.get("tokenCount"), "prompt profile tokenCount") > 0
        and (requested_profile == "auto" or resolved == requested_profile)
    )
    if not valid:
        raise TofuProjectionError(
            f"context round {round_number} prompt profile drifted")
    return json.loads(_canonical_bytes(value))


def _dispatch_timing(
    usage: dict[str, Any], *, call_index: int,
) -> tuple[float, float, int, int, int, float, str]:
    dispatch = usage.get("_dispatch")
    if not isinstance(dispatch, dict):
        raise TofuProjectionError(
            f"API call {call_index} lacks public dispatch timing")
    if dispatch.get("model") not in (None, "", "kimi-k3"):
        raise TofuProjectionError(
            f"API call {call_index} used a different physical model")
    latency = _non_negative_number(
        dispatch.get("latency_ms"), f"API call {call_index} latency_ms")
    ttft = _non_negative_number(
        dispatch.get("ttft_ms"), f"API call {call_index} ttft_ms")
    started = _non_negative_integer(
        dispatch.get("stream_started_at_unix_ns"),
        f"API call {call_index} stream_started_at_unix_ns",
    )
    first = _non_negative_integer(
        dispatch.get("first_content_at_unix_ns"),
        f"API call {call_index} first_content_at_unix_ns",
    )
    completed = _non_negative_integer(
        dispatch.get("stream_completed_at_unix_ns"),
        f"API call {call_index} stream_completed_at_unix_ns",
    )
    queue_wait = _non_negative_number(
        dispatch.get("queue_wait_ms"),
        f"API call {call_index} queue_wait_ms",
    )
    queue_measurement = str(
        dispatch.get("queue_wait_measurement") or "")
    if not queue_measurement:
        raise TofuProjectionError(
            f"API call {call_index} lacks queue measurement provenance")
    if not started <= first <= completed or ttft > latency + 5:
        raise TofuProjectionError(
            f"API call {call_index} dispatch timing is inconsistent")
    return (
        latency, ttft, started, first, completed,
        queue_wait, queue_measurement,
    )


def _request_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": event.get("model"),
        "params": event.get("params") or {},
        "messages": event.get("messages") or [],
        "tools": event.get("tools") or [],
    }


def _parse_envelope(content: str) -> dict[str, Any] | None:
    """Compatibility parser for pre-split native event recordings."""
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) \
            or value.get("contractVersion") != "tofu.tool-result/v2":
        return None
    return {
        key: value.get(key)
        for key in (
            "contractVersion", "status", "artifactRef", "cursor", "truncated",
            "rawBytes", "visibleBytes", "freshness", "evidenceId", "error",
        )
        if key in value
    }


def _parse_result_evidence(value: Any) -> dict[str, Any] | None:
    """Project the non-model V1 sidecar into the release envelope record."""
    if not isinstance(value, dict) or value.get(
            "contractVersion") != "tofu.tool-result-evidence/v1":
        return None
    return {
        key: projected
        for key, projected in {
            "contractVersion": value.get("resultContractVersion"),
            "status": value.get("status"),
            "artifactRef": value.get("artifactRef"),
            "cursor": value.get("cursor"),
            "truncated": value.get("truncated"),
            "rawBytes": value.get("rawBytes"),
            "visibleBytes": value.get("visibleBytes"),
            "freshness": value.get("freshness"),
            "evidenceId": value.get("evidenceId"),
            "error": value.get("error"),
        }.items()
        if projected is not None
    }


def project_tofu_trial(
    *,
    native_events: Path,
    runtime_evidence: Path,
    tool_audit: Path,
    runtime_config: dict[str, Any],
    expected_runtime_config_digest: str,
    expected_prompt_contract_digest: str,
    expected_tool_schema_digest: str,
) -> dict[str, Any]:
    """Reconcile one formal production AgentRuntime execution."""

    if _sha256(runtime_config) != expected_runtime_config_digest:
        raise TofuProjectionError("formal runtime config digest drifted")
    if tofu_kimi_prompt_contract_sha256(runtime_config) \
            != expected_prompt_contract_digest:
        raise TofuProjectionError("formal prompt contract digest drifted")
    if not _is_sha256(expected_tool_schema_digest):
        raise TofuProjectionError("formal tool schema digest is invalid")

    evidence = _strict_load(runtime_evidence, "Tofu runtime evidence")
    audit = _strict_load(tool_audit, "Tofu tool audit")
    observations = _strict_jsonl(native_events, "Tofu native events")
    if evidence.get("contractVersion") != _RUNTIME_CONTRACT:
        raise TofuProjectionError("Tofu runtime evidence contract drifted")
    if audit.get("contractVersion") != _TOOL_AUDIT_CONTRACT:
        raise TofuProjectionError("Tofu tool audit contract drifted")
    if evidence.get("model") != "kimi-k3" \
            or evidence.get("status") != "done" \
            or evidence.get("customToolsMode") != "exclusive":
        raise TofuProjectionError(
            "Tofu runtime did not settle on exclusive kimi-k3 success")

    event_rows: list[tuple[int, int, dict[str, Any]]] = []
    previous_observed = 0
    previous_seq = 0
    for event_index, row in enumerate(observations, 1):
        if row.get("contractVersion") != _EVENT_CONTRACT:
            raise TofuProjectionError(
                f"native observation {event_index} contract drifted")
        observed = _non_negative_integer(
            row.get("observedAtUnixNs"),
            f"native observation {event_index} timestamp",
        )
        event = row.get("event")
        if not isinstance(event, dict):
            raise TofuProjectionError(
                f"native observation {event_index} event is missing")
        seq = _non_negative_integer(
            event.get("seq"), f"native observation {event_index} seq")
        if observed < previous_observed or seq <= previous_seq:
            raise TofuProjectionError(
                "native event timestamps/sequences are not monotonic")
        previous_observed = observed
        previous_seq = seq
        event_rows.append((event_index, observed, event))

    terminals = [
        (index, event) for index, _observed, event in event_rows
        if event.get("type") in _TERMINAL_EVENTS
    ]
    if len(terminals) != 1 or terminals[0][1].get("type") != "done" \
            or terminals[0][0] != len(event_rows):
        raise TofuProjectionError(
            "native events require exactly one final done terminal")
    if any(event.get("type") == "model_fallback"
           for _index, _observed, event in event_rows):
        raise TofuProjectionError("formal kimi-k3 trial used model fallback")

    schemas = evidence.get("toolSchemas")
    if not isinstance(schemas, list) or not schemas \
            or _sha256(schemas) != expected_tool_schema_digest:
        raise TofuProjectionError("runtime tool schemas drifted")
    schema_bytes = len(_canonical_bytes(schemas))
    schema_count = len(schemas)
    allowed_tool_names = {
        str((schema.get("function") or {}).get("name") or "")
        for schema in schemas if isinstance(schema, dict)
    }
    if "" in allowed_tool_names or len(allowed_tool_names) != schema_count:
        raise TofuProjectionError("runtime tool schemas have invalid names")

    requests: dict[int, tuple[int, int, dict[str, Any]]] = {}
    round_usage_events: list[tuple[int, int, dict[str, Any]]] = []
    custom_calls: dict[str, tuple[int, int, dict[str, Any]]] = {}
    tool_completions: dict[str, tuple[int, int, dict[str, Any]]] = {}
    incidents: list[dict[str, Any]] = []
    for event_index, observed, event in event_rows:
        event_type = str(event.get("type") or "")
        if event_type == "messages_snapshot" and event.get("kind") == "request":
            round_number = _non_negative_integer(
                event.get("roundNum"), "messages_snapshot.roundNum")
            if round_number < 1 or round_number in requests:
                raise TofuProjectionError(
                    "request snapshots must be unique positive rounds")
            if event.get("model") != "kimi-k3" \
                    or not isinstance(event.get("messages"), list):
                raise TofuProjectionError("request snapshot model/messages drifted")
            round_schemas = event.get("tools")
            if not isinstance(round_schemas, list) \
                    or _sha256(round_schemas) != expected_tool_schema_digest:
                raise TofuProjectionError(
                    f"request round {round_number} tool schemas drifted")
            requests[round_number] = (event_index, observed, event)
        elif event_type == "round_usage":
            round_usage_events.append((event_index, observed, event))
        elif event_type == "custom_tool_call":
            call_id = str(event.get("callId") or "")
            if not call_id or call_id in custom_calls:
                raise TofuProjectionError("custom tool call IDs are invalid")
            custom_calls[call_id] = (event_index, observed, event)
        elif event_type == "tool_complete":
            tool_call_id = str(event.get("toolCallId") or "")
            if not tool_call_id or tool_call_id in tool_completions:
                raise TofuProjectionError("tool completion IDs are invalid")
            tool_completions[tool_call_id] = (event_index, observed, event)
        elif event_type in {"retry_reset", "budget_warning", "error"}:
            incidents.append({
                "severity": "error" if event_type == "error" else "warning",
                "code": f"tofu_{event_type}",
                "eventIndex": event_index,
                "eventDigest": _sha256(event),
            })

    api_rounds = evidence.get("apiRounds")
    context_rows = evidence.get("contextTelemetryRounds")
    if not isinstance(api_rounds, list) or not api_rounds:
        raise TofuProjectionError("runtime evidence contains no API rounds")
    if not isinstance(context_rows, list) or not context_rows:
        raise TofuProjectionError("runtime evidence contains no context rounds")
    contexts: dict[int, dict[str, Any]] = {}
    for row in context_rows:
        if not isinstance(row, dict):
            raise TofuProjectionError("context telemetry row must be an object")
        round_number = _non_negative_integer(
            row.get("round"), "context telemetry round")
        if round_number < 1 or round_number in contexts:
            raise TofuProjectionError(
                "context telemetry rounds must be unique and positive")
        contexts[round_number] = row

    if len(round_usage_events) != len(api_rounds):
        raise TofuProjectionError(
            "native round_usage count differs from billed API rounds")
    requested_profile = "auto"
    responses = runtime_config.get("responses")
    if isinstance(responses, dict):
        requested_profile = str(responses.get("promptProfile") or "auto")

    rounds: list[dict[str, Any]] = []
    context_blocks: list[dict[str, Any]] = []
    tool_schema_rows: list[dict[str, Any]] = []
    usages: list[dict[str, int]] = []
    first_output_times: list[int] = []
    model_ms = 0.0
    queue_ms = 0.0
    queue_measurements: set[str] = set()
    logical_rounds: set[int] = set()
    for call_index, (api_row, usage_event_row) in enumerate(
            zip(api_rounds, round_usage_events), 1):
        if not isinstance(api_row, dict):
            raise TofuProjectionError(f"API round {call_index} is not an object")
        logical_round = _non_negative_integer(
            api_row.get("round"), f"API round {call_index}.round")
        if logical_round < 1:
            raise TofuProjectionError("API logical rounds must be positive")
        logical_rounds.add(logical_round)
        request_row = requests.get(logical_round)
        context = contexts.get(logical_round)
        if request_row is None or context is None:
            raise TofuProjectionError(
                f"API logical round {logical_round} lacks request/context evidence")
        event_index, _observed, usage_event = usage_event_row
        if _non_negative_integer(
                usage_event.get("roundNum"), "round_usage.roundNum") \
                != logical_round:
            raise TofuProjectionError("round_usage logical round drifted")
        if usage_event.get("model") not in (None, "", "kimi-k3") \
                or api_row.get("model") not in (None, "", "kimi-k3"):
            raise TofuProjectionError("billed API round used another model")
        raw_usage = api_row.get("usage")
        usage = _canonical_usage(
            raw_usage, f"API round {call_index} usage")
        event_usage = _canonical_usage(
            usage_event.get("usage"),
            f"round_usage event {event_index} usage",
        )
        if usage != event_usage:
            raise TofuProjectionError(
                f"API round {call_index} usage differs from native event")
        (
            latency_ms, call_ttft_ms, started_ns, first_ns, completed_ns,
            call_queue_ms, queue_measurement,
        ) = _dispatch_timing(raw_usage, call_index=call_index)
        usages.append(usage)
        first_output_times.append(first_ns)
        model_ms += latency_ms
        queue_ms += call_queue_ms
        queue_measurements.add(queue_measurement)
        profile = _prompt_profile(
            context.get("promptProfile"),
            requested_profile=requested_profile,
            round_number=logical_round,
        )
        request_payload = _request_payload(request_row[2])
        request_bytes = len(_canonical_bytes(request_payload))
        request_digest = _sha256(request_payload)
        stable_prefix_tokens = _non_negative_integer(
            context.get("stablePrefixTokens"), "stablePrefixTokens")
        tool_schema_tokens = _non_negative_integer(
            context.get("toolSchemaTokens"), "toolSchemaTokens")
        raw_tool_tokens = _non_negative_integer(
            context.get("rawToolResultTokens"), "rawToolResultTokens")
        model_tool_tokens = _non_negative_integer(
            context.get("modelToolResultTokens"), "modelToolResultTokens")
        prefix_fingerprint = str(context.get("prefixFingerprint") or "")
        if not prefix_fingerprint:
            raise TofuProjectionError("context prefix fingerprint is missing")
        round_row = {
            "round": call_index,
            "logicalRound": logical_round,
            "status": "completed",
            "tag": str(api_row.get("tag") or usage_event.get("tag") or ""),
            "usage": usage,
            "requestBytes": request_bytes,
            "requestDigest": request_digest,
            "modelWallMs": latency_ms,
            "ttftMs": call_ttft_ms,
            "queueMs": call_queue_ms,
            "queueMeasurement": queue_measurement,
            "streamStartedAtUnixNs": started_ns,
            "firstModelOutputAtUnixNs": first_ns,
            "streamCompletedAtUnixNs": completed_ns,
            "promptProfile": profile,
        }
        rounds.append(round_row)
        context_blocks.append({
            "round": call_index,
            "logicalRound": logical_round,
            "kind": "tofu_production_request",
            "tokenCount": usage["prompt_tokens"],
            "cacheReadTokens": usage["cache_read_tokens"],
            "stablePrefixTokens": stable_prefix_tokens,
            "toolSchemaTokens": tool_schema_tokens,
            "rawToolResultTokens": raw_tool_tokens,
            "modelToolResultTokens": model_tool_tokens,
            "visibleBytes": request_bytes,
            "requestDigest": request_digest,
            "prefixFingerprint": prefix_fingerprint,
            "provenance": {"promptProfile": profile},
        })
        tool_schema_rows.append({
            "round": call_index,
            "logicalRound": logical_round,
            "count": schema_count,
            "visibleBytes": schema_bytes,
            "tokenCount": tool_schema_tokens,
            "sha256": expected_tool_schema_digest,
        })

    if logical_rounds != set(requests) or logical_rounds != set(contexts):
        raise TofuProjectionError(
            "request/context logical rounds differ from billed API rounds")
    main_usage = _aggregate_usage(usages)
    compaction_raw = evidence.get("compactionUsage") or {}
    if not isinstance(compaction_raw, dict):
        raise TofuProjectionError("compactionUsage must be an object")
    compaction_calls = _non_negative_integer(
        compaction_raw.get("n_calls", 0), "compactionUsage.n_calls")
    compaction_usage = _canonical_usage(
        compaction_raw, "compactionUsage")
    if compaction_calls == 0 and any(compaction_usage.values()):
        raise TofuProjectionError(
            "compaction usage has tokens but no model calls")
    compaction_timing = compaction_raw.get("timing") or {}
    if not isinstance(compaction_timing, dict):
        raise TofuProjectionError("compactionUsage.timing must be an object")
    compaction_model_ms = 0.0
    compaction_queue_ms = 0.0
    compaction_queue_measurement = ""
    compaction_first_output = 0
    compaction_ttft_measurement = ""
    if compaction_calls:
        compaction_model_ms = _non_negative_number(
            compaction_timing.get("modelWallMs"),
            "compactionUsage.timing.modelWallMs",
        )
        compaction_queue_ms = _non_negative_number(
            compaction_timing.get("queueWaitMs"),
            "compactionUsage.timing.queueWaitMs",
        )
        compaction_queue_measurement = str(
            compaction_timing.get("queueMeasurement") or "")
        compaction_first_output = _non_negative_integer(
            compaction_timing.get("firstModelOutputAtUnixNs"),
            "compactionUsage.timing.firstModelOutputAtUnixNs",
        )
        compaction_ttft_measurement = str(
            compaction_timing.get("ttftMeasurement") or "")
        if compaction_first_output <= 0 \
                or not compaction_ttft_measurement \
                or not compaction_queue_measurement:
            raise TofuProjectionError(
                "model compaction calls lack conservative timing evidence")
        queue_ms += compaction_queue_ms
        queue_measurements.add(compaction_queue_measurement)
    aggregate_usage = _canonical_usage(
        evidence.get("usage"), "runtime aggregate usage")
    if _add_usage(main_usage, compaction_usage) != aggregate_usage:
        raise TofuProjectionError(
            "runtime aggregate usage differs from main plus compaction calls")

    audit_rows = audit.get("calls")
    if not isinstance(audit_rows, list) or len(audit_rows) != len(custom_calls):
        raise TofuProjectionError(
            "tool audit count differs from native custom tool calls")
    tool_results: list[dict[str, Any]] = []
    call_graph: list[dict[str, Any]] = []
    matched_completions: set[str] = set()
    tool_ms = 0.0
    for audit_index, audit_row in enumerate(audit_rows, 1):
        if not isinstance(audit_row, dict):
            raise TofuProjectionError("tool audit call must be an object")
        call_id = str(audit_row.get("callId") or "")
        call = custom_calls.get(call_id)
        if call is None:
            raise TofuProjectionError("tool audit references an unknown call")
        call_event_index, call_observed, call_event = call
        tool_call_id = str(call_event.get("toolCallId") or "")
        completion = tool_completions.get(tool_call_id)
        if not tool_call_id or completion is None:
            raise TofuProjectionError(
                "custom tool call lacks its native completion")
        completion_index, completion_observed, completion_event = completion
        matched_completions.add(tool_call_id)
        tool_name = str(audit_row.get("toolName") or "")
        arguments = audit_row.get("arguments")
        if tool_name not in allowed_tool_names \
                or tool_name != call_event.get("toolName") \
                or tool_name != completion_event.get("toolName") \
                or not isinstance(arguments, dict) \
                or _canonical_bytes(arguments) != _canonical_bytes(
                    call_event.get("arguments")):
            raise TofuProjectionError("tool audit name/arguments drifted")
        if _non_negative_integer(
                audit_row.get("observedAtUnixNs"),
                "tool audit observedAtUnixNs") != call_observed:
            raise TofuProjectionError("tool audit observation timestamp drifted")
        resolved = _non_negative_integer(
            audit_row.get("resolvedAtUnixNs"),
            "tool audit resolvedAtUnixNs")
        if resolved < call_observed or completion_observed < resolved:
            raise TofuProjectionError("tool lifecycle timestamps are invalid")
        result = audit_row.get("result")
        visible_digest = _text_sha256(result) if isinstance(result, str) else ""
        raw_digest = audit_row.get("rawResultSha256")
        if not isinstance(result, str) \
                or audit_row.get("resultSha256") != visible_digest \
                or audit_row.get("visibleResultSha256") != visible_digest \
                or not _is_sha256(raw_digest):
            raise TofuProjectionError("tool audit result digest drifted")
        visible_bytes = len(result.encode("utf-8"))
        if _non_negative_integer(
                audit_row.get("visibleBytes"), "tool visibleBytes") \
                != visible_bytes:
            raise TofuProjectionError("tool audit visible byte count drifted")
        raw_bytes = _non_negative_integer(
            audit_row.get("rawBytes"), "tool rawBytes")
        truncated = audit_row.get("truncated")
        if not isinstance(truncated, bool) \
                or truncated and raw_bytes <= visible_bytes \
                or not truncated and (
                    raw_bytes != visible_bytes or raw_digest != visible_digest
                ):
            raise TofuProjectionError("tool audit truncation metadata drifted")
        duration_ms = _non_negative_number(
            audit_row.get("durationMs"), "tool durationMs")
        tool_ms += duration_ms
        model_content = completion_event.get("toolContent")
        if not isinstance(model_content, str):
            raise TofuProjectionError("tool completion lacks model-visible content")
        model_visible_bytes = len(model_content.encode("utf-8"))
        row = {
            "callId": call_id,
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "status": (
                "error" if audit_row.get("isError") else
                str(completion_event.get("status") or "completed")),
            "durationMs": duration_ms,
            "rawBytes": raw_bytes,
            "adapterVisibleBytes": visible_bytes,
            "visibleBytes": model_visible_bytes,
            "truncated": truncated,
            "rawResultDigest": raw_digest,
            "adapterResultDigest": visible_digest,
            "resultDigest": _text_sha256(model_content),
            "paidCostUsd": 0,
        }
        result_evidence = completion_event.get("toolResultEvidence")
        envelope = _parse_result_evidence(result_evidence)
        if envelope is not None and _non_negative_integer(
                envelope.get("visibleBytes"),
                "tool result evidence visibleBytes") != model_visible_bytes:
            raise TofuProjectionError(
                "tool result evidence visible byte count drifted")
        if envelope is None:
            envelope = _parse_envelope(model_content)
        if envelope is not None:
            row["envelope"] = envelope
        tool_results.append(row)
        call_graph.append({
            "callId": call_id,
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "callEventIndex": call_event_index,
            "completionEventIndex": completion_index,
            "round": _non_negative_integer(
                call_event.get("roundNum"), "custom tool roundNum"),
        })
        if audit_row.get("isError"):
            incidents.append({
                "severity": "error",
                "code": "tofu_custom_tool_error",
                "callId": call_id,
                "resultDigest": _text_sha256(result),
            })
    if matched_completions != set(tool_completions):
        raise TofuProjectionError(
            "native tool completions include non-audited execution")

    compaction_events = evidence.get("contextCompactionEvents")
    if not isinstance(compaction_events, list) \
            or any(not isinstance(row, dict) for row in compaction_events):
        raise TofuProjectionError("context compaction events are invalid")
    compactions = [
        {
            "kind": "tofu_context_compaction",
            "index": index,
            "eventDigest": _sha256(row),
            **json.loads(_canonical_bytes(row)),
        }
        for index, row in enumerate(compaction_events, 1)
    ]
    if compaction_calls:
        compactions.append({
            "kind": "model_compaction_aggregate",
            "callCount": compaction_calls,
            "usage": compaction_usage,
            "modelWallMs": compaction_model_ms,
            "queueMs": compaction_queue_ms,
            "queueMeasurement": compaction_queue_measurement,
            "firstModelOutputAtUnixNs": compaction_first_output,
            "ttftMeasurement": compaction_ttft_measurement,
            "timingAvailable": True,
        })

    output = evidence.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("content"), str):
        raise TofuProjectionError("runtime output evidence is missing")
    final_output = output["content"]
    if output.get("sha256") != _text_sha256(final_output) \
            or _non_negative_integer(
                output.get("charCount"), "output.charCount") != len(final_output):
        raise TofuProjectionError("runtime final output digest/count drifted")
    terminal_usage = terminals[0][1].get("usage")
    if isinstance(terminal_usage, dict) \
            and _canonical_usage(terminal_usage, "terminal usage") \
            != aggregate_usage:
        raise TofuProjectionError("terminal usage differs from runtime evidence")

    decisions = evidence.get("orchestrationDecisions") or []
    if not isinstance(decisions, list) \
            or any(not isinstance(row, dict) for row in decisions):
        raise TofuProjectionError("orchestration decision evidence is invalid")
    return {
        "rounds": rounds,
        "contextBlocks": context_blocks,
        "toolSchemas": tool_schema_rows,
        "toolResults": tool_results,
        "compactions": compactions,
        "callGraph": call_graph,
        "incidents": incidents,
        "orchestrationDecisions": json.loads(_canonical_bytes(decisions)),
        "finalOutput": final_output,
        "finalOutputDigest": _text_sha256(final_output),
        "aggregateUsage": aggregate_usage,
        "mainUsage": main_usage,
        "compactionUsage": compaction_usage,
        "compactionCalls": compaction_calls,
        "evidenceDigests": {
            "nativeEventsSha256": hashlib.sha256(
                native_events.read_bytes()).hexdigest(),
            "runtimeEvidenceSha256": hashlib.sha256(
                runtime_evidence.read_bytes()).hexdigest(),
            "toolAuditSha256": hashlib.sha256(
                tool_audit.read_bytes()).hexdigest(),
        },
        "timing": {
            "firstModelOutputAtUnixNs": min(
                first_output_times
                + ([compaction_first_output] if compaction_first_output else [])),
            "modelMs": model_ms + compaction_model_ms,
            "queueMs": queue_ms,
            "queueMeasurement": (
                next(iter(queue_measurements))
                if len(queue_measurements) == 1 else "mixed"
            ),
            "toolMs": tool_ms,
            "translationCpuMs": 0.0,
            "proxyCpuMs": 0.0,
        },
    }


def build_tofu_release_task_record(
    *,
    manifest: dict[str, Any],
    task_id: str,
    projection: dict[str, Any],
    oracle: dict[str, Any],
    artifacts: list[dict[str, Any]],
    task_started_at_unix_ns: int,
    oracle_ready_ms: float,
    queue_ms: float | None = None,
    retries: list[dict[str, Any]] | None = None,
    judges: list[dict[str, Any]] | None = None,
    incidents: list[dict[str, Any]] | None = None,
    environment: dict[str, Any] | None = None,
    completed_at_unix_ms: int | None = None,
) -> dict[str, Any]:
    """Build one exact, manifest-bound candidate record after verification."""

    try:
        validate_record(manifest)
    except BenchmarkContractError as exc:
        raise TofuProjectionError("Tofu projection manifest is invalid") from exc
    if manifest.get("recordType") != "manifest" \
            or manifest.get("comparisonRole") != "candidate":
        raise TofuProjectionError(
            "Tofu projection requires a candidate manifest")
    task_rows = {
        str(row.get("taskId") or ""): row
        for row in manifest.get("tasks") or []
    }
    task = task_rows.get(str(task_id))
    if task is None:
        raise TofuProjectionError("Tofu projection task is not in the manifest")
    if not isinstance(oracle.get("passed"), bool):
        raise TofuProjectionError("Tofu task oracle must be resolved")
    raw_wall_ms = _non_negative_number(oracle_ready_ms, "oracle_ready_ms")
    start_ns = _non_negative_integer(
        task_started_at_unix_ns, "task_started_at_unix_ns")
    first_ns = _non_negative_integer(
        (projection.get("timing") or {}).get("firstModelOutputAtUnixNs"),
        "firstModelOutputAtUnixNs",
    )
    if first_ns < start_ns:
        raise TofuProjectionError("first model output predates task start")
    ttft_ms = (first_ns - start_ns) / 1_000_000
    projection_timing = projection.get("timing") or {}
    queue_value = _non_negative_number(
        projection_timing.get("queueMs") if queue_ms is None else queue_ms,
        "queue_ms",
    )
    queue_measurement = (
        str(projection_timing.get("queueMeasurement") or "")
        if queue_ms is None else "caller_supplied"
    )
    if not queue_measurement:
        raise TofuProjectionError("queue measurement provenance is missing")

    model_cost = sum(
        public_price_cost_from_usage(
            row["usage"], manifest["priceCard"])["costUsd"]
        for row in projection["rounds"]
    )
    compaction_cost = public_price_cost_from_usage(
        projection["compactionUsage"], manifest["priceCard"])["costUsd"]
    retry_rows = list(retries or [])
    retry_model_cost = sum(
        public_price_cost_from_usage(
            usage, manifest["priceCard"])["costUsd"]
        for retry in retry_rows
        for usage in retry.get("modelUsages") or []
    )
    paid_tool_cost = sum(
        _non_negative_number(row.get("paidCostUsd", 0), "paid tool cost")
        for row in projection.get("toolResults") or []
    ) + sum(
        _non_negative_number(
            retry.get("paidToolCostUsd", 0), "retry paid tool cost")
        for retry in retry_rows
    )
    model_cost += retry_model_cost
    cost = {
        "modelCostUsd": model_cost,
        "compactionCostUsd": compaction_cost,
        "paidToolCostUsd": paid_tool_cost,
        "agentCostUsd": model_cost + compaction_cost + paid_tool_cost,
    }
    latency = {
        "rawWallMs": raw_wall_ms,
        "oracleReadyMs": raw_wall_ms,
        "queueMs": queue_value,
        "queueMeasurement": queue_measurement,
        "ttftMs": ttft_ms,
        "modelMs": _non_negative_number(
            projection["timing"]["modelMs"], "modelMs"),
        "toolMs": _non_negative_number(
            projection["timing"]["toolMs"], "toolMs"),
        "translationCpuMs": 0.0,
        "proxyCpuMs": 0.0,
        "codexFavoredCorrectedWallMs": raw_wall_ms,
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
        orchestration_decisions=list(
            projection.get("orchestrationDecisions") or []),
        artifacts=artifacts,
        completed_at=completed_at_unix_ms,
    )


__all__ = [
    "TofuProjectionError",
    "build_tofu_release_task_record",
    "project_tofu_trial",
]
