"""Codex raw/proxy evidence projection into immutable benchmark v2 tasks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluations.codex_kimi_proxy.codex_contract import benchmark_trial_token
from evaluations.long_agent_release.codex_projection import (
    CodexProjectionError,
    build_codex_release_task_record,
    project_codex_trial,
)
from evaluations.long_agent_release.run_store import (
    audit_release_run,
    initialize_release_run,
    record_release_task,
    store_run_artifact,
)
from lib.benchmark_contract import build_manifest_v2


pytestmark = pytest.mark.unit


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest() -> dict:
    return build_manifest_v2(
        run_id="codex-projection-run",
        harness={
            "name": "paired-harness", "version": "1",
            "commitSha256": _sha("harness"),
        },
        agent={
            "name": "codex", "version": "0.149.1",
            "binarySha256": _sha("codex-binary"),
        },
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-fixture",
        thinking="high",
        experiment_arm="codex_0_149_1",
        pair_id="pair-projection",
        comparison_role="baseline",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=_sha("prompt"),
        tool_schema_digest=_sha("tools"),
        dataset_snapshot={
            "id": "pilot", "sha256": _sha("dataset"), "frozen": True,
        },
        task_table=[{
            "taskId": "pilot:codex-1", "family": "pilot", "dataset": "pilot",
        }],
        sandbox={"kind": "rootless-qemu", "networkPolicy": "frozen"},
        retry_rule={
            "maxInfrastructureRetries": 1,
            "retryableFailureClasses": ["infrastructure"],
        },
        artifact_limits={
            "maximumArtifactBytes": 1_000_000,
            "maximumTaskArtifactBytes": 2_000_000,
            "maximumRunArtifactBytes": 10_000_000,
        },
        timeout_seconds=600,
        maximum_infrastructure_failure_rate=0.02,
        environment={"gitCommit": _sha("repo")},
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _evidence(tmp_path: Path, *, second_input: int = 30,
              second_status: str = "completed") -> tuple[Path, Path]:
    token = benchmark_trial_token("projection", "one")
    raw = tmp_path / "codex.jsonl"
    _write_jsonl(raw, [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {
            "id": "warning", "type": "error",
            "message": (
                "Model metadata for `kimi-k3` not found. Defaulting to fallback "
                "metadata; this can degrade performance and cause issues."
            ),
        }},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "command-1", "type": "command_execution",
            "status": "completed", "duration_ms": 12,
            "command": "pwd", "aggregated_output": "/workspace\n",
        }},
        {"type": "item.completed", "item": {
            "id": "message-1", "type": "agent_message", "text": "done",
        }},
        {"type": "turn.completed", "usage": {
            "input_tokens": 20 + second_input,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 0,
            "output_tokens": 11,
            "reasoning_output_tokens": 2,
        }},
    ])
    metrics = tmp_path / "metrics.jsonl"
    common = {
        "event": "responsesTranslation",
        "trialToken": token,
        "status": "completed",
        "clientDisconnected": False,
        "upstreamCalls": 1,
        "invalidTrial": False,
        "translationCpuNs": 2_000_000,
        "proxyCpuNs": 3_000_000,
        "upstreamWallNs": 40_000_000,
        "rawWallNs": 45_000_000,
        "requestBytes": 1000,
        "toolSchemaBytes": 400,
        "toolCount": 3,
        "toolSchemaDigest": _sha("schema"),
    }
    _write_jsonl(metrics, [
        {**common, "traceId": "trace-1", "requestDigest": "request-1",
         "startedAtUnixNs": 1_010_000_000,
         "firstUpstreamByteAtUnixNs": 1_050_000_000,
         "usage": {
             "input_tokens": 20,
             "input_tokens_details": {"cached_tokens": 0},
             "output_tokens": 6,
             "output_tokens_details": {"reasoning_tokens": 1},
         }},
        {**common, "traceId": "trace-2", "requestDigest": "request-2",
         "status": second_status,
         "startedAtUnixNs": 1_100_000_000,
         "firstUpstreamByteAtUnixNs": 1_130_000_000,
         "usage": {
             "input_tokens": second_input,
             "input_tokens_details": {"cached_tokens": 10},
             "output_tokens": 5,
             "output_tokens_details": {"reasoning_tokens": 1},
         }},
    ])
    return raw, metrics


def test_projection_reconciles_usage_and_commits_exact_v2_record(tmp_path):
    raw, metrics = _evidence(tmp_path)
    projection = project_codex_trial(
        raw_trajectory=raw, proxy_metrics=metrics)

    assert len(projection["rounds"]) == 2
    assert projection["aggregateUsage"]["prompt_tokens"] == 50
    assert projection["aggregateUsage"]["cache_read_tokens"] == 10
    assert projection["finalOutput"] == "done"
    assert projection["toolResults"][0]["toolName"] == "command_execution"
    assert projection["timing"]["toolMs"] == 12
    assert projection["incidents"] == []

    manifest = _manifest()
    run_root = tmp_path / "run"
    initialize_release_run(run_root, manifest)
    artifacts = [
        store_run_artifact(
            run_root, task_id="pilot:codex-1",
            kind="raw_trajectory", source=raw),
        store_run_artifact(
            run_root, task_id="pilot:codex-1",
            kind="proxy_metrics", source=metrics),
    ]
    record = build_codex_release_task_record(
        manifest=manifest,
        task_id="pilot:codex-1",
        projection=projection,
        oracle={"passed": True, "type": "exact"},
        artifacts=artifacts,
        task_started_at_unix_ns=1_000_000_000,
        oracle_ready_ms=200,
    )
    assert record["latency"]["ttftMs"] == 50
    assert record["latency"]["codexFavoredCorrectedWallMs"] == 196
    assert record["cost"]["agentCostUsd"] > 0
    record_release_task(run_root, record)
    assert audit_release_run(run_root, require_complete=True)["complete"] is True


def test_projection_rejects_usage_drift_between_codex_and_proxy(tmp_path):
    raw, metrics = _evidence(tmp_path, second_input=29)
    raw_rows = [json.loads(line) for line in raw.read_text().splitlines()]
    raw_rows[-1]["usage"]["input_tokens"] = 50
    _write_jsonl(raw, raw_rows)

    with pytest.raises(CodexProjectionError, match="aggregate usage"):
        project_codex_trial(raw_trajectory=raw, proxy_metrics=metrics)


def test_projection_rejects_transport_failure_as_invalid_trial(tmp_path):
    raw, metrics = _evidence(tmp_path, second_status="transport_error")

    with pytest.raises(CodexProjectionError, match="invalidate"):
        project_codex_trial(raw_trajectory=raw, proxy_metrics=metrics)
