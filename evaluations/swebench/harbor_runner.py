from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION
from evaluations.codex_kimi_proxy.supervisor import (
    CodexKimiProxySupervisor,
    reserve_loopback_port,
)

from .artifacts import (
    atomic_write_json,
    create_run_dir,
    harden_artifact_tree,
    make_run_id,
    utc_now,
    validate_run_id,
)
from .constants import (
    BENCHMARKS,
    BenchmarkDefinition,
    DEFAULT_BENCHMARK,
    FRAMEWORK_VERSION,
    HARBOR_COMMIT,
    ISOLATED_BACKENDS,
)
from .process import prepare_runtime_environment, resolve_executable, run_streaming
from .rootless_qemu import RootlessQemuSettings, rootless_sandbox_identity
from .codex_kimi_runtime import (
    CODEX_KIMI_AGENT,
    CodexKimiBaselineSettings,
)
from .tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TofuKimiCandidateSettings,
)


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROOTLESS_CREDENTIAL_SAFE_AGENTS = {
    "rootless_vm.harbor_tofu_agent:TofuHostAgent",
    TOFU_KIMI_AGENT,
    CODEX_KIMI_AGENT,
    "oracle",
}


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
    rootless_qemu: RootlessQemuSettings | None = None
    codex_kimi: CodexKimiBaselineSettings | None = None
    tofu_kimi: TofuKimiCandidateSettings | None = None
    release_run_root: Path | None = None

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
        if self.backend == "rootless-qemu":
            if self.rootless_qemu is None:
                raise ValueError("rootless-qemu backend requires local runtime settings")
            if self.agent not in _ROOTLESS_CREDENTIAL_SAFE_AGENTS:
                raise ValueError(
                    "rootless-qemu accepts only credential-safe agents so provider "
                    "secrets cannot enter a public-network benchmark guest"
                )
        elif self.rootless_qemu is not None:
            raise ValueError("rootless QEMU settings require backend='rootless-qemu'")
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
        if self.agent == CODEX_KIMI_AGENT:
            if self.codex_kimi is None:
                raise ValueError("Codex Kimi baseline requires its pinned runtime settings")
            if self.backend != "rootless-qemu":
                raise ValueError("Codex Kimi baseline requires backend='rootless-qemu'")
            if self.models != ("kimi-k3",):
                raise ValueError("Codex Kimi baseline requires exactly model 'kimi-k3'")
            if self.agent_version != CODEX_VERSION:
                raise ValueError(
                    f"Codex Kimi baseline requires agent version {CODEX_VERSION}"
                )
            if self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError("Codex Kimi baseline requires an explicit thinking setting")
            if self.timeout_multiplier != 1.0:
                raise ValueError(
                    "Codex Kimi formal runs require timeout_multiplier=1.0"
                )
            if self.secret_env or self.agent_hosts:
                raise ValueError(
                    "Codex Kimi credentials and upstream routes are launcher-owned"
                )
            if self.max_retries != 0:
                raise ValueError(
                    "Codex Kimi formal runs disable Harbor's evidence-erasing "
                    "internal retries; preregister maxInfrastructureRetries=0"
                )
            assert self.rootless_qemu is not None
            if self.rootless_qemu.loopback_services:
                raise ValueError(
                    "Codex Kimi baseline reserves the only guest control-plane route"
                )
            self.codex_kimi.validate(verify_binary=False)
            if self.tofu_kimi is not None:
                raise ValueError(
                    "Codex and Tofu Kimi runtime settings are mutually exclusive")
        elif self.agent == TOFU_KIMI_AGENT:
            if self.tofu_kimi is None:
                raise ValueError(
                    "Tofu Kimi candidate requires its frozen runtime settings")
            if self.codex_kimi is not None:
                raise ValueError(
                    "Codex and Tofu Kimi runtime settings are mutually exclusive")
            if self.backend != "rootless-qemu":
                raise ValueError(
                    "Tofu Kimi candidate requires backend='rootless-qemu'")
            if self.models != ("kimi-k3",):
                raise ValueError(
                    "Tofu Kimi candidate requires exactly model 'kimi-k3'")
            if self.agent_version != self.tofu_kimi.agent_version:
                raise ValueError(
                    "Tofu Kimi agent version differs from frozen settings")
            if self.reasoning_effort not in {
                "low", "medium", "high", "xhigh", "max", "ultra",
            }:
                raise ValueError(
                    "Tofu Kimi candidate requires an explicit thinking setting")
            if self.timeout_multiplier != 1.0:
                raise ValueError(
                    "Tofu Kimi formal runs require timeout_multiplier=1.0")
            if self.secret_env or self.agent_hosts:
                raise ValueError(
                    "Tofu Kimi credentials and upstream routes are launcher-owned")
            if self.max_retries != 0:
                raise ValueError(
                    "Tofu Kimi formal runs disable Harbor's evidence-erasing "
                    "internal retries; preregister maxInfrastructureRetries=0")
            assert self.rootless_qemu is not None
            if self.rootless_qemu.loopback_services:
                raise ValueError(
                    "Tofu Kimi candidate forbids guest control-plane services")
            self.tofu_kimi.validate()
        elif self.codex_kimi is not None:
            raise ValueError("Codex Kimi runtime settings require the formal agent import")
        elif self.tofu_kimi is not None:
            raise ValueError("Tofu Kimi runtime settings require the formal agent import")
        elif self.release_run_root is not None:
            raise ValueError(
                "release attempt tracking requires codex-kimi or tofu-kimi")
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


def build_job_config(
    spec: HarborRunSpec,
    *,
    run_dir: Path,
    run_id: str,
    codex_proxy_host_port: int | None = None,
) -> dict:
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
        if spec.codex_kimi is not None:
            agent_kwargs.update(spec.codex_kimi.agent_kwargs(run_dir))
        if spec.tofu_kimi is not None:
            agent_kwargs.update(spec.tofu_kimi.agent_kwargs())
        if agent_kwargs:
            agent["kwargs"] = agent_kwargs
        agents.append(agent)
    if spec.backend == "rootless-qemu":
        assert spec.rootless_qemu is not None
        rootless_settings = spec.rootless_qemu
        if spec.codex_kimi is not None:
            if codex_proxy_host_port is None:
                raise ValueError("Codex Kimi job config requires a bound proxy port")
            rootless_settings = replace(
                rootless_settings,
                loopback_services=(
                    spec.codex_kimi.service_forward(
                        host_port=int(codex_proxy_host_port)
                    ),
                ),
            )
        environment: dict[str, object] = {
            "import_path": (
                "rootless_vm.harbor_environment:RootlessQemuEnvironment"
            ),
            "force_build": False,
            "delete": True,
            "kwargs": rootless_settings.environment_kwargs(
                spec.definition,
                run_dir=run_dir,
                required_tasks=spec.task_ids,
            ),
        }
    else:
        environment = {
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


def _child_environment_without(names: tuple[str, ...]) -> dict[str, str]:
    environment = os.environ.copy()
    for name in names:
        environment.pop(name, None)
    return environment


def _harbor_version(executable: str, *, unset_env: tuple[str, ...] = ()) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            env=_child_environment_without(unset_env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def _git_revision(*, unset_env: tuple[str, ...] = ()) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_child_environment_without(unset_env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty(*, unset_env: tuple[str, ...] = ()) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=20,
            env=_child_environment_without(unset_env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _executable_identity(executable: str) -> tuple[str, str]:
    path = Path(executable).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Harbor executable is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return str(path), digest.hexdigest()


def start_harbor_run(spec: HarborRunSpec, *, dry_run: bool = False) -> tuple[int, Path]:
    spec.validate()
    credential_exclusions = (
        spec.codex_kimi.child_environment_exclusions
        if spec.codex_kimi is not None else (
            spec.tofu_kimi.credential_environment_names
            if spec.tofu_kimi is not None else ()
        )
    )
    if spec.codex_kimi is not None:
        spec.codex_kimi.validate(verify_binary=True)
        if not dry_run:
            # Fail before creating a run when the launcher-owned secret inputs
            # are unavailable. Values remain in this process only.
            spec.codex_kimi.credentials_from_environment()
    if spec.tofu_kimi is not None:
        spec.tofu_kimi.validate()
        if not dry_run:
            spec.tofu_kimi.credentials_from_environment()
    project_revision = _git_revision(unset_env=credential_exclusions)
    project_dirty = _git_dirty(unset_env=credential_exclusions)
    if (spec.codex_kimi is not None or spec.tofu_kimi is not None) \
            and not dry_run and (
        project_dirty is not False
        or project_revision in {"", "unknown"}
    ):
        raise ValueError(
            "formal Kimi release execution requires a clean pinned "
            "runner revision before any paid trial starts"
        )
    run_id = validate_run_id(spec.run_id) if spec.run_id else make_run_id("harbor")
    run_dir = create_run_dir(spec.output_root, run_id)
    resolved_executable = resolve_executable(spec.harbor_bin)
    executable = resolved_executable or spec.harbor_bin
    if resolved_executable is None and dry_run \
            and spec.codex_kimi is None and spec.tofu_kimi is None:
        harbor_binary, harbor_binary_sha256 = "unresolved", ""
    else:
        harbor_binary, harbor_binary_sha256 = _executable_identity(executable)
    proxy: CodexKimiProxySupervisor | None = None
    proxy_port: int | None = None
    dispatch_started = False
    manifest: dict | None = None
    manifest_path: Path | None = None
    if spec.codex_kimi is not None:
        if dry_run:
            proxy_port = reserve_loopback_port()
        else:
            proxy = CodexKimiProxySupervisor(
                spec.codex_kimi.proxy_config(run_dir), port=0
            ).start()
            proxy_port = proxy.port
    try:
        config = build_job_config(
            spec,
            run_dir=run_dir,
            run_id=run_id,
            codex_proxy_host_port=proxy_port,
        )
        config_path = run_dir / "job-config.json"
        atomic_write_json(config_path, config)
        definition = spec.definition
        expected_tasks = (
            len(spec.task_ids)
            if spec.task_ids
            else (spec.limit or definition.task_count)
        )
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
            "local_execution": spec.backend in {"rootless-qemu", "singularity"},
            "network_namespace_isolation": spec.backend != "singularity",
            "strict_cgroup_isolation": spec.backend not in {
                "rootless-qemu", "singularity"
            },
            "vm_isolation": spec.backend == "rootless-qemu",
            "host_mounts": False,
            "secret_env_names": list(spec.secret_env),
            "host_only_secret_env_names": (
                list(spec.codex_kimi.child_environment_exclusions)
                if spec.codex_kimi is not None else (
                    list(spec.tofu_kimi.credential_environment_names)
                    if spec.tofu_kimi is not None else []
                )
            ),
            "harbor_version": _harbor_version(
                executable,
                unset_env=credential_exclusions,
            ),
            "harbor_binary": harbor_binary,
            "harbor_binary_sha256": harbor_binary_sha256,
            "harbor_source_commit": HARBOR_COMMIT,
            "project_revision": project_revision,
            "project_dirty": project_dirty,
            "job_config": str(config_path),
        }
        manifest["harness_identity"] = {
            "name": "harbor",
            "version": manifest["harbor_version"],
            "binarySha256": harbor_binary_sha256,
            "sourceCommit": HARBOR_COMMIT,
            "runnerFrameworkVersion": FRAMEWORK_VERSION,
            "runnerProjectRevision": manifest["project_revision"],
            "runnerProjectDirty": manifest["project_dirty"],
        }
        if spec.backend == "rootless-qemu":
            manifest["sandbox_identity"] = rootless_sandbox_identity(config)
        if spec.codex_kimi is not None:
            assert proxy_port is not None
            manifest["provider_face"] = spec.codex_kimi.provider_face
            manifest["provider_slot_id"] = spec.codex_kimi.provider_slot_id
            manifest["codex_kimi_runtime"] = spec.codex_kimi.manifest_record(
                run_dir, host_port=proxy_port
            )
        if spec.tofu_kimi is not None:
            manifest["provider_face"] = spec.tofu_kimi.provider_face
            manifest["provider_slot_id"] = spec.tofu_kimi.provider_slot_id
            manifest["experiment_arm"] = spec.tofu_kimi.experiment_arm
            manifest["tofu_kimi_runtime"] = (
                spec.tofu_kimi.manifest_record())
        manifest_path = run_dir / "manifest.json"
        manifest["release_evidence_eligible"] = False
        if spec.release_run_root is not None and not dry_run:
            manifest["status"] = "claiming_release_attempts"
            atomic_write_json(manifest_path, manifest)
            if spec.codex_kimi is not None:
                from evaluations.long_agent_release.harbor_tracking import (
                    claim_codex_harbor_release_attempts,
                )
                tracking = claim_codex_harbor_release_attempts(
                    release_run_root=spec.release_run_root,
                    harbor_manifest=manifest,
                    job_config=config,
                )
            else:
                from evaluations.long_agent_release.harbor_tracking import (
                    claim_tofu_harbor_release_attempts,
                )
                tracking = claim_tofu_harbor_release_attempts(
                    release_run_root=spec.release_run_root,
                    harbor_manifest=manifest,
                    job_config=config,
                )
            manifest["release_attempt_tracking"] = tracking
            manifest["release_evidence_eligible"] = True
            manifest["status"] = "running"
        atomic_write_json(manifest_path, manifest)
        if dry_run:
            return 0, run_dir

        command = [executable, "run", "--config", str(config_path), "--yes"]
        atomic_write_json(run_dir / "command.json", command)
        lock_path = run_dir / ".launcher.lock"
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if proxy is not None:
                proxy.assert_healthy()
            dispatch_started = True
            exit_code = run_streaming(
                command,
                cwd=run_dir,
                log_path=run_dir / "launcher.log",
                env=prepare_runtime_environment(spec.backend, run_dir),
                unset_env=(
                    spec.codex_kimi.child_environment_exclusions
                    if spec.codex_kimi is not None else ()
                ),
            )
            if proxy is not None:
                proxy.assert_healthy()
    except BaseException as exc:
        if manifest is not None \
                and manifest.get("release_attempt_tracking") is not None \
                and spec.release_run_root is not None \
                and not dispatch_started:
            try:
                from evaluations.long_agent_release.run_store import (
                    fail_release_execution_before_dispatch,
                )

                closed = fail_release_execution_before_dispatch(
                    spec.release_run_root, execution_id=run_id,
                    code="harbor_launcher_pre_dispatch",
                )
                manifest["release_attempts_closed"] = len(closed["closed"])
            except (OSError, TypeError, ValueError) as close_exc:
                manifest["release_attempt_close_failure_type"] = type(
                    close_exc).__name__
        if manifest is not None and manifest_path is not None:
            manifest["status"] = "launcher_failed"
            manifest["exit_code"] = None
            manifest["failure_type"] = type(exc).__name__
            manifest["updated_at"] = utc_now()
            try:
                atomic_write_json(manifest_path, manifest)
            except OSError:
                pass
        raise
    finally:
        if proxy is not None:
            proxy.stop()
    harden_artifact_tree(run_dir)
    assert manifest is not None and manifest_path is not None
    manifest.pop("failure_type", None)
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


def _validated_resume_codex_runtime(
    *,
    run_dir: Path,
    manifest: dict,
    config: dict,
    rootless_qemu: RootlessQemuSettings | None,
) -> tuple[CodexKimiBaselineSettings, int] | None:
    value = manifest.get("codex_kimi_runtime")
    formal_agent = manifest.get("agent") == CODEX_KIMI_AGENT
    if value is None:
        if formal_agent:
            raise ValueError("formal Codex Kimi run is missing its runtime manifest")
        return None
    if not formal_agent or manifest.get("backend") != "rootless-qemu" \
            or manifest.get("models") != ["kimi-k3"] \
            or manifest.get("agent_version") != CODEX_VERSION:
        raise ValueError("Codex Kimi runtime is attached to an incompatible run")
    if rootless_qemu is None:
        raise ValueError("Codex Kimi runtime requires rootless QEMU settings")
    settings, host_port = CodexKimiBaselineSettings.from_manifest_record(
        value, run_dir=run_dir
    )
    if manifest.get("provider_face") != settings.provider_face \
            or manifest.get("provider_slot_id") != settings.provider_slot_id:
        raise ValueError("Codex Kimi provider/slot binding drifted")
    expected_service = settings.service_forward(host_port=host_port)
    if rootless_qemu.loopback_services != (expected_service,):
        raise ValueError("Codex Kimi guest control-plane route drifted")
    expected_kwargs: dict[str, object] = {
        "reasoning_effort": manifest.get("reasoning_effort"),
        "version": CODEX_VERSION,
        **settings.agent_kwargs(run_dir),
    }
    agents = config.get("agents") if isinstance(config, dict) else None
    valid_agent = (
        isinstance(agents, list)
        and len(agents) == 1
        and isinstance(agents[0], dict)
        and agents[0].get("name") == CODEX_KIMI_AGENT
        and agents[0].get("model_name") == "kimi-k3"
        and agents[0].get("kwargs") == expected_kwargs
        and not agents[0].get("env")
        and not agents[0].get("extra_allowed_hosts")
    )
    if not valid_agent:
        raise ValueError("Codex Kimi agent config drifted from the formal baseline")
    if manifest.get("secret_env_names") not in ([], None) \
            or manifest.get("host_only_secret_env_names") \
            != list(settings.child_environment_exclusions):
        raise ValueError("Codex Kimi credential-boundary metadata drifted")
    upstream, api_key = settings.credentials_from_environment()
    serialized = json.dumps(
        {"manifest": manifest, "config": config},
        ensure_ascii=False,
        sort_keys=True,
    )
    if upstream in serialized or api_key in serialized:
        raise ValueError("Codex Kimi credential value leaked into persisted config")
    return settings, host_port


def _validated_resume_tofu_runtime(
    *,
    manifest: dict,
    config: dict,
    rootless_qemu: RootlessQemuSettings | None,
) -> TofuKimiCandidateSettings | None:
    value = manifest.get("tofu_kimi_runtime")
    formal_agent = manifest.get("agent") == TOFU_KIMI_AGENT
    if value is None:
        if formal_agent:
            raise ValueError("formal Tofu Kimi run is missing its runtime manifest")
        return None
    if not formal_agent or manifest.get("backend") != "rootless-qemu" \
            or manifest.get("models") != ["kimi-k3"]:
        raise ValueError("Tofu Kimi runtime is attached to an incompatible run")
    if rootless_qemu is None:
        raise ValueError("Tofu Kimi runtime requires rootless QEMU settings")
    if rootless_qemu.loopback_services:
        raise ValueError("Tofu Kimi guest must not receive control-plane services")
    settings = TofuKimiCandidateSettings.from_manifest_record(value)
    if manifest.get("agent_version") != settings.agent_version \
            or manifest.get("provider_face") != settings.provider_face \
            or manifest.get("provider_slot_id") != settings.provider_slot_id \
            or manifest.get("experiment_arm") != settings.experiment_arm:
        raise ValueError("Tofu Kimi frozen runtime binding drifted")
    expected_kwargs: dict[str, object] = {
        "reasoning_effort": manifest.get("reasoning_effort"),
        "version": settings.agent_version,
        **settings.agent_kwargs(),
    }
    agents = config.get("agents") if isinstance(config, dict) else None
    valid_agent = (
        isinstance(agents, list)
        and len(agents) == 1
        and isinstance(agents[0], dict)
        and agents[0].get("name") == TOFU_KIMI_AGENT
        and agents[0].get("model_name") == "kimi-k3"
        and agents[0].get("kwargs") == expected_kwargs
        and not agents[0].get("env")
        and not agents[0].get("extra_allowed_hosts")
    )
    if not valid_agent:
        raise ValueError("Tofu Kimi agent config drifted from the formal candidate")
    if manifest.get("secret_env_names") not in ([], None) \
            or manifest.get("host_only_secret_env_names") \
            != list(settings.credential_environment_names):
        raise ValueError("Tofu Kimi credential-boundary metadata drifted")
    upstream, api_key = settings.credentials_from_environment()
    serialized = json.dumps(
        {"manifest": manifest, "config": config},
        ensure_ascii=False,
        sort_keys=True,
    )
    if upstream in serialized or api_key in serialized:
        raise ValueError("Tofu Kimi credential value leaked into persisted config")
    return settings


def resume_harbor_run(
    run_dir: Path, *, harbor_bin: str = "harbor",
    release_run_root: Path | None = None,
) -> int:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("run manifest must not be a symbolic link")
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

    rootless_qemu = None
    config: dict = {}
    if backend == "rootless-qemu":
        config_path = Path(
            str(manifest.get("job_config") or run_dir / "job-config.json")
        )
        if manifest.get("agent") in {CODEX_KIMI_AGENT, TOFU_KIMI_AGENT} and (
            config_path.is_symlink()
            or config_path.expanduser().resolve() != (run_dir / "job-config.json").resolve()
        ):
            raise ValueError("formal Kimi job config must be run-owned")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        environment = config.get("environment") or {}
        if environment.get("import_path") != (
            "rootless_vm.harbor_environment:RootlessQemuEnvironment"
        ):
            raise ValueError("manifest rootless backend does not match job environment")
        rootless_qemu = RootlessQemuSettings.from_environment_kwargs(
            environment.get("kwargs") or {}
        )
    release_tracking = manifest.get("release_attempt_tracking")
    if release_tracking is not None:
        if not isinstance(release_tracking, dict) or release_run_root is None:
            raise ValueError(
                "tracked formal Harbor resume requires --release-run-root")
        if manifest.get("agent") == CODEX_KIMI_AGENT:
            from evaluations.long_agent_release.harbor_tracking import (
                validate_codex_harbor_release_attempts,
            )
            validate_codex_harbor_release_attempts(
                release_run_root=release_run_root,
                tracking=release_tracking,
                harbor_manifest=manifest,
                job_config=config,
            )
        elif manifest.get("agent") == TOFU_KIMI_AGENT:
            from evaluations.long_agent_release.harbor_tracking import (
                validate_tofu_harbor_release_attempts,
            )
            validate_tofu_harbor_release_attempts(
                release_run_root=release_run_root,
                tracking=release_tracking,
                harbor_manifest=manifest,
                job_config=config,
            )
        else:
            raise ValueError("tracked Harbor run has no formal runtime")
    elif release_run_root is not None:
        raise ValueError("untracked Harbor runs cannot acquire claims during resume")
    codex_runtime = _validated_resume_codex_runtime(
        run_dir=run_dir,
        manifest=manifest,
        config=config,
        rootless_qemu=rootless_qemu,
    )
    tofu_runtime = _validated_resume_tofu_runtime(
        manifest=manifest,
        config=config,
        rootless_qemu=rootless_qemu,
    )
    if codex_runtime is not None or tofu_runtime is not None:
        exclusions = (
            codex_runtime[0].child_environment_exclusions
            if codex_runtime is not None
            else tofu_runtime.credential_environment_names
        )
        if manifest.get("project_dirty") is not False \
                or _git_dirty(unset_env=exclusions) is not False \
                or _git_revision(unset_env=exclusions) \
                != manifest.get("project_revision"):
            raise ValueError(
                "formal Kimi resume requires the same clean runner revision"
            )
    checks = harbor_checks(
        backend=backend,
        benchmark=str(manifest.get("benchmark") or DEFAULT_BENCHMARK),
        output_root=run_dir.parent,
        harbor_bin=harbor_bin,
        rootless_qemu=rootless_qemu,
        required_tasks=tuple(
            str(task)
            for dataset in config.get("datasets", [])
            if isinstance(dataset, dict)
            for task in dataset.get("task_names", [])
        ),
    )
    if any(check.failed for check in checks):
        raise ValueError("resume preflight failed:\n" + render_checks(checks))
    job_dir = run_dir / "jobs" / str(manifest["run_id"])
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise FileNotFoundError(f"Harbor job directory is missing: {job_dir}")
    executable = resolve_executable(harbor_bin) or harbor_bin
    harbor_binary, harbor_binary_sha256 = _executable_identity(executable)
    if manifest.get("agent") in {CODEX_KIMI_AGENT, TOFU_KIMI_AGENT} and (
        manifest.get("harbor_binary") != harbor_binary
        or manifest.get("harbor_binary_sha256") != harbor_binary_sha256
    ):
        raise ValueError("formal Kimi Harbor executable identity drifted")
    command = [executable, "job", "resume", "--job-path", str(job_dir)]
    with (run_dir / ".launcher.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proxy: CodexKimiProxySupervisor | None = None
        exclusions: tuple[str, ...] = ()
        dispatch_started = False
        try:
            if codex_runtime is not None:
                settings, host_port = codex_runtime
                proxy = CodexKimiProxySupervisor(
                    settings.proxy_config(run_dir), port=host_port
                ).start()
                exclusions = settings.child_environment_exclusions
            manifest.pop("failure_type", None)
            manifest["status"] = "running"
            manifest["updated_at"] = utc_now()
            atomic_write_json(manifest_path, manifest)
            dispatch_started = True
            exit_code = run_streaming(
                command,
                cwd=run_dir,
                log_path=run_dir / "launcher.log",
                env=prepare_runtime_environment(backend, run_dir),
                unset_env=exclusions,
            )
            if proxy is not None:
                proxy.assert_healthy()
        except BaseException as exc:
            if release_tracking is not None \
                    and release_run_root is not None \
                    and not dispatch_started:
                try:
                    from evaluations.long_agent_release.run_store import (
                        fail_release_execution_before_dispatch,
                    )

                    closed = fail_release_execution_before_dispatch(
                        release_run_root,
                        execution_id=str(release_tracking["executionId"]),
                        code="harbor_resume_pre_dispatch",
                    )
                    manifest["release_attempts_closed"] = len(closed["closed"])
                except (OSError, TypeError, ValueError) as close_exc:
                    manifest["release_attempt_close_failure_type"] = type(
                        close_exc).__name__
            manifest["status"] = "resume_failed"
            manifest["exit_code"] = None
            manifest["failure_type"] = type(exc).__name__
            manifest["updated_at"] = utc_now()
            try:
                atomic_write_json(manifest_path, manifest)
            except OSError:
                pass
            raise
        finally:
            if proxy is not None:
                proxy.stop()
    harden_artifact_tree(run_dir)
    manifest.pop("failure_type", None)
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
