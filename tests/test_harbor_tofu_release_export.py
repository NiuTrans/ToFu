"""Harbor candidate lifecycle evidence must enter the immutable run store."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evaluations.long_agent_release import harbor_tofu_export
from evaluations.long_agent_release.harbor_tofu_export import (
    HarborTofuExportError,
    export_tofu_harbor_run,
)
from evaluations.long_agent_release.harbor_tracking import (
    claim_tofu_harbor_release_attempts,
)
from evaluations.long_agent_release.run_store import (
    audit_release_attempts,
    audit_release_run,
    claim_release_task_attempts,
    fail_release_task_attempt,
    initialize_release_run,
    store_run_artifact,
)
from evaluations.swebench.constants import BENCHMARKS, HARBOR_COMMIT
from evaluations.swebench.audit import _tofu_kimi_trial_evidence_checks
from evaluations.swebench.tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TofuKimiCandidateSettings,
    tofu_kimi_clean_tool_schemas,
    tofu_kimi_prompt_contract_sha256,
    tofu_kimi_tool_schema_sha256,
)
from lib.benchmark_contract import build_manifest_v2
from tests.test_tofu_long_agent_projection import _evidence, _runtime_config
from tofu_agent import __version__ as TOFU_AGENT_VERSION


pytestmark = pytest.mark.unit
_SOURCE_TASK = "swe-bench/django__django-11099"
_SOURCE_REF = "sha256:" + "f" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _harness() -> dict:
    return {
        "name": "harbor",
        "version": "harbor 0.21.0",
        "binarySha256": _sha("harbor-binary"),
        "sourceCommit": HARBOR_COMMIT,
        "runnerFrameworkVersion": "1.3.0",
        "runnerProjectRevision": "candidate-revision",
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


def _settings() -> TofuKimiCandidateSettings:
    return TofuKimiCandidateSettings(
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-a",
        agent_version=TOFU_AGENT_VERSION,
        experiment_arm="prompt_lean_kimi",
        runtime_config=_runtime_config(),
    )


def _release_manifest(settings: TofuKimiCandidateSettings) -> dict:
    return build_manifest_v2(
        run_id="release-candidate",
        harness=_harness(),
        agent={
            "name": "tofu", "version": TOFU_AGENT_VERSION,
            "commitSha256": _sha("tofu-agent"),
        },
        provider_face=settings.provider_face,
        provider_slot_id=settings.provider_slot_id,
        thinking="high",
        experiment_arm=settings.experiment_arm,
        pair_id="pair-a",
        comparison_role="candidate",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=tofu_kimi_prompt_contract_sha256(
            settings.runtime_config),
        tool_schema_digest=tofu_kimi_tool_schema_sha256(),
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
            "maxInfrastructureRetries": 1,
            "retryableFailureClasses": ["infrastructure"],
        },
        artifact_limits={
            "maximumArtifactBytes": 1_000_000,
            "maximumTaskArtifactBytes": 4_000_000,
            "maximumRunArtifactBytes": 10_000_000,
        },
        timeout_seconds=3600,
        maximum_infrastructure_failure_rate=0.02,
        environment={
            "gitCommit": "candidate-revision",
            "runtimeConfigSha256": settings.runtime_config_sha256,
        },
    )


def _harbor_run(
    tmp_path: Path, settings: TofuKimiCandidateSettings,
) -> Path:
    run = tmp_path / "harbor-run"
    run.mkdir(mode=0o700, parents=True)
    definition = BENCHMARKS["swebench-verified"]
    job_config = {
        "n_attempts": 1,
        "timeout_multiplier": 1.0,
        "retry": {"max_retries": 0},
        "datasets": [{
            "name": definition.dataset.rsplit("@", 1)[0],
            "ref": definition.dataset.rsplit("@", 1)[1],
            "task_names": [_SOURCE_TASK],
        }],
    }
    _write_json(run / "job-config.json", job_config)
    manifest = {
        "kind": "harbor-agent-evaluation",
        "status": "succeeded",
        "run_id": "harbor-tofu-job",
        "benchmark": "swebench-verified",
        "expected_trials": 1,
        "agent": TOFU_KIMI_AGENT,
        "models": ["kimi-k3"],
        "reasoning_effort": "high",
        "agent_version": TOFU_AGENT_VERSION,
        "experiment_arm": settings.experiment_arm,
        "provider_face": settings.provider_face,
        "provider_slot_id": settings.provider_slot_id,
        "backend": "rootless-qemu",
        "job_config": str((run / "job-config.json").resolve()),
        "harness_identity": _harness(),
        "sandbox_identity": _sandbox(),
        "project_revision": "candidate-revision",
        "tofu_kimi_runtime": settings.manifest_record(),
    }
    _write_json(run / "manifest.json", manifest)
    job = run / "jobs" / "harbor-tofu-job"
    trial_name = "django__django-11099__abc1234"
    trial = job / trial_name
    task = {"name": _SOURCE_TASK, "ref": _SOURCE_REF}
    trial_config = {"task": task}
    _write_json(trial / "config.json", trial_config)
    _write_json(job / "result.json", {
        "id": "11111111-1111-1111-1111-111111111111",
        "n_total_trials": 1,
        "stats": {"n_retries": 0},
    })
    started = datetime(2099, 1, 1, tzinfo=timezone.utc)
    _write_json(trial / "result.json", {
        "id": "22222222-2222-2222-2222-222222222222",
        "task_name": "django__django-11099",
        "trial_name": trial_name,
        "task_checksum": _sha("task-checksum"),
        "config": trial_config,
        "agent_info": {
            "name": "tofu-kimi-runtime",
            "version": TOFU_AGENT_VERSION,
            "model_info": {"name": "kimi-k3"},
        },
        "verifier_result": {"rewards": {"reward": 1}},
        "exception_info": None,
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(milliseconds=700)).isoformat(),
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
            "finished_at": (started + timedelta(milliseconds=500)).isoformat(),
        },
        "verifier": {
            "started_at": (started + timedelta(milliseconds=500)).isoformat(),
            "finished_at": (started + timedelta(milliseconds=600)).isoformat(),
        },
    })
    base_ns = int(started.timestamp() * 1_000_000_000)
    (tmp_path / "source-evidence").mkdir(parents=True)
    source_native, source_runtime, source_audit = _evidence(
        tmp_path / "source-evidence", task_start_ns=base_ns)
    evidence = trial / "agent" / "tofu-kimi-evidence"
    evidence.mkdir(parents=True)
    shutil.copyfile(source_native, evidence / "events.jsonl")
    shutil.copyfile(source_runtime, evidence / "runtime-evidence.json")
    shutil.copyfile(source_audit, evidence / "tool-audit.json")
    runtime = settings.manifest_record()
    _write_json(trial / "agent" / "trajectory.json", {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "tofu-kimi-runtime",
            "version": TOFU_AGENT_VERSION,
            "model_name": "kimi-k3",
            "tool_definitions": tofu_kimi_clean_tool_schemas(),
            "extra": {
                "credential_boundary": "harbor-host-only",
                "harness_profile": "tofu-kimi",
                "experiment_arm": settings.experiment_arm,
                "runtime_config_sha256": settings.runtime_config_sha256,
                "prompt_contract_sha256": runtime["promptContractSha256"],
                "tool_schema_sha256": runtime["toolSchemaSha256"],
            },
        },
        "steps": [
            {"step_id": 1, "source": "user", "message": "solve"},
            {"step_id": 2, "source": "agent", "message": "done"},
        ],
    })
    return run


def _bind_tracking(release_root: Path, harbor_run: Path) -> None:
    manifest_path = harbor_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    config = json.loads((harbor_run / "job-config.json").read_text())
    manifest["release_attempt_tracking"] = (
        claim_tofu_harbor_release_attempts(
            release_run_root=release_root,
            harbor_manifest=manifest,
            job_config=config,
        )
    )
    manifest["release_evidence_eligible"] = True
    _write_json(manifest_path, manifest)


def test_export_commits_candidate_with_raw_oracle_ready_latency(
    tmp_path, monkeypatch,
):
    settings = _settings()
    release_root = tmp_path / "release"
    initialize_release_run(release_root, _release_manifest(settings))
    harbor_run = _harbor_run(tmp_path, settings)
    _bind_tracking(release_root, harbor_run)
    monkeypatch.setattr(
        harbor_tofu_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )

    report = export_tofu_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root)
    repeated = export_tofu_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root)

    assert report["records"] == {"created": 1}
    assert repeated["records"] == {"unchanged": 1}
    assert audit_release_run(release_root, require_complete=True)["complete"] \
        is True
    task_path = next((release_root / "tasks").glob("*.json"))
    record = json.loads(task_path.read_text())
    assert record["latency"]["rawWallMs"] == 600
    assert record["latency"]["codexFavoredCorrectedWallMs"] == 600
    assert record["latency"]["translationCpuMs"] == 0
    assert record["environment"]["harbor"]["latencyScope"] \
        == "trial.started_at_to_verifier.finished_at"
    assert {row["kind"] for row in record["artifacts"]} == {
        "raw_trajectory", "runtime_evidence", "tool_audit", "agent_trajectory",
    }


def test_export_rejects_unclaimed_or_prompt_drifted_candidate(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        harbor_tofu_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )
    settings = _settings()
    unclaimed_root = tmp_path / "unclaimed-release"
    initialize_release_run(unclaimed_root, _release_manifest(settings))
    with pytest.raises(HarborTofuExportError, match="not preclaimed"):
        export_tofu_harbor_run(
            harbor_run_dir=_harbor_run(tmp_path / "unclaimed", settings),
            release_run_root=unclaimed_root,
        )

    drift_manifest = _release_manifest(settings)
    drift_manifest["promptDigest"] = _sha("different-prompt")
    drift_root = tmp_path / "drift-release"
    initialize_release_run(drift_root, drift_manifest)
    drift_run = _harbor_run(tmp_path / "drift", settings)
    with pytest.raises(HarborTofuExportError, match="controls drifted"):
        export_tofu_harbor_run(
            harbor_run_dir=drift_run, release_run_root=drift_root)


def test_current_audit_reconciles_candidate_and_detects_secret_value(
    tmp_path, monkeypatch,
):
    settings = _settings()
    harbor_run = _harbor_run(tmp_path, settings)
    manifest = json.loads((harbor_run / "manifest.json").read_text())
    trial = next((harbor_run / "jobs" / "harbor-tofu-job").glob("*__*"))
    monkeypatch.setenv(settings.upstream_api_key_env, "audit-secret-value")

    checks = {
        row.name: row for row in _tofu_kimi_trial_evidence_checks(
            manifest, [trial], 1)
    }
    assert checks["tofu_kimi_reconciled_trial_evidence"].ok is True
    assert checks["tofu_kimi_no_trial_credential_persistence"].ok is True

    audit_path = trial / "agent" / "tofu-kimi-evidence" / "tool-audit.json"
    audit = json.loads(audit_path.read_text())
    audit["diagnosticLeak"] = "audit-secret-value"
    _write_json(audit_path, audit)
    checks = {
        row.name: row for row in _tofu_kimi_trial_evidence_checks(
            manifest, [trial], 1)
    }
    assert checks["tofu_kimi_reconciled_trial_evidence"].ok is True
    assert checks["tofu_kimi_no_trial_credential_persistence"].ok is False


def test_export_accounts_for_failed_tofu_attempt_usage_and_artifacts(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        harbor_tofu_export, "audit_run",
        lambda _run: {"ok": True, "checks": []},
    )
    settings = _settings()
    release_root = tmp_path / "retry-release"
    manifest = _release_manifest(settings)
    initialize_release_run(release_root, manifest)
    task_id = "swe-bench-verified:django__django-11099"
    failed_claim = claim_release_task_attempts(
        release_root, task_ids=[task_id], execution_id="failed-tofu-job",
        runner_kind="harbor-tofu",
    )
    failed_raw = tmp_path / "failed-tofu-events.jsonl"
    failed_raw.write_text(
        '{"type":"round_usage","roundNum":1}\n', encoding="utf-8")
    failed_usage = {
        "prompt_tokens": 20,
        "completion_tokens": 3,
        "cache_read_tokens": 4,
    }
    failed_compaction = {
        "n_calls": 1,
        "prompt_tokens": 5,
        "completion_tokens": 1,
        "cache_read_tokens": 0,
    }
    failed_runtime = tmp_path / "failed-tofu-runtime.json"
    _write_json(failed_runtime, {
        "contractVersion": "tofu.agent-runtime-evidence/v1",
        "requestId": "failed-tofu-job",
        "taskId": "failed-runtime-task",
        "model": "kimi-k3",
        "status": "error",
        "finishReason": "timeout",
        "usage": {
            "prompt_tokens": 25,
            "completion_tokens": 4,
            "cache_read_tokens": 4,
        },
        "apiRounds": [{"round": 1, "usage": failed_usage}],
        "compactionUsage": failed_compaction,
    })
    failed_artifacts = [
        store_run_artifact(
            release_root, task_id=task_id,
            kind="failed_attempt_trajectory", source=failed_raw,
        ),
        store_run_artifact(
            release_root, task_id=task_id,
            kind="failed_attempt_runtime_evidence", source=failed_runtime,
        ),
    ]
    fail_release_task_attempt(
        release_root, task_id=task_id, execution_id="failed-tofu-job",
        code="stream_truncated",
        model_usages=[failed_usage, failed_compaction],
        paid_tool_cost_usd=0, artifacts=failed_artifacts,
        no_paid_calls=False,
        task_started_at_unix_ms=failed_claim["claims"][0]["occurredAt"],
    )
    harbor_run = _harbor_run(tmp_path / "successful-retry", settings)
    _bind_tracking(release_root, harbor_run)

    report = export_tofu_harbor_run(
        harbor_run_dir=harbor_run, release_run_root=release_root)

    assert report["records"] == {"created": 1}
    ledger = audit_release_attempts(release_root, require_complete=True)
    assert ledger["totalAttempts"] == 2
    assert ledger["failedAttempts"] == 1
    record = json.loads(next((release_root / "tasks").glob("*.json")).read_text())
    assert record["retries"][0]["modelUsages"] == [
        failed_usage, failed_compaction,
    ]
    assert {row["kind"] for row in record["artifacts"]} >= {
        "failed_attempt_trajectory", "failed_attempt_runtime_evidence",
    }
    assert record["latency"]["rawWallMs"] > 600
    assert record["cost"]["modelCostUsd"] > 0.0001
