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
    model_result = "ready"
    result_evidence = {
        "contractVersion": "tofu.tool-result-evidence/v1",
        "resultContractVersion": "tofu.tool-result/v2",
        "status": "ok",
        "projectionKind": "text",
        "truncated": False,
        "rawBytes": len(raw_result.encode()),
        "visibleBytes": len(model_result.encode()),
        "envelopeBytes": 256,
        "evidenceId": "ev_test",
    }
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
         "toolContent": model_result,
         "toolResultEvidence": result_evidence, "status": "done",
         "tStart": 1120, "tEnd": 1170},
        {"type": "messages_snapshot", "kind": "request", "roundNum": 2,
         "model": "kimi-k3", "params": {"stream": True},
         "messages": [
             {"role": "user", "content": "solve"},
             {"role": "tool", "tool_call_id": "tool-1",
              "content": model_result},
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


def _project_paths(native: Path, runtime: Path, audit: Path, *, expected_model='kimi-k3') -> dict:
    config = _runtime_config()
    return project_tofu_trial(
        native_events=native,
        runtime_evidence=runtime,
        tool_audit=audit,
        runtime_config=config,
        expected_runtime_config_digest=_sha_json(config),
        expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(config),
        expected_tool_schema_digest=tofu_kimi_tool_schema_sha256(),
        expected_model=expected_model,
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
    # Production evidence bills compaction as a ``compaction-<k>`` API round
    # alongside the agent rounds (tb21 v11 shape).
    evidence["apiRounds"].append({
        "round": "compaction-1", "model": "kimi-k3", "tag": "COMPACTION-L2",
        "usage": _usage(prompt=10, output=2, cache=0, latency=50, ttft=25,
                        started=task_start_ns + 50_000_000, queue=4),
    })
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


def test_glm_projection_requires_the_registered_physical_model(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    for path in (native, runtime, audit):
        path.write_text(path.read_text().replace('"kimi-k3"', '"glm-5.3-m17"'))
    projection = _project_paths(native, runtime, audit, expected_model='glm-5.3-m17')
    assert projection['finalOutput'] == 'done'
    with pytest.raises(TofuProjectionError, match='exclusive kimi-k3'):
        _project_paths(native, runtime, audit)
    evidence = json.loads(runtime.read_text())
    evidence['apiRounds'][0]['usage']['_dispatch']['model'] = 'kimi-k3'
    runtime.write_text(json.dumps(evidence))
    with pytest.raises(TofuProjectionError, match='different physical model'):
        _project_paths(native, runtime, audit, expected_model='glm-5.3-m17')


@pytest.mark.parametrize('wrong_field', [None, 'logical', 'physical'])
def test_projection_distinguishes_canonical_model_from_exact_wire_alias(tmp_path, wrong_field):
    native, runtime, audit = _evidence(tmp_path)
    for path in (native, runtime, audit):
        path.write_text(path.read_text().replace('"kimi-k3"', '"glm-5.3"'))
    evidence = json.loads(runtime.read_text())
    for row in evidence['apiRounds']:
        row['usage']['_dispatch']['model'] = 'glm-5.3-m17'
    if wrong_field == 'logical':
        evidence['apiRounds'][0]['model'] = 'glm-5.2'
    elif wrong_field == 'physical':
        evidence['apiRounds'][0]['usage']['_dispatch']['model'] = 'glm-5.3-other'
    _write_json(runtime, evidence)
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    for row in rows:
        usage = row['event'].get('usage') or {}
        if usage.get('_dispatch'):
            usage['_dispatch']['model'] = 'glm-5.3-m17'
    _write_jsonl(native, rows)
    if wrong_field:
        with pytest.raises(TofuProjectionError, match='another model|different physical model'):
            _project_paths(native, runtime, audit, expected_model='glm-5.3-m17')
    else:
        assert _project_paths(native, runtime, audit, expected_model='glm-5.3-m17')['finalOutput'] == 'done'


def _insert_native_event(native: Path, event: dict, *, before_done_ms: int = 5):
    """Insert one event before the final done row, re-sequencing seq/stamps."""
    rows = [json.loads(line) for line in native.read_text().splitlines()]
    events = [row["event"] for row in rows]
    stamps = [row["observedAtUnixNs"] for row in rows]
    events.insert(-1, event)
    stamps.insert(-1, stamps[-1] - before_done_ms * 1_000_000)
    _write_jsonl(native, [
        {
            "contractVersion": "tofu.harbor-runtime-event-observation/v1",
            "observedAtUnixNs": stamp,
            "event": {**value, "seq": index},
        }
        for index, (stamp, value) in enumerate(zip(stamps, events), 1)
    ])


def _augmented_runtime(runtime: Path) -> None:
    evidence = json.loads(runtime.read_text())
    evidence["customToolsMode"] = "augment"
    _write_json(runtime, evidence)


def _production_project(native: Path, runtime: Path, audit: Path) -> dict:
    config = _runtime_config()
    catalog_digest = tofu_kimi_tool_schema_sha256()
    return project_tofu_trial(
        native_events=native,
        runtime_evidence=runtime,
        tool_audit=audit,
        runtime_config=config,
        expected_runtime_config_digest=_sha_json(config),
        expected_prompt_contract_digest=tofu_kimi_prompt_contract_sha256(
            config, tool_surface="production",
            tool_catalog_sha256=catalog_digest),
        expected_tool_schema_digest=catalog_digest,
        tool_surface="production",
    )


def test_projection_reconciles_production_surface_catalog_tool_completions(
        tmp_path):
    """Production runs mix audited custom tools with unaudited catalog tools."""
    native, runtime, audit = _evidence(tmp_path)
    _augmented_runtime(runtime)
    _insert_native_event(native, {
        "type": "tool_complete", "roundNum": 2, "toolCallId": "tool-catalog-1",
        "toolName": "read_files", "toolContent": "file body",
        "status": "done", "tStart": 1200, "tEnd": 1250,
    })

    projection = _production_project(native, runtime, audit)

    assert len(projection["rounds"]) == 2
    assert projection["toolResults"]


def test_projection_rejects_unaudited_custom_completion_in_production(
        tmp_path):
    """Custom (custom__*) completions must stay audited on the production
    surface — only catalog tools are exempt."""
    native, runtime, audit = _evidence(tmp_path)
    _augmented_runtime(runtime)
    _insert_native_event(native, {
        "type": "tool_complete", "roundNum": 2, "toolCallId": "tool-rogue",
        "toolName": "custom__mystery", "toolContent": "x",
        "status": "done", "tStart": 1200, "tEnd": 1250,
    })

    with pytest.raises(TofuProjectionError, match="non-audited"):
        _production_project(native, runtime, audit)


def test_projection_rejects_catalog_completion_on_exclusive_surface(
        tmp_path):
    """Exclusive surface: every completion must be custom-audited (v11 rule)."""
    native, runtime, audit = _evidence(tmp_path)
    _insert_native_event(native, {
        "type": "tool_complete", "roundNum": 2, "toolCallId": "tool-catalog-1",
        "toolName": "read_files", "toolContent": "file body",
        "status": "done", "tStart": 1200, "tEnd": 1250,
    })

    with pytest.raises(TofuProjectionError, match="non-audited"):
        _project_paths(native, runtime, audit)


def test_projection_reconciles_billed_compaction_round_with_streaming_ttft(
        tmp_path):
    """v11 shape: billed ``compaction-<k>`` round, no ttftMeasurement label —
    the streaming measured first-content timestamp derives the label."""
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
        },
    }
    evidence["apiRounds"].append({
        "round": "compaction-1", "model": "kimi-k3", "tag": "COMPACTION-L2",
        "usage": _usage(prompt=10, output=2, cache=0, latency=50, ttft=25,
                        started=task_start_ns + 50_000_000, queue=4),
    })
    _write_json(runtime, evidence)
    native_rows = [json.loads(line) for line in native.read_text().splitlines()]
    native_rows[-1]["event"]["usage"].update({
        "prompt_tokens": 60, "completion_tokens": 11,
        "cache_read_tokens": 12, "reasoning_tokens": 3,
    })
    _write_jsonl(native, native_rows)

    projection = _project_paths(native, runtime, audit)

    assert projection["compactionCalls"] == 1
    assert projection["compactions"][-1]["timingAvailable"] is True
    assert "streaming_first_content_measured" in json.dumps(projection)


def test_projection_rejects_compaction_billed_round_count_drift(tmp_path):
    """compactionUsage.n_calls must equal the billed compaction-<k> rounds."""
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
    _write_json(runtime, evidence)
    native_rows = [json.loads(line) for line in native.read_text().splitlines()]
    native_rows[-1]["event"]["usage"].update({
        "prompt_tokens": 60, "completion_tokens": 11,
        "cache_read_tokens": 12, "reasoning_tokens": 3,
    })
    _write_jsonl(native, native_rows)

    with pytest.raises(
            TofuProjectionError,
            match="compaction rounds differ|compactionUsage"):
        _project_paths(native, runtime, audit)


def _aborted_timeout_evidence(tmp_path: Path):
    """tb21 v12 shape: timeout abort mid tool-call, no done terminal.

    The final custom_tool_call has no completion/audit row, the stream has
    no done terminal, and runtime evidence records finishReason=aborted.
    """
    native, runtime, audit = _evidence(tmp_path)
    # drop the done terminal
    native_rows = [json.loads(line) for line in native.read_text().splitlines()]
    native_rows = [row for row in native_rows if row["event"].get("type") != "done"]
    # add an in-flight custom_tool_call with no completion (seq continues)
    last_seq = native_rows[-1]["event"]["seq"]
    last_obs = native_rows[-1]["observedAtUnixNs"]
    native_rows.append({
        "contractVersion": "tofu.harbor-runtime-event-observation/v1",
        "observedAtUnixNs": last_obs + 10_000_000,
        "event": {
            "type": "custom_tool_call", "roundNum": 2,
            "toolCallId": "tool-dangling", "callId": "ctool_dangling",
            "toolName": "custom__run_command",
            "arguments": {"command": "sleep 9999"},
            "seq": last_seq + 1,
        },
    })
    _write_jsonl(native, native_rows)
    # runtime evidence: aborted finish
    evidence = json.loads(runtime.read_text())
    evidence["finishReason"] = "aborted"
    evidence["output"] = {"content": "", "charCount": 0, "sha256": _sha_text("")}
    _write_json(runtime, evidence)
    return native, runtime, audit


def test_projection_accepts_bounded_timeout_abort_without_done_terminal(tmp_path):
    native, runtime, audit = _aborted_timeout_evidence(tmp_path)

    projection = _project_paths(native, runtime, audit)

    # only the resolved tool call is reconciled; the dangling one is bounded
    assert len(projection["toolResults"]) == 1
    assert projection["finalOutput"] == ""


def test_projection_rejects_dangling_tool_call_outside_abort(tmp_path):
    native, runtime, audit = _aborted_timeout_evidence(tmp_path)
    # flip back to a clean stop finish so the dangling call is unexplained
    evidence = json.loads(runtime.read_text())
    evidence["finishReason"] = "stop"
    _write_json(runtime, evidence)

    with pytest.raises(
            TofuProjectionError,
            match="done terminal|lack completions outside an abort"):
        _project_paths(native, runtime, audit)


def test_projection_tolerates_unobserved_ttft_on_incomplete_stream(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    # simulate a premature_close round: null ttft/first-content (tb21 v12)
    evidence = json.loads(runtime.read_text())
    evidence["apiRounds"][0]["usage"]["_dispatch"]["ttft_ms"] = None
    evidence["apiRounds"][0]["usage"]["_dispatch"]["first_content_at_unix_ns"] = None
    _write_json(runtime, evidence)

    projection = _project_paths(native, runtime, audit)

    # falls back to latency as the conservative upper bound
    assert projection["rounds"][0]["ttftMs"] == 100
    assert projection["rounds"][0]["firstModelOutputAtUnixNs"] == \
        projection["rounds"][0]["streamCompletedAtUnixNs"]


@pytest.mark.parametrize("target", ["all", "api_round", "aggregate", "round_event", "terminal"])
def test_projection_rejects_missing_provider_counters(tmp_path, target):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    events = [json.loads(line) for line in native.read_text().splitlines()]

    def remove_counters(value):
        if isinstance(value, list):
            for child in value:
                remove_counters(child)
        elif isinstance(value, dict):
            for key, child in list(value.items()):
                if key == "usage" and isinstance(child, dict):
                    value[key] = {k: v for k, v in child.items()
                                  if k.startswith("_") or k == "stream_elapsed_ms"}
                else:
                    remove_counters(child)

    if target == "all":
        remove_counters(evidence)
        remove_counters(events)
    elif target == "api_round":
        remove_counters(evidence["apiRounds"][0])
    elif target == "aggregate":
        evidence["usage"] = {}
    else:
        event_type = "round_usage" if target == "round_event" else "done"
        for row in events:
            if row.get("event", {}).get("type") == event_type:
                remove_counters(row)
    runtime.write_text(json.dumps(evidence))
    native.write_text("".join(json.dumps(row) + "\n" for row in events))
    with pytest.raises(TofuProjectionError, match="missing input tokens"):
        _project_paths(native, runtime, audit)


@pytest.mark.parametrize("compaction,reason", [
    ({"n_calls": 0, "total_tokens": 1}, "tokens but no model calls"),
    ({"n_calls": 0, "prompt_tokens": 1}, "tokens but no model calls"),
    ({"n_calls": 0, "prompt_tokens_details": []}, "must be an object"),
])
def test_zero_call_compaction_cannot_hide_reported_or_malformed_tokens(tmp_path, compaction, reason):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["compactionUsage"] = compaction
    runtime.write_text(json.dumps(evidence))
    with pytest.raises(TofuProjectionError, match=reason):
        _project_paths(native, runtime, audit)


@pytest.mark.parametrize("accounting,reason", [
    ({"complete": False, "observedTokens": None, "scope": "unobserved", "rootTaskId": ""}, "incomplete"),
    ({"complete": True, "observedTokens": 60, "scope": "execution_tree", "rootTaskId": "task-fixture"}, "differs from the execution account"),
    ({"complete": True, "observedTokens": 59, "scope": "execution_tree", "rootTaskId": "another-task"}, "execution tree witness"),
    ({"complete": None, "observedTokens": 59, "scope": "unobserved", "rootTaskId": ""}, "unobserved"),
    ({}, "required observation fields"),
])
def test_successful_projection_cannot_hide_incomplete_or_mismatched_account(tmp_path, accounting, reason):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["usageAccounting"] = accounting
    runtime.write_text(json.dumps(evidence))
    with pytest.raises(TofuProjectionError, match=reason):
        _project_paths(native, runtime, audit)


def test_successful_projection_accepts_matching_execution_tree_witness(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["usageAccounting"] = {
        "complete": True, "observedTokens": 59, "scope": "execution_tree", "rootTaskId": "task-fixture"}
    runtime.write_text(json.dumps(evidence))
    assert _project_paths(native, runtime, audit)["aggregateUsage"]["total_tokens"] == 59



def test_compaction_round_requires_typed_provider_counters(tmp_path):
    native, runtime, audit = _evidence(tmp_path)
    evidence = json.loads(runtime.read_text())
    evidence["apiRounds"].append({"round": "compaction-1", "usage": [1]})
    evidence["compactionUsage"] = {"n_calls": 1, "prompt_tokens": 0, "completion_tokens": 0}
    runtime.write_text(json.dumps(evidence))
    with pytest.raises(TofuProjectionError, match="usage must be an object"):
        _project_paths(native, runtime, audit)
