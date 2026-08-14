from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .artifacts import atomic_write_json, create_run_dir, make_run_id, utc_now, validate_run_id
from .constants import (
    BENCHMARKS,
    BenchmarkDefinition,
    DEFAULT_BENCHMARK,
    FRAMEWORK_VERSION,
    HARBOR_COMMIT,
    ISOLATED_BACKENDS,
)
from .process import prepare_runtime_environment, resolve_executable, run_streaming


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class HarborRunSpec:
    agent: str
    models: tuple[str, ...]
    backend: str
    output_root: Path
    run_id: str | None = None
    benchmark: str = DEFAULT_BENCHMARK
    task_ids: tuple[str, ...] = ()
    limit: int | None = None
    attempts: int | None = None
    concurrency: int = 16
    agent_concurrency: int | None = None
    max_retries: int = 2
    timeout_multiplier: float = 1.0
    secret_env: tuple[str, ...] = ()
    agent_hosts: tuple[str, ...] = ()
    reasoning_effort: str | None = None
    agent_version: str | None = None
    harbor_bin: str = "harbor"

    @property
    def definition(self) -> BenchmarkDefinition:
        try:
            return BENCHMARKS[self.benchmark]
        except KeyError as exc:
            raise ValueError(f"unsupported benchmark: {self.benchmark!r}") from exc

    @property
    def effective_attempts(self) -> int:
        return self.attempts or self.definition.default_attempts

    def validate(self) -> None:
        if not self.agent.strip():
            raise ValueError("agent must not be empty")
        if not self.models or any(not model.strip() for model in self.models):
            raise ValueError("at least one non-empty model is required")
        if len(set(self.models)) != len(self.models):
            raise ValueError("models must be unique")
        if self.backend not in ISOLATED_BACKENDS:
            raise ValueError(f"unsupported or non-isolating backend: {self.backend}")
        definition = self.definition
        if any(not task_id.strip() for task_id in self.task_ids):
            raise ValueError("task ids must not be empty")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task ids must be unique")
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self.agent_concurrency is not None and not (1 <= self.agent_concurrency <= self.concurrency):
            raise ValueError("agent concurrency must be between 1 and total concurrency")
        if self.backend == "singularity" and (
            self.concurrency != 1 or self.agent_concurrency not in {None, 1}
        ):
            raise ValueError(
                "the local Singularity backend must run with concurrency=1 to "
                "prevent shared-host-network trial contamination"
            )
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be positive")
        if self.limit is not None and self.task_ids:
            raise ValueError("--limit and explicit task ids are mutually exclusive")
        if self.attempts is not None and self.attempts < 1:
            raise ValueError("attempts must be positive")
        if self.max_retries < 0:
            raise ValueError("max retries must not be negative")
        if self.timeout_multiplier <= 0:
            raise ValueError("timeout multiplier must be positive")
        if self.reasoning_effort is not None and not self.reasoning_effort.strip():
            raise ValueError("reasoning effort must not be empty")
        if self.agent_version is not None and not self.agent_version.strip():
            raise ValueError("agent version must not be empty")
        invalid_env = [name for name in self.secret_env if not _ENV_NAME_RE.fullmatch(name)]
        if invalid_env:
            raise ValueError(f"invalid environment variable names: {invalid_env}")
        if len(set(self.secret_env)) != len(self.secret_env):
            raise ValueError("secret environment variable names must be unique")
        missing_env = [name for name in self.secret_env if not os.environ.get(name)]
        if missing_env:
            raise ValueError(f"required secret environment variables are missing: {missing_env}")
        if definition.key == "terminal-bench-2.1" and self.limit is not None:
            if self.limit > definition.task_count:
                raise ValueError(
                    f"limit exceeds {definition.key} task count ({definition.task_count})"
                )


def _dataset_config(value: str, task_ids: tuple[str, ...], limit: int | None) -> dict:
    if "@" in value:
        name, ref = value.rsplit("@", 1)
    else:
        name, ref = value, ""
    result: dict[str, object] = {"name": name}
    if ref:
        result["ref" if "/" in name else "version"] = ref
    if task_ids:
        result["task_names"] = list(task_ids)
    if limit is not None:
        result["n_tasks"] = limit
    return result


def build_job_config(spec: HarborRunSpec, *, run_dir: Path, run_id: str) -> dict:
    spec.validate()
    env_templates = {name: "${" + name + "}" for name in spec.secret_env}
    agents = []
    for model in spec.models:
        agent: dict[str, object] = {
            "name": spec.agent,
            "model_name": model,
        }
        if spec.agent_concurrency is not None:
            agent["n_concurrent"] = spec.agent_concurrency
        if env_templates:
            agent["env"] = env_templates
        if spec.agent_hosts:
            agent["extra_allowed_hosts"] = list(spec.agent_hosts)
        agent_kwargs = {}
        if spec.reasoning_effort is not None:
            agent_kwargs["reasoning_effort"] = spec.reasoning_effort
        if spec.agent_version is not None:
            agent_kwargs["version"] = spec.agent_version
        if agent_kwargs:
            agent["kwargs"] = agent_kwargs
        agents.append(agent)
    environment: dict[str, object] = {
        "type": spec.backend,
        "force_build": False,
        "delete": True,
    }
    if spec.benchmark == "terminal-bench-2.1" and spec.backend == "daytona":
        environment["kwargs"] = {"snapshot_template_name": "{name}-tb-2-1"}
    elif spec.backend == "singularity":
        environment["kwargs"] = {
            "singularity_image_cache_dir": str(
                (run_dir.parent / ".image-cache" / "singularity").resolve()
            ),
            "singularity_force_pull": False,
        }
    return {
        "job_name": run_id,
        "jobs_dir": str((run_dir / "jobs").resolve()),
        "n_attempts": spec.effective_attempts,
        "timeout_multiplier": spec.timeout_multiplier,
        "n_concurrent_trials": spec.concurrency,
        "retry": {"max_retries": spec.max_retries},
        "environment": environment,
        "agents": agents,
        "datasets": [
            _dataset_config(spec.definition.dataset, spec.task_ids, spec.limit)
        ],
    }


def _harbor_version(executable: str) -> str:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def start_harbor_run(spec: HarborRunSpec, *, dry_run: bool = False) -> tuple[int, Path]:
    spec.validate()
    run_id = validate_run_id(spec.run_id) if spec.run_id else make_run_id("harbor")
    run_dir = create_run_dir(spec.output_root, run_id)
    executable = resolve_executable(spec.harbor_bin) or spec.harbor_bin
    config = build_job_config(spec, run_dir=run_dir, run_id=run_id)
    config_path = run_dir / "job-config.json"
    atomic_write_json(config_path, config)
    definition = spec.definition
    expected_tasks = len(spec.task_ids) if spec.task_ids else (spec.limit or definition.task_count)
    attempts = spec.effective_attempts
    full_dataset = not spec.task_ids and spec.limit is None
    leaderboard_trial_shape = (
        full_dataset
        and attempts >= definition.official_min_attempts
        and spec.timeout_multiplier == 1.0
        and bool(spec.reasoning_effort)
        and bool(spec.agent_version)
    )
    manifest = {
        "schema_version": 1,
        "framework_version": FRAMEWORK_VERSION,
        "kind": "harbor-agent-evaluation",
        "status": "prepared" if dry_run else "running",
        "run_id": run_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "benchmark": spec.benchmark,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "benchmark_source_url": definition.source_url,
        "benchmark_source_commit": definition.source_commit,
        "expected_tasks": expected_tasks,
        "attempts_per_task": attempts,
        "official_min_attempts": definition.official_min_attempts,
        "leaderboard_trial_shape": leaderboard_trial_shape,
        "upload_enabled": False,
        "expected_trials": expected_tasks * len(spec.models) * attempts,
        "agent": spec.agent,
        "models": list(spec.models),
        "reasoning_effort": spec.reasoning_effort,
        "agent_version": spec.agent_version,
        "backend": spec.backend,
        "local_execution": spec.backend == "singularity",
        "network_namespace_isolation": spec.backend != "singularity",
        "strict_cgroup_isolation": spec.backend != "singularity",
        "secret_env_names": list(spec.secret_env),
        "harbor_version": _harbor_version(executable),
        "harbor_source_commit": HARBOR_COMMIT,
        "project_revision": _git_revision(),
        "job_config": str(config_path),
    }
    manifest_path = run_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    if dry_run:
        return 0, run_dir

    command = [executable, "run", "--config", str(config_path), "--yes"]
    (run_dir / "command.json").write_text(
        json.dumps(command, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lock_path = run_dir / ".launcher.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code = run_streaming(
            command,
            cwd=run_dir,
            log_path=run_dir / "launcher.log",
            env=prepare_runtime_environment(spec.backend, run_dir),
        )
    manifest["status"] = "succeeded" if exit_code == 0 else "failed"
    manifest["exit_code"] = exit_code
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)
    if exit_code == 0:
        from .audit import audit_run

        audit = audit_run(run_dir)
        atomic_write_json(run_dir / "audit.json", audit)
        if not audit["ok"]:
            exit_code = 3
            manifest["status"] = "audit_failed"
            manifest["exit_code"] = exit_code
            manifest["updated_at"] = utc_now()
            atomic_write_json(manifest_path, manifest)
    return exit_code, run_dir


def resume_harbor_run(run_dir: Path, *, harbor_bin: str = "harbor") -> int:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "harbor-agent-evaluation":
        raise ValueError(f"not a Harbor evaluation run: {run_dir}")
    if manifest.get("status") in {"succeeded", "audit_failed"}:
        raise ValueError(
            f"run is already terminal with status {manifest.get('status')!r}; audit it instead"
        )
    backend = str(manifest.get("backend") or "")
    if backend not in ISOLATED_BACKENDS:
        raise ValueError(f"manifest contains unsupported backend: {backend!r}")
    missing_env = [
        str(name)
        for name in manifest.get("secret_env_names") or []
        if not os.environ.get(str(name))
    ]
    if missing_env:
        raise ValueError(f"required secret environment variables are missing: {missing_env}")
    from .preflight import harbor_checks, render_checks

    checks = harbor_checks(
        backend=backend,
        benchmark=str(manifest.get("benchmark") or DEFAULT_BENCHMARK),
        output_root=run_dir.parent,
        harbor_bin=harbor_bin,
    )
    if any(check.failed for check in checks):
        raise ValueError("resume preflight failed:\n" + render_checks(checks))
    job_dir = run_dir / "jobs" / str(manifest["run_id"])
    if not job_dir.is_dir():
        raise FileNotFoundError(f"Harbor job directory is missing: {job_dir}")
    executable = resolve_executable(harbor_bin) or harbor_bin
    command = [executable, "job", "resume", "--job-path", str(job_dir)]
    manifest["status"] = "running"
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)
    with (run_dir / ".launcher.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code = run_streaming(
            command,
            cwd=run_dir,
            log_path=run_dir / "launcher.log",
            env=prepare_runtime_environment(backend, run_dir),
        )
    manifest["status"] = "succeeded" if exit_code == 0 else "failed"
    manifest["exit_code"] = exit_code
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)
    if exit_code == 0:
        from .audit import audit_run

        audit = audit_run(run_dir)
        atomic_write_json(run_dir / "audit.json", audit)
        if not audit["ok"]:
            exit_code = 3
            manifest["status"] = "audit_failed"
            manifest["exit_code"] = exit_code
            manifest["updated_at"] = utc_now()
            atomic_write_json(manifest_path, manifest)
    return exit_code
