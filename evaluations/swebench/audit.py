from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import (
    BENCHMARKS,
    DEFAULT_BENCHMARK,
    HARBOR_COMMIT,
    ISOLATED_BACKENDS,
    OFFICIAL_DATASET,
    SWEBENCH_VERSION,
    terminal_bench_21_task_digests,
)
from .official import load_predictions, normalized_predictions_sha256


@dataclass(frozen=True)
class AuditCheck:
    name: str
    ok: bool
    detail: str


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _artifact_checks(run_dir: Path) -> list[AuditCheck]:
    checks = []
    for name in (".gitignore", ".ignore"):
        path = run_dir / name
        protected = path.is_file() and path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("*")
        checks.append(AuditCheck(f"artifact_{name}", protected, "self-ignoring" if protected else "missing wildcard ignore"))
    return checks


def _audit_harbor(run_dir: Path, manifest: dict[str, Any], allow_errors: bool) -> list[AuditCheck]:
    checks = _artifact_checks(run_dir)
    config_path = Path(str(manifest.get("job_config") or run_dir / "job-config.json"))
    config = _load_json(config_path)
    environment = config.get("environment") or {}
    backend = environment.get("type")
    checks.append(AuditCheck("isolated_backend", backend in ISOLATED_BACKENDS, str(backend)))
    checks.append(AuditCheck("ephemeral_environment", environment.get("delete") is True, f"delete={environment.get('delete')}"))
    checks.append(
        AuditCheck(
            "no_host_mounts",
            not environment.get("mounts"),
            f"mounts={environment.get('mounts') or []}",
        )
    )
    if backend == "singularity":
        agents = config.get("agents") or []
        serial = int(config.get("n_concurrent_trials") or 0) == 1 and all(
            agent.get("n_concurrent") in {None, 1}
            for agent in agents
        )
        checks.append(
            AuditCheck(
                "serial_local_runtime",
                serial,
                f"trials={config.get('n_concurrent_trials')}",
            )
        )
        disclosed = (
            manifest.get("local_execution") is True
            and manifest.get("network_namespace_isolation") is False
            and manifest.get("strict_cgroup_isolation") is False
        )
        checks.append(
            AuditCheck(
                "local_isolation_disclosed",
                disclosed,
                "Singularity shares host networking and lacks strict per-trial cgroups",
            )
        )
    benchmark = str(manifest.get("benchmark") or DEFAULT_BENCHMARK)
    definition = BENCHMARKS.get(benchmark)
    checks.append(
        AuditCheck(
            "audited_benchmark",
            definition is not None,
            benchmark,
        )
    )
    checks.append(
        AuditCheck(
            "pinned_harbor",
            manifest.get("harbor_source_commit") == HARBOR_COMMIT,
            str(manifest.get("harbor_source_commit")),
        )
    )
    checks.append(
        AuditCheck(
            "upload_disabled",
            manifest.get("upload_enabled") is False,
            f"upload_enabled={manifest.get('upload_enabled')}",
        )
    )
    models = [agent.get("model_name") for agent in config.get("agents") or []]
    manifest_models = manifest.get("models") or []
    unique_models = (
        bool(models)
        and len(models) == len(set(models))
        and models == manifest_models
    )
    checks.append(AuditCheck("unique_model_configs", unique_models, f"models={models}"))
    dataset_configs = config.get("datasets") or []
    dataset_config = dataset_configs[0] if len(dataset_configs) == 1 else {}
    configured_dataset = (
        f"{dataset_config.get('name')}@{dataset_config.get('version')}"
        if dataset_config.get("version")
        else (
            f"{dataset_config.get('name')}@{dataset_config.get('ref')}"
            if dataset_config.get("ref")
            else str(dataset_config.get("name"))
        )
    )
    dataset_pinned = (
        definition is not None
        and configured_dataset == definition.dataset
        and manifest.get("dataset") == definition.dataset
        and manifest.get("dataset_source_revision")
        == definition.dataset_source_revision
        and manifest.get("benchmark_source_commit") == definition.source_commit
    )
    checks.append(
        AuditCheck(
            "pinned_dataset",
            dataset_pinned,
            f"dataset={configured_dataset}, source_revision={manifest.get('dataset_source_revision')}",
        )
    )
    attempts = int(manifest.get("attempts_per_task") or 0)
    config_attempts = int(config.get("n_attempts") or 0)
    checks.append(
        AuditCheck(
            "attempts_per_task",
            attempts > 0 and attempts == config_attempts,
            f"manifest={attempts}, config={config_attempts}",
        )
    )

    result_path = run_dir / "jobs" / str(manifest.get("run_id")) / "result.json"
    if not result_path.is_file():
        checks.append(AuditCheck("job_result", False, f"missing {result_path}"))
        return checks
    result = _load_json(result_path)
    stats = result.get("stats") or {}
    total = int(result.get("n_total_trials") or 0)
    completed = int(stats.get("n_completed_trials") or stats.get("n_trials") or 0)
    errors = int(stats.get("n_errored_trials") or stats.get("n_errors") or 0)
    pending = int(stats.get("n_pending_trials") or 0)
    running = int(stats.get("n_running_trials") or 0)
    expected = int(manifest.get("expected_trials") or 0)
    checks.append(AuditCheck("positive_expected_trials", expected > 0, f"expected={expected}"))
    trial_dirs = [
        path
        for path in result_path.parent.iterdir()
        if path.is_dir() and (path / "config.json").is_file()
    ]
    trial_pairs: list[tuple[str, str]] = []
    terminal_bench_task_refs: list[tuple[str, str, str]] = []
    for trial_dir in trial_dirs:
        trial_config = _load_json(trial_dir / "config.json")
        agent = trial_config.get("agent") or {}
        task = trial_config.get("task") or {}
        task_identity = str(task.get("name") or Path(str(task.get("path") or "")).name)
        trial_pairs.append((str(agent.get("model_name") or ""), task_identity))
        if benchmark == "terminal-bench-2.1":
            terminal_bench_task_refs.append(
                (
                    task_identity,
                    str(task.get("ref") or ""),
                    str(task.get("source") or ""),
                )
            )
    checks.append(
        AuditCheck(
            "one_directory_per_trial",
            len(trial_dirs) == expected,
            f"directories={len(trial_dirs)}, expected={expected}",
        )
    )
    trial_result_paths = [
        trial_dir / "result.json"
        for trial_dir in trial_dirs
        if (trial_dir / "result.json").is_file()
    ]
    checks.append(
        AuditCheck(
            "one_result_per_trial",
            len(trial_result_paths) == expected,
            f"results={len(trial_result_paths)}, expected={expected}",
        )
    )
    pair_counts = Counter(trial_pairs)
    expected_pairs = (
        int(manifest.get("expected_tasks") or 0)
        * len(manifest_models)
    )
    checks.append(
        AuditCheck(
            "model_task_attempts",
            len(pair_counts) == expected_pairs
            and all(count == attempts for count in pair_counts.values()),
            f"pairs={len(pair_counts)}, expected_pairs={expected_pairs}, attempts={sorted(set(pair_counts.values()))}",
        )
    )
    if benchmark == "terminal-bench-2.1":
        expected_task_refs = terminal_bench_21_task_digests()
        valid_task_refs = sum(
            expected_task_refs.get(name) == ref
            and source == "terminal-bench/terminal-bench-2-1"
            for name, ref, source in terminal_bench_task_refs
        )
        checks.append(
            AuditCheck(
                "canonical_task_digests",
                valid_task_refs == expected,
                f"valid={valid_task_refs}, expected={expected}",
            )
        )
        rewarded = 0
        rewarded_with_trajectory = 0
        for trial_result_path in trial_result_paths:
            trial_dir = trial_result_path.parent
            trial_result = _load_json(trial_result_path)
            rewards = (trial_result.get("verifier_result") or {}).get("rewards") or {}
            try:
                successful = float(rewards.get("reward") or 0) > 0
            except (TypeError, ValueError):
                successful = False
            if successful:
                rewarded += 1
                if (trial_dir / "agent" / "trajectory.json").is_file():
                    rewarded_with_trajectory += 1
        checks.append(
            AuditCheck(
                "rewarded_trials_have_trajectories",
                rewarded == rewarded_with_trajectory,
                f"rewarded={rewarded}, with_trajectory={rewarded_with_trajectory}",
            )
        )
    checks.append(AuditCheck("trial_cardinality", total == expected, f"actual={total}, expected={expected}"))
    checks.append(AuditCheck("all_trials_terminal", completed == total and pending == 0 and running == 0, f"completed={completed}, total={total}, pending={pending}, running={running}"))
    checks.append(AuditCheck("infrastructure_errors", allow_errors or errors == 0, f"errors={errors}"))
    checks.append(AuditCheck("launcher_status", manifest.get("status") == "succeeded", str(manifest.get("status"))))
    return checks


def _audit_official(run_dir: Path, manifest: dict[str, Any]) -> list[AuditCheck]:
    checks = _artifact_checks(run_dir)
    backend = manifest.get("backend")
    digest = str(manifest.get("normalized_predictions_sha256") or "")
    pinned = (
        manifest.get("dataset") == OFFICIAL_DATASET
        and manifest.get("swebench_version") == SWEBENCH_VERSION
    )
    checks.append(AuditCheck("isolated_backend", backend in {"modal", "docker"}, str(backend)))
    checks.append(
        AuditCheck(
            "pinned_official_harness",
            pinned,
            f"dataset={manifest.get('dataset')}, swebench={manifest.get('swebench_version')}",
        )
    )
    groups = manifest.get("groups") or {}
    prediction_rows: list[dict[str, str]] = []
    prediction_error = ""
    try:
        for key in groups:
            prediction_rows.extend(
                load_predictions(run_dir / "models" / key / "predictions.jsonl")
            )
    except (OSError, ValueError) as exc:
        prediction_error = str(exc)
    actual_digest = (
        normalized_predictions_sha256(prediction_rows) if prediction_rows else ""
    )
    expected_predictions = int(manifest.get("prediction_count") or 0)
    digest_ok = (
        not prediction_error
        and len(prediction_rows) == expected_predictions
        and actual_digest == digest
    )
    checks.append(
        AuditCheck(
            "prediction_digest",
            digest_ok,
            prediction_error
            or f"actual={actual_digest}, recorded={digest}, rows={len(prediction_rows)}",
        )
    )
    statuses = [group.get("status") for group in groups.values() if isinstance(group, dict)]
    checks.append(AuditCheck("model_groups_present", bool(groups), f"groups={len(groups)}"))
    checks.append(AuditCheck("all_model_groups_succeeded", bool(statuses) and all(status == "succeeded" for status in statuses), f"statuses={statuses}"))
    checks.append(AuditCheck("launcher_status", manifest.get("status") == "succeeded", str(manifest.get("status"))))
    for key, group in groups.items():
        model_dir = run_dir / "models" / key
        run_id = group.get("backend_run_id") if isinstance(group, dict) else None
        reports = list(model_dir.glob(f"**/*.{run_id}.json")) if run_id else []
        checks.append(AuditCheck(f"report_{key}", bool(reports), str(reports[0]) if reports else "official report missing"))
        if reports:
            report = _load_json(reports[0])
            expected = int(group.get("instances") or 0)
            submitted = int(report.get("submitted_instances") or 0)
            completed = int(report.get("completed_instances") or 0)
            empty = int(report.get("empty_patch_instances") or 0)
            errors = int(report.get("error_instances") or 0)
            checks.append(
                AuditCheck(
                    f"report_cardinality_{key}",
                    submitted == expected and completed + empty == expected,
                    f"submitted={submitted}, completed={completed}, empty={empty}, expected={expected}",
                )
            )
            checks.append(
                AuditCheck(
                    f"report_errors_{key}",
                    errors == 0,
                    f"errors={errors}",
                )
            )
    return checks


def audit_run(run_dir: Path, *, allow_errors: bool = False) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = _load_json(manifest_path)
    kind = manifest.get("kind")
    if kind == "harbor-agent-evaluation":
        checks = _audit_harbor(run_dir, manifest, allow_errors)
    elif kind == "official-patch-evaluation":
        checks = _audit_official(run_dir, manifest)
    else:
        raise ValueError(f"unknown evaluation manifest kind: {kind!r}")
    return {
        "ok": all(check.ok for check in checks),
        "run_dir": str(run_dir),
        "kind": kind,
        "checks": [asdict(check) for check in checks],
    }
