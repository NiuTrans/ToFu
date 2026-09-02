"""Project audited Harbor Codex trials into immutable release evidence.

The Harbor result and per-trial files remain the execution authority.  This
module binds them to a preregistered ``tofu-benchmark/v2`` baseline manifest,
uses the verifier finish time as oracle-ready, and commits only fully
reconciled trials.  Harbor's current retry implementation deletes failed trial
directories, so a run with an observed retry is rejected instead of silently
dropping failed-call cost or failure evidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION
from evaluations.swebench.audit import audit_run
from evaluations.swebench.codex_kimi_runtime import CODEX_KIMI_AGENT
from evaluations.swebench.constants import HARBOR_COMMIT

from .codex_projection import (
    build_codex_release_task_record,
    project_codex_trial,
)
from .harbor_tracking import (
    HarborReleaseTrackingError,
    validate_codex_harbor_release_attempts,
)
from .run_store import (
    load_release_manifest,
    record_release_task,
    release_task_retry_evidence,
    store_run_artifact,
)


HARBOR_CODEX_EXPORT_CONTRACT = "tofu-harbor-codex-export/v1"
_BENCHMARK_DATASETS = {
    "swebench-verified": "swe-bench-verified",
    "terminal-bench-2.1": "terminal-bench-2.1",
}


class HarborCodexExportError(ValueError):
    """The Harbor run cannot serve as complete release evidence."""


@dataclass(frozen=True)
class _PreparedTrial:
    task_id: str
    trial_name: str
    raw_trajectory: Path
    proxy_metrics: Path
    agent_trajectory: Path
    projection: dict[str, Any]
    oracle: dict[str, Any]
    task_started_at_unix_ns: int
    oracle_ready_ms: float
    completed_at_unix_ms: int
    environment: dict[str, Any]


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarborCodexExportError(
                f"JSON evidence contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise HarborCodexExportError(
        f"JSON evidence contains non-finite number {value}"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HarborCodexExportError(
            f"{label} must be a regular non-symlink file"
        )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarborCodexExportError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise HarborCodexExportError(f"{label} must contain a JSON object")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise HarborCodexExportError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarborCodexExportError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarborCodexExportError(f"{label} must include a timezone")
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
        raise HarborCodexExportError(f"trial {label} timing is required")
    started = _timestamp(value.get("started_at"), f"{label}.started_at")
    finished = _timestamp(value.get("finished_at"), f"{label}.finished_at")
    duration_ms = (finished - started).total_seconds() * 1000
    if duration_ms < 0:
        raise HarborCodexExportError(f"trial {label} finishes before it starts")
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
        raise HarborCodexExportError(
            "Harbor verifier must provide a numeric reward"
        )
    score = float(raw)
    if not math.isfinite(score) or score not in {0.0, 1.0}:
        raise HarborCodexExportError(
            "release software-task reward must be exactly 0 or 1"
        )
    return score == 1.0, score


def _formal_binding(
    *, harbor_manifest: dict[str, Any], release_manifest: dict[str, Any],
    job_config: dict[str, Any], job_result: dict[str, Any],
) -> str:
    runtime = harbor_manifest.get("codex_kimi_runtime")
    if not isinstance(runtime, dict) \
            or harbor_manifest.get("agent") != CODEX_KIMI_AGENT:
        raise HarborCodexExportError("Harbor run is not the formal Codex baseline")
    release_agent = release_manifest.get("agent") or {}
    if release_manifest.get("comparisonRole") != "baseline" \
            or not isinstance(release_agent, dict) \
            or release_agent.get("name") != "codex" \
            or release_agent.get("version") != CODEX_VERSION \
            or release_agent.get("binarySha256") != runtime.get("codexSha256"):
        raise HarborCodexExportError(
            "release run is not bound to this Codex 0.149.1 binary"
        )
    bindings = {
        "model": "kimi-k3",
        "providerFace": runtime.get("providerFace"),
        "providerSlotId": runtime.get("providerSlotId"),
        "thinking": harbor_manifest.get("reasoning_effort"),
    }
    drift = [
        field for field, expected in bindings.items()
        if release_manifest.get(field) != expected
    ]
    if drift:
        raise HarborCodexExportError(
            f"release/Harbor model-provider controls drifted: {drift}"
        )
    if release_manifest.get("harness") != harbor_manifest.get(
        "harness_identity"
    ):
        raise HarborCodexExportError(
            "release harness identity does not match the executed Harbor binary"
        )
    if (harbor_manifest.get("harness_identity") or {}).get(
        "sourceCommit"
    ) != HARBOR_COMMIT:
        raise HarborCodexExportError("Harbor source commit is not pinned")
    if (harbor_manifest.get("harness_identity") or {}).get(
        "runnerProjectDirty"
    ) is not False:
        raise HarborCodexExportError(
            "formal release requires a clean pinned runner revision"
        )
    sandbox = release_manifest.get("sandbox") or {}
    if sandbox != harbor_manifest.get("sandbox_identity"):
        raise HarborCodexExportError(
            "release sandbox identity does not match the executed rootless QEMU"
        )
    timeout_seconds = (release_manifest.get("limits") or {}).get(
        "timeoutSeconds"
    )
    if timeout_seconds != runtime.get("agentTimeoutSeconds") \
            or float(job_config.get("timeout_multiplier", 1.0)) != 1.0:
        raise HarborCodexExportError(
            "release/Harbor timeout controls drifted"
        )
    retry = job_config.get("retry") or {}
    max_retries = retry.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) \
            or max_retries != 0:
        raise HarborCodexExportError(
            "formal release disables Harbor's evidence-erasing internal retries; "
            "external retries use the immutable release attempt ledger"
        )
    observed_retries = (job_result.get("stats") or {}).get("n_retries", 0)
    if isinstance(observed_retries, bool) \
            or not isinstance(observed_retries, int) \
            or observed_retries != 0:
        raise HarborCodexExportError(
            "Harbor retries delete failed-attempt evidence; refusing incomplete "
            "cost/retry projection"
        )
    benchmark = str(harbor_manifest.get("benchmark") or "")
    try:
        return _BENCHMARK_DATASETS[benchmark]
    except KeyError as exc:
        raise HarborCodexExportError(
            f"unsupported formal Harbor benchmark: {benchmark!r}"
        ) from exc


def _trial_task_identity(config: dict[str, Any]) -> tuple[str, str]:
    task = config.get("task")
    if not isinstance(task, dict):
        raise HarborCodexExportError("Harbor trial task config is missing")
    name = str(task.get("name") or "")
    ref = str(task.get("ref") or "")
    if not name or not ref:
        raise HarborCodexExportError(
            "Harbor trial task name and immutable ref are required"
        )
    return name, ref


def _ordered_trial_pairs(
    source: str,
    trial_dirs: list[Path],
    release_tasks: list[dict[str, Any]],
) -> list[tuple[Path, dict[str, Any]]]:
    ordered_tasks = sorted(
        release_tasks, key=lambda row: int(row.get("trialIndex") or 0)
    )
    indices = [row.get("trialIndex") for row in ordered_tasks]
    if indices != list(range(1, len(ordered_tasks) + 1)) \
            or len(ordered_tasks) != len(trial_dirs):
        raise HarborCodexExportError(
            f"release attempt shape differs for source task {source}"
        )
    # Harbor generates opaque random suffixes. Lexical ordering is frozen,
    # deterministic, and independent of reward/timing, so it cannot select
    # favorable attempt pairings.
    return list(zip(
        sorted(trial_dirs, key=lambda path: path.name),
        ordered_tasks,
        strict=True,
    ))


def _prepare_trial(
    *, trial_dir: Path, release_task: dict[str, Any],
    harbor_manifest: dict[str, Any], job_id: str,
) -> _PreparedTrial:
    config = _load_json(trial_dir / "config.json", "Harbor trial config")
    result = _load_json(trial_dir / "result.json", "Harbor trial result")
    trial_name = str(result.get("trial_name") or "")
    if trial_name != trial_dir.name:
        raise HarborCodexExportError(
            "Harbor trial directory and result trial_name differ"
        )
    task_name, task_ref = _trial_task_identity(config)
    if task_name != release_task.get("sourceTaskId") \
            or task_ref != release_task.get("sourceSha256"):
        raise HarborCodexExportError(
            f"Harbor task lock drifted for {release_task.get('taskId')}"
        )
    result_config = result.get("config") or {}
    result_task = result_config.get("task") \
        if isinstance(result_config, dict) else None
    if not isinstance(result_task, dict) \
            or str(result_task.get("name") or "") != task_name \
            or str(result_task.get("ref") or "") != task_ref:
        raise HarborCodexExportError("Harbor result embeds a different task config")
    if result.get("exception_info") is not None:
        raise HarborCodexExportError(
            f"Harbor trial {trial_name} has an unresolved exception"
        )
    agent = result.get("agent_info") or {}
    model = agent.get("model_info") if isinstance(agent, dict) else None
    if not isinstance(agent, dict) or agent.get("name") != "codex-kimi-guest" \
            or agent.get("version") != CODEX_VERSION \
            or not isinstance(model, dict) or model.get("name") != "kimi-k3":
        raise HarborCodexExportError("Harbor trial agent/model identity drifted")

    started = _timestamp(result.get("started_at"), "trial.started_at")
    verifier = _phase_timing(result.get("verifier"), "verifier")
    verifier_finished = _timestamp(
        verifier["finishedAt"], "verifier.finished_at"
    )
    oracle_ready_ms = (verifier_finished - started).total_seconds() * 1000
    finished = _timestamp(result.get("finished_at"), "trial.finished_at")
    if oracle_ready_ms < 0 or finished < verifier_finished:
        raise HarborCodexExportError(
            "Harbor trial lifecycle does not contain a valid oracle-ready point"
        )
    phases = {
        "environmentSetup": _phase_timing(
            result.get("environment_setup"), "environment_setup"
        ),
        "agentSetup": _phase_timing(result.get("agent_setup"), "agent_setup"),
        "agentExecution": _phase_timing(
            result.get("agent_execution"), "agent_execution"
        ),
        "verifier": verifier,
    }
    evidence = trial_dir / "agent" / "codex-kimi-evidence"
    raw = evidence / "codex-events.jsonl"
    metrics = evidence / "proxy-metrics.jsonl"
    trajectory = trial_dir / "agent" / "trajectory.json"
    projection = project_codex_trial(
        raw_trajectory=raw,
        proxy_metrics=metrics,
    )
    passed, score = _reward(result)
    return _PreparedTrial(
        task_id=str(release_task["taskId"]),
        trial_name=trial_name,
        raw_trajectory=raw,
        proxy_metrics=metrics,
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
        oracle_ready_ms=oracle_ready_ms,
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
        },
    )


def export_codex_harbor_run(
    *, harbor_run_dir: Path, release_run_root: Path,
) -> dict[str, Any]:
    """Validate and idempotently commit one Harbor benchmark slice."""

    candidate = harbor_run_dir.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise HarborCodexExportError(
            "Harbor run directory must be a real non-symlink directory"
        )
    run_dir = candidate.resolve(strict=True)
    harbor_manifest = _load_json(run_dir / "manifest.json", "Harbor manifest")
    if harbor_manifest.get("status") != "succeeded":
        raise HarborCodexExportError("Harbor run is not successful")
    current_audit = audit_run(run_dir)
    if not current_audit.get("ok"):
        failures = [
            row.get("name") for row in current_audit.get("checks") or []
            if isinstance(row, dict) and not row.get("ok")
        ]
        raise HarborCodexExportError(
            f"Harbor run does not pass its current audit: {failures[:8]}"
        )
    release_manifest = load_release_manifest(release_run_root)
    config_path = Path(str(harbor_manifest.get("job_config") or ""))
    if config_path.resolve(strict=True) != (run_dir / "job-config.json").resolve(
        strict=True
    ):
        raise HarborCodexExportError("Harbor job config is not run-owned")
    job_config = _load_json(config_path, "Harbor job config")
    job_dir = run_dir / "jobs" / str(harbor_manifest.get("run_id") or "")
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise HarborCodexExportError("Harbor job directory is missing")
    job_result = _load_json(job_dir / "result.json", "Harbor job result")
    dataset = _formal_binding(
        harbor_manifest=harbor_manifest,
        release_manifest=release_manifest,
        job_config=job_config,
        job_result=job_result,
    )
    tracking = harbor_manifest.get("release_attempt_tracking")
    if harbor_manifest.get("release_evidence_eligible") is not True \
            or not isinstance(tracking, dict):
        raise HarborCodexExportError(
            "Harbor run was not preclaimed before paid dispatch")
    try:
        tracked_execution = validate_codex_harbor_release_attempts(
            release_run_root=release_run_root,
            tracking=tracking,
            harbor_manifest=harbor_manifest,
            job_config=job_config,
            allow_oracle_ready=True,
        )
    except HarborReleaseTrackingError as exc:
        raise HarborCodexExportError(
            "Harbor release attempt tracking is invalid") from exc

    release_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in release_manifest.get("tasks") or []:
        if row.get("dataset") != dataset:
            continue
        source = str(row.get("sourceTaskId") or "")
        if not source:
            raise HarborCodexExportError(
                f"release task {row.get('taskId')} lacks sourceTaskId"
            )
        release_by_source.setdefault(source, []).append(row)

    trial_rows: list[tuple[Path, dict[str, Any], str]] = []
    for trial_dir in sorted(job_dir.iterdir(), key=lambda path: path.name):
        if not trial_dir.is_dir() or trial_dir.is_symlink() \
                or not (trial_dir / "config.json").is_file():
            continue
        config = _load_json(trial_dir / "config.json", "Harbor trial config")
        source, _ref = _trial_task_identity(config)
        if source not in release_by_source:
            raise HarborCodexExportError(
                f"Harbor task is absent from release manifest: {source}"
            )
        trial_rows.append((trial_dir, config, source))
    expected_trials = int(harbor_manifest.get("expected_trials") or 0)
    if len(trial_rows) != expected_trials or expected_trials <= 0:
        raise HarborCodexExportError(
            "Harbor trial directory count differs from its frozen manifest"
        )

    grouped: dict[str, list[Path]] = {}
    for trial_dir, _config, source in trial_rows:
        grouped.setdefault(source, []).append(trial_dir)
    prepared: list[_PreparedTrial] = []
    for source, trial_dirs in sorted(grouped.items()):
        for trial_dir, release_task in _ordered_trial_pairs(
            source, trial_dirs, release_by_source[source]
        ):
            prepared.append(_prepare_trial(
                trial_dir=trial_dir,
                release_task=release_task,
                harbor_manifest=harbor_manifest,
                job_id=str(job_result.get("id") or ""),
            ))
    tracked_task_ids = tracked_execution["taskIds"]
    if sorted(trial.task_id for trial in prepared) != sorted(tracked_task_ids):
        raise HarborCodexExportError(
            "prepared Harbor trials differ from their pre-dispatch claims")
    claims_by_task = {
        str(row["taskId"]): row for row in tracked_execution["claims"]
    }
    if any(
        int(claims_by_task[trial.task_id]["occurredAt"]) * 1_000_000
        > trial.task_started_at_unix_ns
        for trial in prepared
    ):
        raise HarborCodexExportError(
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
            raise HarborCodexExportError(
                "Harbor retry evidence starts after oracle-ready")
        retry_artifacts = [
            artifact for retry in retries
            for artifact in retry.get("artifacts") or []
        ]
        artifacts = [
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="raw_trajectory", source=trial.raw_trajectory,
            ),
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="proxy_metrics", source=trial.proxy_metrics,
            ),
            store_run_artifact(
                release_run_root, task_id=trial.task_id,
                kind="agent_trajectory", source=trial.agent_trajectory,
            ),
            *retry_artifacts,
        ]
        record = build_codex_release_task_record(
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
        "contractVersion": HARBOR_CODEX_EXPORT_CONTRACT,
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
    "HARBOR_CODEX_EXPORT_CONTRACT",
    "HarborCodexExportError",
    "export_codex_harbor_run",
]
