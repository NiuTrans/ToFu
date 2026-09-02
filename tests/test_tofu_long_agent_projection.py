"""Production Tofu evidence must become exact immutable candidate records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluations.long_agent_release.run_store import (
    audit_release_run,
    initialize_release_run,
    record_release_task,
    store_run_artifact,
)
from evaluations.long_agent_release.tofu_projection import (
    TofuProjectionError,
    build_tofu_release_task_record,
    project_tofu_trial,
)
from evaluations.swebench.tofu_kimi_runtime import (
    tofu_kimi_clean_tool_schemas,
    tofu_kimi_prompt_contract_sha256,
    tofu_kimi_tool_schema_sha256,
)
from lib.benchmark_contract import build_manifest_v2


pytestmark = pytest.mark.unit


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sha_json(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _runtime_config() -> dict:
    return {
        "responses": {"promptProfile": "lean"},
        "tools": {"schemaBudgetTokens": 4_000, "resultEnvelope": "v2"},
        "context": {"globalBudgetTokens": 96_000},
        "compaction": {"strategy": "adaptive"},
        "orchestration": {"policy": "v1"},
    }


def _manifest() -> dict:
    config = _runtime_config()
    return build_manifest_v2(
        run_id="tofu-projection-run",
        harness={
            "name": "paired-harness", "version": "1",
            "commitSha256": _sha_text("harness"),
        },
        agent={
            "name": "tofu", "version": "0.17.0",
            "commitSha256": _sha_text("tofu-agent"),
        },
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-fixture",
        thinking="high",
        experiment_arm="prompt_lean_kimi",
        pair_id="pair-projection",
        comparison_role="candidate",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=tofu_kimi_prompt_contract_sha256(config),
        tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        dataset_snapshot={
            "id": "pilot", "sha256": _sha_text("dataset"), "frozen": True,
        },
        task_table=[{
            "taskId": "pilot:tofu-1", "family": "pilot", "dataset": "pilot",
        }],
        sandbox={"kind": "rootless-qemu", "networkPolicy": "frozen"},
        retry_rule={
            "maxInfrastructureRetries": 1,
            "retryableFailureClasses": ["infrastructure"],
        },
        artifact_limits={
            "maximumArtifactBytes": 1_000_000,
            "maximumTaskArtifactBytes": 3_000_000,
            "maximumRunArtifactBytes": 10_000_000,
        },
        timeout_seconds=600,
        maximum_infrastructure_failure_rate=0.02,
        environment={
            "gitCommit": _sha_text("repo"),
            "runtimeConfigSha256": _sha_json(config),
        },
    )


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _profile() -> dict:
    content = "lean production prompt"
    return {
        "contractVersion": "tofu.prompt-profile/v1",
        "requestedProfile": "lean",
        "resolvedProfile": "lean",
        "effectiveProfile": "lean",
        "status": "applied",
        "reason": "",
        "model": "kimi-k3",
        "charCount": len(content),
        "tokenCount": 4,
        "sha256": _sha_text(content),
    }


def _usage(*, prompt: int, output: int, cache: int, latency: int,
           ttft: int, started: int, queue: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "cache_read_tokens": cache,
        "reasoning_tokens": 1,
        "stream_elapsed_ms": latency,
        "_dispatch": {
            "model": "kimi-k3",
            "provider_id": "harbor-formal-kimi",
            "protocol": "openai",
            "latency_ms": latency,
            "ttft_ms": ttft,
            "queue_wait_ms": queue,
            "queue_wait_measurement": "dispatcher_backpressure_only",
            "stream_started_at_unix_ns": started,
            "first_content_at_unix_ns": started + ttft * 1_000_000,
            "stream_completed_at_unix_ns": started + latency * 1_000_000,
            "attempt": 1,
            "429_retries": 0,
        },
    }


def _evidence(
    tmp_path: Path, *, task_start_ns: int = 900_000_000,
) -> tuple[Path, Path, Path]:
    schemas = tofu_kimi_clean_tool_schemas()
    first = _usage(
        prompt=20, output=5, cache=2, latency=100, ttft=50,
        started=task_start_ns + 100_000_000, queue=7,
    )
    second = _usage(
        prompt=30, output=4, cache=10, latency=80, ttft=30,
        started=task_start_ns + 300_000_000, queue=3,
    )
    raw_result = "exit_code=0\nstdout:\nready"
    envelope = json.dumps({
        "contractVersion": "tofu.tool-result/v2",
        "status": "ok",
        "summary": "ready",
        "items": [],
        "artifactRef": None,
        "cursor": None,
        "truncated": False,
        "rawBytes": len(raw_result.encode()),
        "visibleBytes": 0,
    }, separators=(",", ":"))
    event_values = [
        {"type": "messages_snapshot", "kind": "request", "roundNum": 1,
         "model": "kimi-k3", "params": {"stream": True},
         "messages": [{"role": "user", "content": "solve"}],
         "tools": schemas},
        {"type": "round_usage", "roundNum": 1, "model": "kimi-k3",
         "tag": "R1", "usage": first},
        {"type": "custom_tool_call", "roundNum": 1,
         "toolCallId": "tool-1", "callId": "ctool_1",
         "toolName": "custom__run_command",
         "arguments": {"command": "printf ready"}},
        {"type": "tool_complete", "roundNum": 1,
         "toolCallId": "tool-1", "toolName": "custom__run_command",
         "toolContent": envelope, "status": "done",
         "tStart": 1120, "tEnd": 1170},
        {"type": "messages_snapshot", "kind": "request", "roundNum": 2,
         "model": "kimi-k3", "params": {"stream": True},
         "messages": [
             {"role": "user", "content": "solve"},
             {"role": "tool", "tool_call_id": "tool-1",
              "content": envelope},
         ], "tools": schemas},
        {"type": "delta", "content": "done"},
        {"type": "round_usage", "roundNum": 2, "model": "kimi-k3",
         "tag": "R2", "usage": second},
        {"type": "done", "finishReason": "stop", "usage": {
            "prompt_tokens": 50, "completion_tokens": 9,
            "cache_read_tokens": 12, "reasoning_tokens": 2,
        }},
    ]
    observed = [
        task_start_ns + 50_000_000,
        task_start_ns + 210_000_000,
        task_start_ns + 220_000_000,
        task_start_ns + 270_000_000,
        task_start_ns + 280_000_000,
        task_start_ns + 330_000_000,
        task_start_ns + 390_000_000,
        task_start_ns + 400_000_000,
    ]
    native = tmp_path / "events.jsonl"
    _write_jsonl(native, [
        {
            "contractVersion": "tofu.harbor-runtime-event-observation/v1",
            "observedAtUnixNs": timestamp,
            "event": {**event, "seq": index},
        }
        for index, (timestamp, event) in enumerate(
            zip(observed, event_values), 1)
    ])
    runtime = tmp_path / "runtime-evidence.json"
    output = "done"
    _write_json(runtime, {
        "contractVersion": "tofu.agent-runtime-evidence/v1",
        "requestId": "fixture",
        "taskId": "task-fixture",
        "model": "kimi-k3",
        "providerId": "harbor-formal-kimi",
        "status": "done",
        "finishReason": "stop",
        "usage": {
            "prompt_tokens": 50, "completion_tokens": 9,
            "cache_read_tokens": 12, "reasoning_tokens": 2,
        },
        "apiRounds": [
            {"round": 1, "model": "kimi-k3", "tag": "R1", "usage": first},
            {"round": 2, "model": "kimi-k3", "tag": "R2", "usage": second},
        ],
        "contextTelemetryRounds": [
            {"round": 1, "stablePrefixTokens": 5, "toolSchemaTokens": 100,
             "rawToolResultTokens": 0, "modelToolResultTokens": 0,
             "prefixFingerprint": "prefix-one", "promptProfile": _profile()},
            {"round": 2, "stablePrefixTokens": 5, "toolSchemaTokens": 100,
             "rawToolResultTokens": 8, "modelToolResultTokens": 7,
             "prefixFingerprint": "prefix-one", "promptProfile": _profile()},
        ],
        "contextCompactionEvents": [],
        "compactionUsage": {},
        "toolExposureTelemetry": {"exposedTools": 2},
        "toolSchemas": schemas,
        "customToolsMode": "exclusive",
        "programRuns": [],
        "orchestrationDecisions": [],
        "output": {
            "content": output, "charCount": len(output),
            "sha256": _sha_text(output),
        },
    })
    audit = tmp_path / "tool-audit.json"
    _write_json(audit, {
        "contractVersion": "tofu.harbor-custom-tool-audit/v1",
        "calls": [{
            "callId": "ctool_1",
            "toolName": "custom__run_command",
            "arguments": {"command": "printf ready"},
            "result": raw_result,
            "isError": False,
            "observedAtUnixNs": task_start_ns + 220_000_000,
            "resolvedAtUnixNs": task_start_ns + 260_000_000,
            "durationMs": 40,
            "resultSha256": _sha_text(raw_result),
            "rawResultSha256": _sha_text(raw_result),
            "visibleResultSha256": _sha_text(raw_result),
            "rawBytes": len(raw_result.encode()),
            "visibleBytes": len(raw_result.encode()),
            "truncated": False,
        }],
    })
    return native, runtime, audit


def _project(tmp_path: Path) -> dict:
    native, runtime, audit = _evidence(tmp_path)
    return _project_paths(native, runtime, audit)


def _project_paths(native: Path, runtime: Path, audit: Path) -> dict:
    config = _runtime_config()
    return project_tofu_trial(
        native_events=native,
        runtime_evidence=runtime,
        tool_audit=audit,
        runtime_config=config,
        expected_runtime_config_digest=_sha_json(config),
        expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(config),
        expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
    )


def test_projection_reconciles_runtime_events_tools_and_v2_record(tmp_path):
    projection = _project(tmp_path)

    assert len(projection["rounds"]) == 2
    assert projection["aggregateUsage"]["prompt_tokens"] == 50
    assert projection["aggregateUsage"]["cache_read_tokens"] == 12
    assert projection["toolResults"][0]["rawBytes"] == len(
        "exit_code=0\nstdout:\nready".encode())
    assert projection["toolResults"][0]["rawResultDigest"] == \
        _sha_text("exit_code=0\nstdout:\nready")
    assert projection["toolResults"][0]["adapterResultDigest"] == \
        _sha_text("exit_code=0\nstdout:\nready")
    assert projection["toolResults"][0]["envelope"]["status"] == "ok"
    assert projection["timing"]["modelMs"] == 180
    assert projection["timing"]["queueMs"] == 10
    assert projection["timing"]["toolMs"] == 40
    assert projection["finalOutput"] == "done"

    manifest = _manifest()
    run_root = tmp_path / "run"
    initialize_release_run(run_root, manifest)
    native = tmp_path / "events.jsonl"
    runtime = tmp_path / "runtime-evidence.json"
    audit = tmp_path / "tool-audit.json"
    artifacts = [
        store_run_artifact(
            run_root, task_id="pilot:tofu-1",
            kind="raw_trajectory", source=native),
        store_run_artifact(
            run_root, task_id="pilot:tofu-1",
            kind="runtime_evidence", source=runtime),
        store_run_artifact(
            run_root, task_id="pilot:tofu-1",
            kind="tool_audit", source=audit),
    ]
    record = build_tofu_release_task_record(
        manifest=manifest,
        task_id="pilot:tofu-1",
        projection=projection,
        oracle={"passed": True, "type": "exact"},
        artifacts=artifacts,
        task_started_at_unix_ns=900_000_000,
        oracle_ready_ms=500,
    )
    assert record["latency"]["ttftMs"] == 150
    assert record["latency"]["queueMs"] == 10
    assert record["latency"]["queueMeasurement"] \
        == "dispatcher_backpressure_only"
    assert record["latency"]["codexFavoredCorrectedWallMs"] == 500
    assert record["cost"]["agentCostUsd"] > 0
    record_release_task(run_root, record)
    assert audit_release_run(run_root, require_complete=True)["complete"] is True


def test_projection_rejects_tool_result_and_aggregate_usage_drift(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    audit_value = json.loads(audit.read_text())
    audit_value["calls"][0]["result"] = "changed"
    _write_json(audit, audit_value)
    config = _runtime_config()
    with pytest.raises(TofuProjectionError, match="result digest"):
        project_tofu_trial(
            native_events=native,
            runtime_evidence=runtime,
            tool_audit=audit,
            runtime_config=config,
            expected_runtime_config_digest=_sha_json(config),
            expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(
                config),
            expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        )

    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["usage"]["prompt_tokens"] = 51
    _write_json(runtime, evidence)
    with pytest.raises(TofuProjectionError, match="aggregate usage"):
        project_tofu_trial(
            native_events=native,
            runtime_evidence=runtime,
            tool_audit=audit,
            runtime_config=config,
            expected_runtime_config_digest=_sha_json(config),
            expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(
                config),
            expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        )


def test_projection_rejects_prompt_or_schema_contract_drift(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    config = _runtime_config()
    with pytest.raises(TofuProjectionError, match="prompt contract"):
        project_tofu_trial(
            native_events=native,
            runtime_evidence=runtime,
            tool_audit=audit,
            runtime_config=config,
            expected_runtime_config_digest=_sha_json(config),
            expected_prompt_contract_digest=_sha_text("different"),
            expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        )
    with pytest.raises(TofuProjectionError, match="tool schemas drifted"):
        project_tofu_trial(
            native_events=native,
            runtime_evidence=runtime,
            tool_audit=audit,
            runtime_config=config,
            expected_runtime_config_digest=_sha_json(config),
            expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(
                config),
            expected_tool_schema_digest=_sha_text("different"),
        )


def test_projection_counts_model_compaction_cost_and_conservative_ttft(tmp_path):
    task_start_ns = 900_000_000
    native, runtime, audit = _evidence(
        tmp_path, task_start_ns=task_start_ns)
    evidence = json.loads(runtime.read_text())
    evidence["usage"].update({
        "prompt_tokens": 60, "completion_tokens": 11,
        "cache_read_tokens": 12, "reasoning_tokens": 3,
    })
    evidence["compactionUsage"] = {
        "n_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "reasoning_tokens": 1,
        "timing": {
            "modelWallMs": 50,
            "queueWaitMs": 4,
            "queueMeasurement": "dispatcher_backpressure_only",
            "firstModelOutputAtUnixNs": task_start_ns + 75_000_000,
            "ttftMeasurement": "nonstream_response_complete_upper_bound",
        },
    }
    evidence["contextCompactionEvents"] = [{
        "trigger": "economic", "tokensBefore": 1000, "tokensAfter": 500,
        "evidenceRetained": ["goal"], "evidenceLost": [],
    }]
    _write_json(runtime, evidence)
    native_rows = [json.loads(line) for line in native.read_text().splitlines()]
    native_rows[-1]["event"]["usage"].update({
        "prompt_tokens": 60, "completion_tokens": 11,
        "cache_read_tokens": 12, "reasoning_tokens": 3,
    })
    _write_jsonl(native, native_rows)
    config = _runtime_config()

    projection = project_tofu_trial(
        native_events=native,
        runtime_evidence=runtime,
        tool_audit=audit,
        runtime_config=config,
        expected_runtime_config_digest=_sha_json(config),
        expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(config),
        expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
    )

    assert projection["compactionCalls"] == 1
    assert projection["timing"]["modelMs"] == 230
    assert projection["timing"]["queueMs"] == 14
    assert projection["timing"]["firstModelOutputAtUnixNs"] \
        == task_start_ns + 75_000_000
    assert projection["compactions"][-1]["timingAvailable"] is True


@pytest.mark.parametrize("invalid_call_id", ["empty", "duplicate"])
def test_projection_rejects_empty_or_duplicate_custom_call_ids(
        tmp_path, invalid_call_id):
    native, runtime, audit = _evidence(tmp_path)
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    call_row = next(
        row for row in rows
        if row["event"].get("type") == "custom_tool_call"
    )
    if invalid_call_id == "empty":
        call_row["event"]["callId"] = ""
    else:
        replacement = next(
            row for row in rows if row["event"].get("type") == "delta"
        )
        sequence = replacement["event"]["seq"]
        replacement["event"] = {**call_row["event"], "seq": sequence}
    _write_jsonl(native, rows)

    with pytest.raises(
            TofuProjectionError, match="custom tool call IDs are invalid"):
        _project_paths(native, runtime, audit)


def test_projection_rejects_missing_queue_measurement(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["apiRounds"][0]["usage"]["_dispatch"].pop("queue_wait_ms")
    _write_json(runtime, evidence)
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    first_usage = next(
        row["event"]["usage"] for row in rows
        if row["event"].get("type") == "round_usage"
        and row["event"].get("roundNum") == 1
    )
    first_usage["_dispatch"].pop("queue_wait_ms")
    _write_jsonl(native, rows)

    with pytest.raises(TofuProjectionError, match="queue_wait_ms"):
        _project_paths(native, runtime, audit)


def test_projection_rejects_model_fallback_event(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    delta = next(row for row in rows if row["event"].get("type") == "delta")
    delta["event"] = {
        "type": "model_fallback",
        "seq": delta["event"]["seq"],
        "fromModel": "kimi-k3",
        "toModel": "another-model",
    }
    _write_jsonl(native, rows)

    with pytest.raises(TofuProjectionError, match="model fallback"):
        _project_paths(native, runtime, audit)


def test_projection_rejects_multiple_terminal_events(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    delta = next(row for row in rows if row["event"].get("type") == "delta")
    delta["event"] = {
        "type": "done",
        "seq": delta["event"]["seq"],
        "finishReason": "stop",
    }
    _write_jsonl(native, rows)

    with pytest.raises(
            TofuProjectionError,
            match="exactly one final done terminal"):
        _project_paths(native, runtime, audit)
