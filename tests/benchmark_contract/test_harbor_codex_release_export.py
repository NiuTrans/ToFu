"""Harbor lifecycle evidence must become exact immutable baseline records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evaluations.codex_kimi_proxy.codex_contract import benchmark_trial_token
from evaluations.long_agent_release import harbor_codex_export
from evaluations.long_agent_release.harbor_codex_export import (
    HarborCodexExportError,
    _ordered_trial_pairs,
    export_codex_harbor_run,
)
from evaluations.long_agent_release.harbor_tracking import (
    claim_codex_harbor_release_attempts,
)
from evaluations.long_agent_release.run_store import (
    audit_release_attempts,
    audit_release_run,
    claim_release_task_attempts,
    fail_release_task_attempt,
    initialize_release_run,
    store_run_artifact,
)
from evaluations.swebench.codex_kimi_runtime import CODEX_KIMI_AGENT
from evaluations.swebench.constants import BENCHMARKS, HARBOR_COMMIT
from evaluations.swebench.audit import _codex_kimi_trial_evidence_checks
from lib.benchmark_contract import build_manifest_v2


pytestmark = pytest.mark.unit
_SOURCE_TASK = "swe-bench/django__django-11099"
_SOURCE_REF = "sha256:" + "f" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _harness() -> dict:
    return {
        "name": "harbor",
        "version": "harbor 0.21.0",
        "binarySha256": _sha("harbor-binary"),
        "sourceCommit": HARBOR_COMMIT,
        "runnerFrameworkVersion": "1.3.0",
        "runnerProjectRevision": "project-revision",
        "runnerProjectDirty": False,
    }


def _sandbox() -> dict:
    return {
        "kind": "rootless-qemu",
        "networkPolicy": "rootless-restricted-task-egress-v1",
        "baseDiskSha256": _sha("base-disk"),
        "qemuSha256": _sha("qemu"),
        "qemuImgSha256": _sha("qemu-img"),
        "vmCpus": 2,
        "egressMaxBytes": 4 * 1024**3,
        "egressGlobalConcurrency": 16,
    }


def _release_manifest(*, max_retries: int = 0) -> dict:
    return build_manifest_v2(
        run_id="release-baseline",
        harness=_harness(),
        agent={
            "name": "codex", "version": "0.149.1",
            "binarySha256": _sha("codex-binary"),
        },
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-a",
        thinking="high",
        experiment_arm="codex_0_149_1",
        pair_id="pair-a",
        comparison_role="baseline",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=_sha("prompt"),
        tool_schema_digest=_sha("tools"),
        dataset_snapshot={
            "id": "software-pilot", "sha256": _sha("dataset"),
            "frozen": True,
        },
        task_table=[{
            "taskId": "swe-bench-verified:django__django-11099",
            "family": "software_engineering",
            "dataset": "swe-bench-verified",
            "sourceTaskId": _SOURCE_TASK,
            "sourceSha256": _SOURCE_REF,
            "trialIndex": 1,
        }],
        sandbox=_sandbox(),
        retry_rule={
            "maxInfrastructureRetries": max_retries,
            "retryableFailureClasses": ["infrastructure"],
        },
        artifact_limits={
            "maximumArtifactBytes": 1_000_000,
            "maximumTaskArtifactBytes": 3_000_000,
            "maximumRunArtifactBytes": 10_000_000,
        },
        timeout_seconds=3600,
        maximum_infrastructure_failure_rate=0.02,
        environment={"gitCommit": _sha("repo")},
    )


def _harbor_run(tmp_path: Path, *, max_retries: int = 0,
                observed_retries: int = 0) -> Path:
    run = tmp_path / "harbor-run"
    run.mkdir(mode=0o700, parents=True)
    job_config = {
        "n_attempts": 1,
        "timeout_multiplier": 1.0,
        "retry": {"max_retries": max_retries},
        "datasets": [{
            "name": BENCHMARKS["swebench-verified"].dataset.rsplit("@", 1)[0],
            "ref": BENCHMARKS["swebench-verified"].dataset.rsplit("@", 1)[1],
            "task_names": [_SOURCE_TASK],
        }],
    }
    _write_json(run / "job-config.json", job_config)
    harbor_manifest = {
        "kind": "harbor-agent-evaluation",
        "status": "succeeded",
        "run_id": "harbor-job",
        "benchmark": "swebench-verified",
        "expected_trials": 1,
        "agent": CODEX_KIMI_AGENT,
        "models": ["kimi-k3"],
        "reasoning_effort": "high",
        "agent_version": "0.149.1",
        "backend": "rootless-qemu",
        "job_config": str((run / "job-config.json").resolve()),
        "harness_identity": _harness(),
        "sandbox_identity": _sandbox(),
        "codex_kimi_runtime": {
            "codexSha256": _sha("codex-binary"),
            "providerFace": "meituan-chat",
            "providerSlotId": "kimi-slot-a",
            "agentTimeoutSeconds": 3600,
        },
    }
    _write_json(run / "manifest.json", harbor_manifest)
    job = run / "jobs" / "harbor-job"
    trial_name = "django__django-11099__abc1234"
    trial = job / trial_name
    task = {"name": _SOURCE_TASK, "ref": _SOURCE_REF}
    trial_config = {"task": task}
    _write_json(trial / "config.json", trial_config)
    _write_json(job / "result.json", {
        "id": "11111111-1111-1111-1111-111111111111",
        "n_total_trials": 1,
        "stats": {"n_retries": observed_retries},
    })
    started = datetime(2099, 1, 1, tzinfo=timezone.utc)
    _write_json(trial / "result.json", {
        "id": "22222222-2222-2222-2222-222222222222",
        "task_name": "django__django-11099",
        "trial_name": trial_name,
        "task_checksum": _sha("task-checksum"),
        "config": trial_config,
        "agent_info": {
            "name": "codex-kimi-guest",
            "version": "0.149.1",
            "model_info": {"name": "kimi-k3"},
        },
        "verifier_result": {"rewards": {"reward": 1}},
        "exception_info": None,
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(milliseconds=300)).isoformat(),
        "environment_setup": {
            "started_at": started.isoformat(),
            "finished_at": (started + timedelta(milliseconds=20)).isoformat(),
        },
        "agent_setup": {
            "started_at": (started + timedelta(milliseconds=20)).isoformat(),
            "finished_at": (started + timedelta(milliseconds=40)).isoformat(),
        },
        "agent_execution": {
            "started_at": (started + timedelta(milliseconds=40)).isoformat(),
            "finished_at": (started + timedelta(milliseconds=160)).isoformat(),
        },
        "verifier": {
            "started_at": (started + timedelta(milliseconds=160)).isoformat(),
            "finished_at": (started + timedelta(milliseconds=200)).isoformat(),
        },
    })
    token = benchmark_trial_token("export-fixture")
    evidence = trial / "agent" / "codex-kimi-evidence"
    _write_jsonl(evidence / "codex-events.jsonl", [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "message-1", "type": "agent_message", "text": "done",
        }},
        {"type": "turn.completed", "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 5,
            "reasoning_output_tokens": 1,
        }},
    ])
    _write_jsonl(evidence / "proxy-metrics.jsonl", [{
        "event": "responsesTranslation",
        "trialToken": token,
        "traceId": "trace-1",
        "requestDigest": "request-1",
        "status": "completed",
        "clientDisconnected": False,
        "upstreamCalls": 1,
        "invalidTrial": False,
        "translationCpuNs": 2_000_000,
        "proxyCpuNs": 3_000_000,
        "upstreamWallNs": 100_000_000,
        "rawWallNs": 105_000_000,
        "startedAtUnixNs": int(started.timestamp() * 1_000_000_000) + 40_000_000,
        "firstUpstreamByteAtUnixNs": (
            int(started.timestamp() * 1_000_000_000) + 60_000_000
        ),
        "requestBytes": 1000,
        "toolSchemaBytes": 400,
        "toolCount": 3,
        "toolSchemaDigest": _sha("schema"),
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }])
    _write_json(trial / "agent" / "trajectory.json", {
        "schema_version": "ATIF-v1.7",
        "agent": {"extra": {
            "binary_sha256": _sha("codex-binary"),
            "trial_token": token,
        }},
        "steps": [{"step_id": 1, "source": "agent", "message": "done"}],
    })
    return run


def _bind_tracking(release_root: Path, harbor_run: Path) -> None:
    manifest_path = harbor_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    config = json.loads((harbor_run / "job-config.json").read_text())
    manifest["release_attempt_tracking"] = (
        claim_codex_harbor_release_attempts(
            release_run_root=release_root,
            harbor_manifest=manifest,
            job_config=config,
        )
    )
    manifest["release_evidence_eligible"] = True
    _write_json(manifest_path, manifest)


def test_export_uses_outer_trial_start_and_verifier_finish(
    tmp_path, monkeypatch,
):
    release_root = tmp_path / "release"
    initialize_release_run(release_root, _release_manifest())
    harbor_run = _harbor_run(tmp_path)
    _bind_tracking(release_root, harbor_run)
    monkeypatch.setattr(
        harbor_codex_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )

    report = export_codex_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root,
    )
    repeated = export_codex_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root,
    )

    assert report["trials"] == 1
    assert report["records"] == {"created": 1}
    assert repeated["records"] == {"unchanged": 1}
    audit = audit_release_run(release_root, require_complete=True)
    assert audit["complete"] is True
    task_path = next((release_root / "tasks").glob("*.json"))
    record = json.loads(task_path.read_text())
    assert record["providerSlotId"] == "kimi-slot-a"
    assert record["latency"]["rawWallMs"] == 200
    assert record["latency"]["codexFavoredCorrectedWallMs"] == 198
    assert record["environment"]["harbor"]["latencyScope"] \
        == "trial.started_at_to_verifier.finished_at"
    assert {row["kind"] for row in record["artifacts"]} == {
        "raw_trajectory", "proxy_metrics", "agent_trajectory",
    }


def test_export_rejects_a_formal_run_that_was_not_preclaimed(
    tmp_path, monkeypatch,
):
    release_root = tmp_path / "untracked-release"
    initialize_release_run(release_root, _release_manifest())
    monkeypatch.setattr(
        harbor_codex_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )

    with pytest.raises(HarborCodexExportError, match="not preclaimed"):
        export_codex_harbor_run(
            harbor_run_dir=_harbor_run(tmp_path),
            release_run_root=release_root,
        )


def test_export_rejects_provider_drift_and_erased_retry_evidence(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        harbor_codex_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )
    drift_root = tmp_path / "drift-release"
    drift = _release_manifest()
    drift["providerSlotId"] = "other-slot"
    initialize_release_run(drift_root, drift)
    with pytest.raises(HarborCodexExportError, match="controls drifted"):
        export_codex_harbor_run(
            harbor_run_dir=_harbor_run(tmp_path / "drift"),
            release_run_root=drift_root,
        )

    configured_retry_root = tmp_path / "configured-retry-release"
    initialize_release_run(
        configured_retry_root, _release_manifest(max_retries=1)
    )
    with pytest.raises(HarborCodexExportError, match="evidence-erasing"):
        export_codex_harbor_run(
            harbor_run_dir=_harbor_run(
                tmp_path / "retry", max_retries=1, observed_retries=1,
            ),
            release_run_root=configured_retry_root,
        )

    observed_retry_root = tmp_path / "observed-retry-release"
    initialize_release_run(observed_retry_root, _release_manifest())
    with pytest.raises(HarborCodexExportError, match="delete failed-attempt"):
        export_codex_harbor_run(
            harbor_run_dir=_harbor_run(
                tmp_path / "observed-retry", observed_retries=1,
            ),
            release_run_root=observed_retry_root,
        )


def test_export_accounts_for_external_failed_attempt_usage_and_artifacts(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        harbor_codex_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )
    release_root = tmp_path / "external-retry-release"
    initialize_release_run(release_root, _release_manifest(max_retries=1))
    task_id = "swe-bench-verified:django__django-11099"
    failed_claim = claim_release_task_attempts(
        release_root, task_ids=[task_id], execution_id="failed-harbor-job",
        runner_kind="harbor-codex",
    )
    failed_raw = tmp_path / "failed-codex.jsonl"
    _write_jsonl(failed_raw, [{"type": "turn.failed", "code": "truncated"}])
    failed_usage = {
        "input_tokens": 20,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 1},
    }
    failed_metrics = tmp_path / "failed-proxy.jsonl"
    _write_jsonl(failed_metrics, [{
        "event": "responsesTranslation",
        "trialToken": benchmark_trial_token("failed-export-fixture"),
        "traceId": "failed-trace",
        "requestDigest": _sha("failed-request"),
        "status": "failed",
        "clientDisconnected": False,
        "upstreamCalls": 1,
        "invalidTrial": False,
        "translationCpuNs": 2_000_000,
        "proxyCpuNs": 3_000_000,
        "rawWallNs": 5_000_000,
        "usage": failed_usage,
    }])
    failed_artifacts = [
        store_run_artifact(
            release_root, task_id=task_id,
            kind="failed_attempt_trajectory", source=failed_raw,
        ),
        store_run_artifact(
            release_root, task_id=task_id,
            kind="failed_attempt_proxy_metrics", source=failed_metrics,
        ),
    ]
    fail_release_task_attempt(
        release_root, task_id=task_id, execution_id="failed-harbor-job",
        code="stream_truncated", model_usages=[failed_usage],
        paid_tool_cost_usd=0, artifacts=failed_artifacts,
        no_paid_calls=False,
        task_started_at_unix_ms=failed_claim["claims"][0]["occurredAt"],
    )
    harbor_run = _harbor_run(tmp_path / "successful-retry")
    _bind_tracking(release_root, harbor_run)

    report = export_codex_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root)

    assert report["records"] == {"created": 1}
    ledger = audit_release_attempts(release_root, require_complete=True)
    assert ledger["totalAttempts"] == 2
    assert ledger["failedAttempts"] == 1
    record = json.loads(next((release_root / "tasks").glob("*.json")).read_text())
    assert record["retries"][0]["modelUsages"] == [failed_usage]
    assert {row["kind"] for row in record["artifacts"]} >= {
        "failed_attempt_trajectory", "failed_attempt_proxy_metrics",
    }
    assert record["cost"]["modelCostUsd"] > 0.0001


def test_harbor_audit_reconciles_the_same_raw_proxy_and_atif_evidence(tmp_path):
    run = _harbor_run(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    trial_dirs = [
        path for path in (run / "jobs" / "harbor-job").iterdir()
        if path.is_dir()
    ]

    checks = _codex_kimi_trial_evidence_checks(manifest, trial_dirs, 1)

    assert len(checks) == 1
    assert checks[0].name == "codex_kimi_reconciled_trial_evidence"
    assert checks[0].ok is True


def test_trial_index_mapping_is_lexical_and_outcome_independent(tmp_path):
    trials = [tmp_path / name for name in ("task__z", "task__a", "task__m")]
    release = [
        {"taskId": f"trial-{index}", "trialIndex": index}
        for index in (3, 1, 2)
    ]

    pairs = _ordered_trial_pairs("task", trials, release)

    assert [(path.name, row["trialIndex"]) for path, row in pairs] == [
        ("task__a", 1), ("task__m", 2), ("task__z", 3),
    ]
    with pytest.raises(HarborCodexExportError, match="attempt shape"):
        _ordered_trial_pairs("task", trials, release[:-1])
