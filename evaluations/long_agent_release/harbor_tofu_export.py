"""Project audited Harbor production-Tofu trials into release evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluations.swebench.audit import audit_run
from evaluations.swebench.constants import HARBOR_COMMIT
from evaluations.swebench.tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TofuKimiCandidateSettings,
)
from tofu_agent import __version__ as TOFU_AGENT_VERSION

from .harbor_tracking import (
    HarborReleaseTrackingError,
    validate_tofu_harbor_release_attempts,
)
from .run_store import (
    load_release_manifest,
    record_release_task,
    release_task_retry_evidence,
    store_run_artifact,
)
from .tofu_projection import (
    TofuProjectionError,
    build_tofu_release_task_record,
    project_tofu_trial,
)


HARBOR_TOFU_EXPORT_CONTRACT = "tofu-harbor-production-export/v1"
_BENCHMARK_DATASETS = {
    "swebench-verified": "swe-bench-verified",
    "terminal-bench-2.1": "terminal-bench-2.1",
}


class HarborTofuExportError(ValueError):
    """The Harbor run cannot serve as complete candidate evidence."""


@dataclass(frozen=True)
class _PreparedTrial:
    task_id: str
    trial_name: str
    native_events: Path
    runtime_evidence: Path
    tool_audit: Path
    agent_trajectory: Path
    projection: dict[str, Any]
    oracle: dict[str, Any]
    task_started_at_unix_ns: int
    completed_at_unix_ms: int
    environment: dict[str, Any]


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarborTofuExportError(
                f"JSON evidence contains duplicate key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise HarborTofuExportError(
        f"JSON evidence contains non-finite number {value}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HarborTofuExportError(
            f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarborTofuExportError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise HarborTofuExportError(f"{label} must contain a JSON object")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise HarborTofuExportError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarborTofuExportError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarborTofuExportError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _unix_ns(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _phase_timing(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarborTofuExportError(f"trial {label} timing is required")
    started = _timestamp(value.get("started_at"), f"{label}.started_at")
    finished = _timestamp(value.get("finished_at"), f"{label}.finished_at")
    duration_ms = (finished - started).total_seconds() * 1000
    if duration_ms < 0:
        raise HarborTofuExportError(f"trial {label} finishes before it starts")
    return {
        "startedAt": started.isoformat(),
        "finishedAt": finished.isoformat(),
        "durationMs": duration_ms,
    }


def _reward(result: dict[str, Any]) -> tuple[bool, float]:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    raw = rewards.get("reward") if isinstance(rewards, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HarborTofuExportError(
            "Harbor verifier must provide a numeric reward")
    score = float(raw)
    if not math.isfinite(score) or score not in {0.0, 1.0}:
        raise HarborTofuExportError(
            "release software-task reward must be exactly 0 or 1")
    return score == 1.0, score


def _formal_binding(
    *, harbor_manifest: dict[str, Any], release_manifest: dict[str, Any],
    job_config: dict[str, Any], job_result: dict[str, Any],
) -> tuple[str, TofuKimiCandidateSettings]:
    runtime = harbor_manifest.get("tofu_kimi_runtime")
    try:
        settings = TofuKimiCandidateSettings.from_manifest_record(runtime)
    except (TypeError, ValueError) as exc:
        raise HarborTofuExportError(
            "Harbor run is not a valid formal Tofu candidate") from exc
    release_agent = release_manifest.get("agent") or {}
    environment = release_manifest.get("environment") or {}
    if harbor_manifest.get("agent") != TOFU_KIMI_AGENT \
            or release_manifest.get("comparisonRole") != "candidate" \
            or not isinstance(release_agent, dict) \
            or release_agent.get("name") != "tofu" \
            or release_agent.get("version") != settings.agent_version \
            or settings.agent_version != TOFU_AGENT_VERSION:
        raise HarborTofuExportError(
            "release run is not bound to this production Tofu version")
    bindings = {
        "model": "kimi-k3",
        "providerFace": settings.provider_face,
        "providerSlotId": settings.provider_slot_id,
        "thinking": harbor_manifest.get("reasoning_effort"),
        "experimentArm": settings.experiment_arm,
        "promptDigest": runtime.get("promptContractSha256"),
        "toolSchemaDigest": runtime.get("toolSchemaSha256"),
    }
    drift = [
        field for field, expected in bindings.items()
        if release_manifest.get(field) != expected
    ]
    if drift:
        raise HarborTofuExportError(
            f"release/Harbor candidate controls drifted: {drift}")
    if not isinstance(environment, dict) \
            or environment.get("gitCommit") != harbor_manifest.get(
                "project_revision") \
            or environment.get("runtimeConfigSha256") \
            != settings.runtime_config_sha256:
        raise HarborTofuExportError(
            "release candidate code/runtime config drifted")
    if release_manifest.get("harness") != harbor_manifest.get(
            "harness_identity"):
        raise HarborTofuExportError(
            "release harness identity differs from executed Harbor")
    harness = harbor_manifest.get("harness_identity") or {}
    if harness.get("sourceCommit") != HARBOR_COMMIT \
            or harness.get("runnerProjectDirty") is not False:
        raise HarborTofuExportError(
            "formal release requires a clean pinned Harbor revision")
    if release_manifest.get("sandbox") != harbor_manifest.get(
            "sandbox_identity"):
        raise HarborTofuExportError(
            "release sandbox differs from executed rootless QEMU")
    if (release_manifest.get("limits") or {}).get("timeoutSeconds") \
            != settings.agent_timeout_seconds \
            or float(job_config.get("timeout_multiplier", 1.0)) != 1.0:
        raise HarborTofuExportError("release/Harbor timeout controls drifted")
    retry = job_config.get("retry") or {}
    if retry.get("max_retries", 0) != 0:
        raise HarborTofuExportError(
            "formal release forbids Harbor's evidence-erasing retries")
    observed_retries = (job_result.get("stats") or {}).get("n_retries", 0)
    if isinstance(observed_retries, bool) \
            or not isinstance(observed_retries, int) \
            or observed_retries != 0:
        raise HarborTofuExportError(
            "Harbor observed an evidence-erasing internal retry")
    benchmark = str(harbor_manifest.get("benchmark") or "")
    try:
        dataset = _BENCHMARK_DATASETS[benchmark]
    except KeyError as exc:
        raise HarborTofuExportError(
            f"unsupported formal Harbor benchmark: {benchmark!r}") from exc
    return dataset, settings


def _trial_task_identity(config: dict[str, Any]) -> tuple[str, str]:
    task = config.get("task")
    if not isinstance(task, dict):
        raise HarborTofuExportError("Harbor trial task config is missing")
    name = str(task.get("name") or "")
    ref = str(task.get("ref") or "")
    if not name or not ref:
        raise HarborTofuExportError(
            "Harbor trial task name and immutable ref are required")
    return name, ref


def _ordered_trial_pairs(
    source: str, trial_dirs: list[Path], release_rows: list[dict[str, Any]],
) -> list[tuple[Path, dict[str, Any]]]:
    trials = sorted(trial_dirs, key=lambda path: path.name)
    rows = sorted(release_rows, key=lambda row: int(row.get("trialIndex") or 0))
    indices = [row.get("trialIndex") for row in rows]
    if len(trials) != len(rows) or indices != list(range(1, len(rows) + 1)):
        raise HarborTofuExportError(
            f"Harbor/release trial shape drifted for source task {source}")
    return list(zip(trials, rows))


def _validate_atif(
    path: Path, *, settings: TofuKimiCandidateSettings,
    projection: dict[str, Any],
) -> None:
    atif = _load_json(path, "Tofu ATIF trajectory")
    agent = atif.get("agent")
    extra = agent.get("extra") if isinstance(agent, dict) else None
    if atif.get("schema_version") != "ATIF-v1.7" \
            or not isinstance(agent, dict) \
            or agent.get("name") != "tofu-kimi-runtime" \
            or agent.get("version") != settings.agent_version \
            or agent.get("model_name") != "kimi-k3" \
            or not isinstance(extra, dict):
        raise HarborTofuExportError("Tofu ATIF identity drifted")
    runtime = settings.manifest_record()
    expected_extra = {
        "credential_boundary": "harbor-host-only",
        "harness_profile": "tofu-kimi",
        "experiment_arm": settings.experiment_arm,
        "runtime_config_sha256": settings.runtime_config_sha256,
        "prompt_contract_sha256": runtime["promptContractSha256"],
        "tool_schema_sha256": runtime["toolSchemaSha256"],
    }
    if any(extra.get(key) != value for key, value in expected_extra.items()):
        raise HarborTofuExportError("Tofu ATIF runtime binding drifted")
    definitions = agent.get("tool_definitions")
    if not isinstance(definitions, list) \
            or runtime["toolSchemaSha256"] != hashlib.sha256(json.dumps(
                definitions, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest():
        raise HarborTofuExportError("Tofu ATIF tool definitions drifted")
    steps = atif.get("steps")
    if not isinstance(steps, list) or not steps \
            or not isinstance(steps[-1], dict) \
            or steps[-1].get("message") != projection["finalOutput"]:
        raise HarborTofuExportError("Tofu ATIF final output drifted")


def _prepare_trial(
    *, trial_dir: Path, release_task: dict[str, Any],
    harbor_manifest: dict[str, Any], job_id: str,
    settings: TofuKimiCandidateSettings,
) -> _PreparedTrial:
    trial_name = trial_dir.name
    config = _load_json(trial_dir / "config.json", "Harbor trial config")
    task_name, task_ref = _trial_task_identity(config)
    if task_name != str(release_task.get("sourceTaskId") or ""):
        raise HarborTofuExportError(
            f"Harbor trial {trial_name} maps to a different release task")
    source_sha = str(release_task.get("sourceSha256") or "")
    if source_sha and source_sha != task_ref:
        raise HarborTofuExportError(
            f"Harbor trial {trial_name} task ref drifted")
    result = _load_json(trial_dir / "result.json", "Harbor trial result")
    result_task = (result.get("config") or {}).get("task")
    if not isinstance(result_task, dict) \
            or str(result_task.get("name") or "") != task_name \
            or str(result_task.get("ref") or "") != task_ref:
        raise HarborTofuExportError(
            "Harbor result embeds a different task config")
    if result.get("exception_info") is not None:
        raise HarborTofuExportError(
            f"Harbor trial {trial_name} has an unresolved exception")
    agent = result.get("agent_info") or {}
    model = agent.get("model_info") if isinstance(agent, dict) else None
    if not isinstance(agent, dict) \
            or agent.get("name") != "tofu-kimi-runtime" \
            or agent.get("version") != settings.agent_version \
            or not isinstance(model, dict) or model.get("name") != "kimi-k3":
        raise HarborTofuExportError("Harbor trial agent/model identity drifted")

    started = _timestamp(result.get("started_at"), "trial.started_at")
    verifier = _phase_timing(result.get("verifier"), "verifier")
    verifier_finished = _timestamp(
        verifier["finishedAt"], "verifier.finished_at")
    finished = _timestamp(result.get("finished_at"), "trial.finished_at")
    if verifier_finished < started or finished < verifier_finished:
        raise HarborTofuExportError(
            "Harbor trial lifecycle lacks a valid oracle-ready point")
    phases = {
        "environmentSetup": _phase_timing(
            result.get("environment_setup"), "environment_setup"),
        "agentSetup": _phase_timing(result.get("agent_setup"), "agent_setup"),
        "agentExecution": _phase_timing(
            result.get("agent_execution"), "agent_execution"),
        "verifier": verifier,
    }
    evidence_dir = trial_dir / "agent" / "tofu-kimi-evidence"
    native = evidence_dir / "events.jsonl"
    runtime_evidence = evidence_dir / "runtime-evidence.json"
    tool_audit = evidence_dir / "tool-audit.json"
    trajectory = trial_dir / "agent" / "trajectory.json"
    runtime_record = settings.manifest_record()
    try:
        projection = project_tofu_trial(
            native_events=native,
            runtime_evidence=runtime_evidence,
            tool_audit=tool_audit,
            runtime_config=dict(settings.runtime_config),
            expected_runtime_config_digest=settings.runtime_config_sha256,
            expected_prompt_contract_digest=runtime_record[
                "promptContractSha256"],
            expected_tool_schema_digest=runtime_record["toolSchemaSha256"],
        )
    except TofuProjectionError as exc:
        raise HarborTofuExportError(
            f"Harbor trial {trial_name} evidence is inconsistent") from exc
    _validate_atif(trajectory, settings=settings, projection=projection)
    passed, score = _reward(result)
    return _PreparedTrial(
        task_id=str(release_task["taskId"]),
        trial_name=trial_name,
        native_events=native,
        runtime_evidence=runtime_evidence,
        tool_audit=tool_audit,
        agent_trajectory=trajectory,
        projection=projection,
        oracle={
            "passed": passed,
            "type": "harbor_verifier_reward",
            "score": score,
            "rewardKey": "reward",
            "verifierFinishedAt": verifier["finishedAt"],
        },
        task_started_at_unix_ns=_unix_ns(started),
        completed_at_unix_ms=_unix_ns(verifier_finished) // 1_000_000,
        environment={
            "releaseAttempt": {
                "taskStartedAtUnixMs": _unix_ns(started) // 1_000_000,
            },
            "harbor": {
                "runId": harbor_manifest.get("run_id"),
                "jobId": job_id,
                "trialId": result.get("id"),
                "trialName": trial_name,
                "taskChecksum": result.get("task_checksum"),
                "taskStartedAt": started.isoformat(),
                "oracleReadyAt": verifier["finishedAt"],
                "latencyScope": "trial.started_at_to_verifier.finished_at",
                "phases": phases,
            },
            "tofuEvidence": dict(projection["evidenceDigests"]),
        },
    )


def export_tofu_harbor_run(
    *, harbor_run_dir: Path, release_run_root: Path,
) -> dict[str, Any]:
    """Validate and idempotently commit one candidate Harbor slice."""

    candidate = harbor_run_dir.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise HarborTofuExportError(
            "Harbor run directory must be a real non-symlink directory")
    run_dir = candidate.resolve(strict=True)
    harbor_manifest = _load_json(run_dir / "manifest.json", "Harbor manifest")
    if harbor_manifest.get("status") != "succeeded":
        raise HarborTofuExportError("Harbor run is not successful")
    current_audit = audit_run(run_dir)
    if not current_audit.get("ok"):
        failures = [
            row.get("name") for row in current_audit.get("checks") or []
            if isinstance(row, dict) and not row.get("ok")
        ]
        raise HarborTofuExportError(
            f"Harbor run does not pass its current audit: {failures[:8]}")
    release_manifest = load_release_manifest(release_run_root)
    config_path = Path(str(harbor_manifest.get("job_config") or ""))
    if config_path.resolve(strict=True) != (run_dir / "job-config.json").resolve(
            strict=True):
        raise HarborTofuExportError("Harbor job config is not run-owned")
    job_config = _load_json(config_path, "Harbor job config")
    job_dir = run_dir / "jobs" / str(harbor_manifest.get("run_id") or "")
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise HarborTofuExportError("Harbor job directory is missing")
    job_result = _load_json(job_dir / "result.json", "Harbor job result")
    dataset, settings = _formal_binding(
        harbor_manifest=harbor_manifest,
        release_manifest=release_manifest,
        job_config=job_config,
        job_result=job_result,
    )
    tracking = harbor_manifest.get("release_attempt_tracking")
    if harbor_manifest.get("release_evidence_eligible") is not True \
            or not isinstance(tracking, dict):
        raise HarborTofuExportError(
            "Harbor run was not preclaimed before paid dispatch")
    try:
        tracked_execution = validate_tofu_harbor_release_attempts(
            release_run_root=release_run_root,
            tracking=tracking,
            harbor_manifest=harbor_manifest,
            job_config=job_config,
            allow_oracle_ready=True,
        )
    except HarborReleaseTrackingError as exc:
        raise HarborTofuExportError(
            "Harbor release attempt tracking is invalid") from exc

    release_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in release_manifest.get("tasks") or []:
        if row.get("dataset") != dataset:
            continue
        source = str(row.get("sourceTaskId") or "")
        if not source:
            raise HarborTofuExportError(
                f"release task {row.get('taskId')} lacks sourceTaskId")
        release_by_source.setdefault(source, []).append(row)

    trial_rows: list[tuple[Path, str]] = []
    for trial_dir in sorted(job_dir.iterdir(), key=lambda path: path.name):
        if not trial_dir.is_dir() or trial_dir.is_symlink() \
                or not (trial_dir / "config.json").is_file():
            continue
        config = _load_json(trial_dir / "config.json", "Harbor trial config")
        source, _ref = _trial_task_identity(config)
        if source not in release_by_source:
            raise HarborTofuExportError(
                f"Harbor task is absent from release manifest: {source}")
        trial_rows.append((trial_dir, source))
    expected_trials = int(harbor_manifest.get("expected_trials") or 0)
    if len(trial_rows) != expected_trials or expected_trials <= 0:
        raise HarborTofuExportError(
            "Harbor trial directory count differs from its frozen manifest")
    grouped: dict[str, list[Path]] = {}
    for trial_dir, source in trial_rows:
        grouped.setdefault(source, []).append(trial_dir)
    prepared: list[_PreparedTrial] = []
    for source, trial_dirs in sorted(grouped.items()):
        for trial_dir, release_task in _ordered_trial_pairs(
                source, trial_dirs, release_by_source[source]):
            prepared.append(_prepare_trial(
                trial_dir=trial_dir,
                release_task=release_task,
                harbor_manifest=harbor_manifest,
                job_id=str(job_result.get("id") or ""),
                settings=settings,
            ))
    if sorted(row.task_id for row in prepared) != sorted(
            tracked_execution["taskIds"]):
        raise HarborTofuExportError(
            "prepared Harbor trials differ from pre-dispatch claims")
    claims_by_task = {
        str(row["taskId"]): row for row in tracked_execution["claims"]
    }
    if any(
        int(claims_by_task[row.task_id]["occurredAt"]) * 1_000_000
        > row.task_started_at_unix_ns for row in prepared
    ):
        raise HarborTofuExportError(
            "Harbor trial started before its release attempt was claimed")

    statuses: dict[str, int] = {}
    for trial in prepared:
        retries = release_task_retry_evidence(
            release_run_root, task_id=trial.task_id)
        successful_start_ms = trial.task_started_at_unix_ns // 1_000_000
        latency_start_ms = min(
            [successful_start_ms]
            + [int(retry["taskStartedAtUnixMs"]) for retry in retries]
        )
        oracle_ready_ms = trial.completed_at_unix_ms - latency_start_ms
        if oracle_ready_ms < 0:
            raise HarborTofuExportError(
                "Harbor retry evidence starts after oracle-ready")
        retry_artifacts = [
            artifact for retry in retries
            for artifact in retry.get("artifacts") or []
        ]
        artifacts = [
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="raw_trajectory", source=trial.native_events),
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="runtime_evidence", source=trial.runtime_evidence),
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="tool_audit", source=trial.tool_audit),
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="agent_trajectory", source=trial.agent_trajectory),
            *retry_artifacts,
        ]
        record = build_tofu_release_task_record(
            manifest=release_manifest,
            task_id=trial.task_id,
            projection=trial.projection,
            oracle=trial.oracle,
            artifacts=artifacts,
            task_started_at_unix_ns=trial.task_started_at_unix_ns,
            oracle_ready_ms=oracle_ready_ms,
            completed_at_unix_ms=trial.completed_at_unix_ms,
            retries=retries,
            environment=trial.environment,
        )
        status = record_release_task(release_run_root, record)["status"]
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "contractVersion": HARBOR_TOFU_EXPORT_CONTRACT,
        "status": "exported",
        "harborRunId": harbor_manifest.get("run_id"),
        "releaseRunId": release_manifest.get("runId"),
        "dataset": dataset,
        "trials": len(prepared),
        "records": statuses,
        "trialIndexRule": "lexical_harbor_trial_name_within_source_task",
        "latencyScope": "trial.started_at_to_verifier.finished_at",
    }


__all__ = [
    "HARBOR_TOFU_EXPORT_CONTRACT",
    "HarborTofuExportError",
    "export_tofu_harbor_run",
]
