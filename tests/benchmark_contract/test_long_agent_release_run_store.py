"""Executable contracts for immutable paired long-agent run evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluations.long_agent_release import run_store
from evaluations.long_agent_release.cli import main
from evaluations.long_agent_release.run_store import (
    ReleaseRunError,
    audit_release_attempts,
    audit_release_pair,
    audit_release_run,
    claim_release_task_attempts,
    fail_release_task_attempt,
    finalize_release_run,
    initialize_release_run,
    record_release_task,
    release_task_retry_evidence,
    store_run_artifact,
)
from evaluations.long_agent_release.report import analyze_release_pair
from lib.benchmark_contract import (
    RELEASE_TASK_MATRIX_V2,
    build_manifest_v2,
    build_task_record_v2,
    read_jsonl,
)


pytestmark = pytest.mark.unit


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest(role: str, *, thinking: str = "high") -> dict:
    baseline = role == "baseline"
    return build_manifest_v2(
        run_id=f"run-{role}",
        harness={
            "name": "paired-harness", "version": "1",
            "commitSha256": _sha("harness"),
        },
        agent={
            "name": "codex" if baseline else "tofu",
            "version": "0.149.1" if baseline else "candidate",
            "binarySha256" if baseline else "commitSha256": (
                _sha("codex") if baseline else _sha("tofu")),
        },
        provider_face="meituan-chat",
        provider_slot_id="kimi-slot-fixture",
        thinking=thinking,
        experiment_arm="codex_0_149_1" if baseline else "control",
        pair_id="pair-1",
        comparison_role=role,
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=_sha(f"prompt-{role}"),
        tool_schema_digest=_sha(f"tools-{role}"),
        dataset_snapshot={
            "id": "pilot", "sha256": _sha("dataset"), "frozen": True,
        },
        task_table=[{
            "taskId": "pilot:task-1", "family": "pilot",
            "dataset": "pilot",
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


def _store_artifacts(
    tmp_path: Path,
    run_root: Path,
    *,
    baseline: bool,
    task_id: str = "pilot:task-1",
) -> list[dict]:
    raw = tmp_path / f"raw-{run_root.name}.jsonl"
    raw.write_text('{"event":"model_round"}\n', encoding="utf-8")
    artifacts = [store_run_artifact(
        run_root, task_id=task_id,
        kind="raw_trajectory", source=raw)]
    if baseline:
        metrics = tmp_path / f"metrics-{run_root.name}.jsonl"
        metrics.write_text(json.dumps({
            "event": "responsesTranslation",
            "trialToken": _sha("trial-token"),
            "status": "completed",
            "upstreamCalls": 1,
            "invalidTrial": False,
            "clientDisconnected": False,
            "translationCpuNs": 10_000_000,
            "proxyCpuNs": 20_000_000,
            "rawWallNs": 80_000_000,
        }) + "\n", encoding="utf-8")
        artifacts.append(store_run_artifact(
            run_root, task_id=task_id,
            kind="proxy_metrics", source=metrics))
    return artifacts


def _task(
    manifest: dict, artifacts: list[dict], *,
    task_id: str = "pilot:task-1", dataset: str = "pilot",
    family: str = "pilot",
) -> dict:
    baseline = manifest["comparisonRole"] == "baseline"
    model_cost = 10 * 2.76 / 1_000_000
    return build_task_record_v2(
        run_id=manifest["runId"], dataset=dataset, family=family,
        task_id=task_id, agent=manifest["agent"],
        provider_face=manifest["providerFace"],
        provider_slot_id=manifest["providerSlotId"],
        thinking=manifest["thinking"],
        experiment_arm=manifest["experimentArm"],
        oracle={"passed": True, "type": "exact"},
        rounds=[{"round": 1, "usage": {"prompt_tokens": 10}}],
        context_blocks=[], tool_schemas=[], tool_results=[], compactions=[],
        call_graph=[], retries=[], cost={
            "modelCostUsd": model_cost,
            "compactionCostUsd": 0,
            "paidToolCostUsd": 0,
            "agentCostUsd": model_cost,
        },
        latency={
            "rawWallMs": 100, "oracleReadyMs": 100,
            "queueMs": 5, "ttftMs": 10, "modelMs": 80, "toolMs": 5,
            "translationCpuMs": 10 if baseline else 0,
            "proxyCpuMs": 20 if baseline else 0,
            "codexFavoredCorrectedWallMs": 90 if baseline else 100,
        },
        final_output_digest=_sha("final"), artifacts=artifacts,
    )


def _complete_run(tmp_path: Path, role: str) -> tuple[Path, dict, dict]:
    manifest = _manifest(role)
    root = tmp_path / role
    initialize_release_run(root, manifest)
    artifacts = _store_artifacts(
        tmp_path, root, baseline=role == "baseline")
    task = _task(manifest, artifacts)
    record_release_task(root, task)
    return root, manifest, task


def test_run_store_is_resumable_immutable_and_finalizes_in_manifest_order(
    tmp_path,
):
    manifest = _manifest("candidate")
    root = tmp_path / "candidate"
    created = initialize_release_run(root, manifest)
    resumed = initialize_release_run(root, manifest)

    assert created["status"] == "created"
    assert resumed["status"] == "unchanged"
    assert audit_release_run(root)["status"] == "incomplete"

    task = _task(manifest, _store_artifacts(tmp_path, root, baseline=False))
    assert record_release_task(root, task)["status"] == "created"
    assert record_release_task(root, task)["status"] == "unchanged"
    assert audit_release_run(root, require_complete=True)["complete"] is True

    finalized = finalize_release_run(root)
    assert finalized["status"] == "created"
    assert finalize_release_run(root)["status"] == "unchanged"
    records = read_jsonl(root / "run.jsonl")
    assert [row["recordType"] for row in records] == ["manifest", "task"]
    assert records[1]["taskId"] == manifest["taskIds"][0]


def test_attempt_ledger_preclaims_retries_and_binds_oracle_ready_record(
    tmp_path,
):
    manifest = _manifest("candidate")
    root = tmp_path / "attempt-ledger"
    initialize_release_run(root, manifest)

    first = claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-1",
        runner_kind="tofu-runner",
    )
    assert first["claims"][0]["attemptIndex"] == 1
    with pytest.raises(ReleaseRunError, match="another execution"):
        claim_release_task_attempts(
            root, task_ids=["pilot:task-1"], execution_id="execution-other",
            runner_kind="tofu-runner",
        )
    fail_release_task_attempt(
        root, task_id="pilot:task-1", execution_id="execution-1",
        code="qemu_start", model_usages=[], paid_tool_cost_usd=0,
        artifacts=[], no_paid_calls=True,
    )
    second = claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-2",
        runner_kind="tofu-runner",
    )

    task = _task(
        manifest, _store_artifacts(tmp_path, root, baseline=False))
    task["retries"] = release_task_retry_evidence(
        root, task_id="pilot:task-1")
    first_started = first["claims"][0]["occurredAt"]
    second_started = second["claims"][0]["occurredAt"]
    task["completedAt"] = max(first_started, second_started) + 100
    raw_wall = task["completedAt"] - first_started
    task["latency"]["rawWallMs"] = raw_wall
    task["latency"]["oracleReadyMs"] = raw_wall
    task["latency"]["codexFavoredCorrectedWallMs"] = raw_wall
    task["environment"] = {
        "releaseAttempt": {"taskStartedAtUnixMs": second_started},
    }
    result = record_release_task(root, task)
    assert result["attemptStatus"] == "created"
    audit = audit_release_attempts(root, require_complete=True)
    assert audit["valid"] is True
    assert audit["complete"] is True
    assert audit["totalAttempts"] == 2
    assert audit["failedAttempts"] == 1
    assert audit["openAttempts"] == 0

    finalize_release_run(root)
    rows = read_jsonl(root / "run.jsonl")
    assert [row["recordType"] for row in rows] == [
        "manifest", "attempt", "attempt", "attempt", "attempt", "task",
    ]
    with pytest.raises(ReleaseRunError, match="finalized"):
        claim_release_task_attempts(
            root, task_ids=["pilot:task-1"], execution_id="execution-3",
            runner_kind="tofu-runner",
        )


def test_attempt_failure_requires_post_dispatch_evidence(tmp_path):
    manifest = _manifest("candidate")
    root = tmp_path / "missing-failure-evidence"
    initialize_release_run(root, manifest)
    claim = claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-1",
        runner_kind="tofu-runner",
    )
    with pytest.raises(ReleaseRunError, match="retained evidence"):
        fail_release_task_attempt(
            root, task_id="pilot:task-1", execution_id="execution-1",
            code="stream_truncated", model_usages=[], paid_tool_cost_usd=0,
            artifacts=[], no_paid_calls=False,
            task_started_at_unix_ms=claim["claims"][0]["occurredAt"],
        )


def test_candidate_failed_attempt_usage_requires_runtime_evidence(tmp_path):
    manifest = _manifest("candidate")
    root = tmp_path / "candidate-failure-evidence"
    initialize_release_run(root, manifest)
    claim = claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-1",
        runner_kind="tofu-runner",
    )
    trajectory = tmp_path / "failed-tofu-events.jsonl"
    trajectory.write_text(
        '{"type":"round_usage","roundNum":1}\n', encoding="utf-8")
    trajectory_artifact = store_run_artifact(
        root, task_id="pilot:task-1",
        kind="failed_attempt_trajectory", source=trajectory,
    )
    usage = {
        "prompt_tokens": 21,
        "completion_tokens": 4,
        "cache_read_tokens": 7,
    }
    compaction_usage = {
        "n_calls": 1,
        "prompt_tokens": 9,
        "completion_tokens": 2,
        "cache_read_tokens": 0,
    }
    with pytest.raises(
            ReleaseRunError, match="failed_attempt_runtime_evidence"):
        fail_release_task_attempt(
            root, task_id="pilot:task-1", execution_id="execution-1",
            code="stream_truncated",
            model_usages=[usage, compaction_usage],
            paid_tool_cost_usd=0, artifacts=[trajectory_artifact],
            no_paid_calls=False,
            task_started_at_unix_ms=claim["claims"][0]["occurredAt"],
        )

    runtime_evidence = tmp_path / "failed-tofu-runtime.json"
    runtime_evidence.write_text(json.dumps({
        "contractVersion": "tofu.agent-runtime-evidence/v1",
        "model": "kimi-k3",
        "status": "error",
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 6,
            "cache_read_tokens": 7,
        },
        "apiRounds": [{"round": 1, "usage": usage}],
        "compactionUsage": compaction_usage,
    }), encoding="utf-8")
    runtime_artifact = store_run_artifact(
        root, task_id="pilot:task-1",
        kind="failed_attempt_runtime_evidence", source=runtime_evidence,
    )
    artifacts = [trajectory_artifact, runtime_artifact]
    with pytest.raises(ReleaseRunError, match="differ from runtime evidence"):
        fail_release_task_attempt(
            root, task_id="pilot:task-1", execution_id="execution-1",
            code="stream_truncated",
            model_usages=[
                {**usage, "prompt_tokens": 22}, compaction_usage],
            paid_tool_cost_usd=0, artifacts=artifacts,
            no_paid_calls=False,
            task_started_at_unix_ms=claim["claims"][0]["occurredAt"],
        )

    result = fail_release_task_attempt(
        root, task_id="pilot:task-1", execution_id="execution-1",
        code="stream_truncated",
        model_usages=[usage, compaction_usage],
        paid_tool_cost_usd=0, artifacts=artifacts, no_paid_calls=False,
        task_started_at_unix_ms=claim["claims"][0]["occurredAt"],
    )
    assert result["outcome"] == "infrastructure_failed"
    audit = audit_release_attempts(root)
    assert audit["valid"] is True
    assert audit["failedAttempts"] == 1
    assert audit["openAttempts"] == 0


def test_attempt_ledger_rejects_latency_that_drops_retry_wall(
    tmp_path, monkeypatch,
):
    manifest = _manifest("candidate")
    root = tmp_path / "retry-latency"
    initialize_release_run(root, manifest)
    timestamps = iter((1.0, 1.1, 2.0))
    real_time = run_store.time.time
    monkeypatch.setattr(run_store.time, "time", lambda: next(timestamps))
    claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-1",
        runner_kind="tofu-runner",
    )
    fail_release_task_attempt(
        root, task_id="pilot:task-1", execution_id="execution-1",
        code="qemu_start", model_usages=[], paid_tool_cost_usd=0,
        artifacts=[], no_paid_calls=True,
    )
    claim_release_task_attempts(
        root, task_ids=["pilot:task-1"], execution_id="execution-2",
        runner_kind="tofu-runner",
    )
    monkeypatch.setattr(run_store.time, "time", real_time)
    task = _task(
        manifest, _store_artifacts(tmp_path, root, baseline=False))
    task["retries"] = release_task_retry_evidence(
        root, task_id="pilot:task-1")
    task["completedAt"] = 2_100
    task["latency"]["rawWallMs"] = 100
    task["latency"]["oracleReadyMs"] = 100
    task["latency"]["codexFavoredCorrectedWallMs"] = 100
    task["environment"] = {
        "releaseAttempt": {"taskStartedAtUnixMs": 2_000},
    }

    with pytest.raises(ReleaseRunError, match="omits attempt or retry"):
        record_release_task(root, task)


def test_full_frozen_matrix_rejects_unclaimed_task_record(tmp_path):
    task_table = [
        {
            "taskId": f"{family}:{dataset}:{index}",
            "family": family,
            "dataset": dataset,
        }
        for (family, dataset), count in RELEASE_TASK_MATRIX_V2.items()
        for index in range(count)
    ]
    pilot = _manifest("candidate")
    manifest = build_manifest_v2(
        run_id="full-candidate", harness=pilot["harness"],
        agent=pilot["agent"], provider_face=pilot["providerFace"],
        provider_slot_id=pilot["providerSlotId"], thinking=pilot["thinking"],
        experiment_arm=pilot["experimentArm"], pair_id="full-pair",
        comparison_role="candidate",
        tool_permissions=pilot["toolPermissions"],
        prompt_digest=pilot["promptDigest"],
        tool_schema_digest=pilot["toolSchemaDigest"],
        dataset_snapshot={
            "id": "full-fixture", "sha256": _sha("full-fixture"),
            "frozen": True, "releaseMatrix": True,
        },
        task_table=task_table, sandbox=pilot["sandbox"],
        retry_rule=pilot["retryRule"], artifact_limits=pilot["artifactLimits"],
        timeout_seconds=pilot["limits"]["timeoutSeconds"],
        maximum_infrastructure_failure_rate=pilot["limits"][
            "maximumInfrastructureFailureRate"],
    )
    root = tmp_path / "full-candidate"
    initialize_release_run(root, manifest)
    first = task_table[0]
    artifacts = _store_artifacts(
        tmp_path, root, baseline=False, task_id=first["taskId"])
    record = _task(
        manifest, artifacts, task_id=first["taskId"],
        dataset=first["dataset"], family=first["family"],
    )

    with pytest.raises(ReleaseRunError, match="pre-dispatch"):
        record_release_task(root, record)
    audit = audit_release_run(root, require_complete=True)
    assert audit["complete"] is False
    assert audit["attemptLedger"]["required"] is True
    assert audit["attemptLedger"]["missingClaimTasks"] == 1845


def test_run_store_rejects_cross_arm_record_and_mutated_artifact(tmp_path):
    root, manifest, task = _complete_run(tmp_path, "candidate")
    with pytest.raises(ReleaseRunError, match="experimentArm"):
        record_release_task(root, {**task, "experimentArm": "tool_surface_v2"})

    artifact = root / task["artifacts"][0]["path"]
    artifact.chmod(0o600)
    artifact.write_text("changed", encoding="utf-8")
    audit = audit_release_run(root, require_complete=True)
    assert audit["valid"] is False
    assert "changed size" in audit["errors"][0] \
        or "digest mismatch" in audit["errors"][0]

    with pytest.raises(ReleaseRunError, match="run is not complete"):
        finalize_release_run(root)
    assert manifest["comparisonRole"] == "candidate"


def test_run_store_requires_raw_trajectory_and_refuses_record_replacement(
    tmp_path,
):
    manifest = _manifest("candidate")
    root = tmp_path / "candidate"
    initialize_release_run(root, manifest)
    with pytest.raises(ReleaseRunError, match="raw_trajectory"):
        record_release_task(root, _task(manifest, []))

    task = _task(manifest, _store_artifacts(tmp_path, root, baseline=False))
    record_release_task(root, task)
    changed = {
        **task,
        "oracle": {"passed": False, "type": "exact"},
        "finalOutputDigest": _sha("different-final"),
    }
    with pytest.raises(ReleaseRunError, match="refusing to replace"):
        record_release_task(root, changed)


def test_pair_audit_requires_complete_runs_and_identical_fairness_controls(
    tmp_path,
):
    baseline, _, _ = _complete_run(tmp_path, "baseline")
    candidate, _, _ = _complete_run(tmp_path, "candidate")

    pair = audit_release_pair(
        baseline_root=baseline, candidate_root=candidate,
        require_complete=True)
    assert pair["pairReady"] is True
    assert pair["completedPairs"] == 1
    assert pair["claim"] == "paired evidence ready for gate analysis"

    drift_manifest = _manifest("candidate", thinking="medium")
    drift = tmp_path / "candidate-drift"
    initialize_release_run(drift, drift_manifest)
    drift_pair = audit_release_pair(
        baseline_root=baseline, candidate_root=drift)
    assert drift_pair["valid"] is False
    assert "fairness controls differ" in drift_pair["errors"][0]


def test_baseline_proxy_metrics_must_match_request_count_and_latency(tmp_path):
    manifest = _manifest("baseline")
    root = tmp_path / "baseline"
    initialize_release_run(root, manifest)
    artifacts = _store_artifacts(tmp_path, root, baseline=True)
    task = _task(manifest, artifacts)
    task["latency"]["proxyCpuMs"] = 18

    with pytest.raises(ReleaseRunError, match="does not match proxy metrics"):
        record_release_task(root, task)


def test_run_store_enforces_preregistered_artifact_and_candidate_latency_limits(
    tmp_path,
):
    manifest = _manifest("candidate")
    manifest["artifactLimits"] = {
        "maximumArtifactBytes": 4,
        "maximumTaskArtifactBytes": 8,
        "maximumRunArtifactBytes": 16,
    }
    root = tmp_path / "bounded"
    initialize_release_run(root, manifest)
    source = tmp_path / "too-large.jsonl"
    source.write_text("12345", encoding="utf-8")
    with pytest.raises(ReleaseRunError, match="byte limit"):
        store_run_artifact(
            root, task_id="pilot:task-1",
            kind="raw_trajectory", source=source)

    normal_manifest = _manifest("candidate")
    normal_root = tmp_path / "candidate-latency"
    initialize_release_run(normal_root, normal_manifest)
    task = _task(
        normal_manifest,
        _store_artifacts(tmp_path, normal_root, baseline=False),
    )
    task["latency"]["translationCpuMs"] = 1
    task["latency"]["proxyCpuMs"] = 1
    task["latency"]["codexFavoredCorrectedWallMs"] = 99
    with pytest.raises(ReleaseRunError, match="cannot subtract"):
        record_release_task(normal_root, task)


def test_run_store_reprices_usage_and_requires_real_jsonl_trajectory(tmp_path):
    manifest = _manifest("candidate")
    root = tmp_path / "candidate-cost"
    initialize_release_run(root, manifest)
    artifacts = _store_artifacts(tmp_path, root, baseline=False)
    task = _task(manifest, artifacts)
    task["cost"]["agentCostUsd"] = 0
    with pytest.raises(ReleaseRunError, match="priced evidence"):
        record_release_task(root, task)

    malformed_root = tmp_path / "candidate-malformed"
    initialize_release_run(malformed_root, manifest)
    malformed = tmp_path / "malformed.txt"
    malformed.write_text("not-json\n", encoding="utf-8")
    artifact = store_run_artifact(
        malformed_root, task_id="pilot:task-1",
        kind="raw_trajectory", source=malformed)
    with pytest.raises(ReleaseRunError, match="not valid UTF-8 JSONL"):
        record_release_task(
            malformed_root, _task(manifest, [artifact]))


def test_run_store_cli_reports_incomplete_as_nonzero_when_required(
    tmp_path, capsys
):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest("candidate")), encoding="utf-8")
    root = tmp_path / "cli-run"

    assert main([
        "run-init", "--manifest", str(manifest_path),
        "--run-root", str(root),
    ]) == 0
    capsys.readouterr()
    assert main([
        "run-status", "--run-root", str(root), "--require-complete",
    ]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "invalid"
    assert report["missingTasks"] == 1


def test_pair_report_is_immutable_and_pilot_can_never_make_release_claim(
    tmp_path, capsys,
):
    baseline, _, _ = _complete_run(tmp_path, "baseline")
    candidate, _, _ = _complete_run(tmp_path, "candidate")
    finalize_release_run(baseline)
    finalize_release_run(candidate)

    report = analyze_release_pair(
        baseline_root=baseline, candidate_root=candidate,
    )

    assert report["fullFrozenMatrix"] is False
    assert report["taskCount"] == 1
    assert report["releaseDecision"]["releaseEligible"] is False
    assert report["releaseDecision"]["gates"] == {"fullFrozenMatrix": False}
    assert "full frozen 1,845-task matrix" in report["claim"]
    assert report["baseline"]["latency"]["formalCodexFavoredP90Ms"] == 90
    assert report["candidate"]["latency"]["formalCodexFavoredP90Ms"] == 100
    assert report["infrastructure"]["evidenceSource"] \
        == "task_retry_rows_diagnostic_only"
    assert report["infrastructure"]["completeImmutableAttemptLedger"] is False

    output = tmp_path / "pair-report.json"
    for expected_status in ("created", "unchanged"):
        assert main([
            "pair-report",
            "--baseline-root", str(baseline),
            "--candidate-root", str(candidate),
            "--output", str(output),
        ]) == 0
        cli_report = json.loads(capsys.readouterr().out)
        assert cli_report["status"] == expected_status
        assert cli_report["releaseEligible"] is False
