from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION
from evaluations.long_agent_release.codex_projection import (
    CodexProjectionError,
    project_codex_trial,
)
from evaluations.long_agent_release.tofu_projection import (
    TofuProjectionError,
    project_tofu_trial,
)

from .codex_kimi_runtime import (
    CODEX_KIMI_AGENT,
    CODEX_KIMI_RUNTIME_SCHEMA,
)
from .tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TOFU_KIMI_RUNTIME_SCHEMA,
    TofuKimiCandidateSettings,
)
from .rootless_qemu import rootless_sandbox_identity
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
    checks = [
        AuditCheck(
            "artifact_root_private",
            run_dir.is_dir() and run_dir.stat().st_mode & 0o077 == 0,
            f"mode={run_dir.stat().st_mode & 0o777:o}" if run_dir.exists() else "missing",
        )
    ]
    for name in (".gitignore", ".ignore"):
        path = run_dir / name
        protected = path.is_file() and path.read_text(encoding="utf-8", errors="replace").lstrip().startswith("*")
        checks.append(AuditCheck(f"artifact_{name}", protected, "self-ignoring" if protected else "missing wildcard ignore"))
    return checks


def _contains_bytes(path: Path, needle: bytes) -> bool:
    if not needle or not path.is_file() or path.is_symlink():
        return False
    overlap = b""
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                value = overlap + chunk
                if needle in value:
                    return True
                overlap = value[-max(0, len(needle) - 1):]
    except OSError:
        return False
    return False


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _codex_kimi_config_checks(
    run_dir: Path, manifest: dict[str, Any], config: dict[str, Any]
) -> list[AuditCheck]:
    """Prove the persisted formal-baseline shape without exposing secrets."""

    formal = manifest.get("agent") == CODEX_KIMI_AGENT
    runtime = manifest.get("codex_kimi_runtime")
    if not formal and runtime is None:
        return []
    checks: list[AuditCheck] = []
    runtime_valid = (
        formal
        and isinstance(runtime, dict)
        and runtime.get("schema") == CODEX_KIMI_RUNTIME_SCHEMA
        and runtime.get("agentImport") == CODEX_KIMI_AGENT
        and runtime.get("codexVersion") == CODEX_VERSION
        and runtime.get("providerFace") == manifest.get("provider_face")
        and runtime.get("providerSlotId") == manifest.get("provider_slot_id")
        and isinstance(runtime.get("providerFace"), str)
        and bool(runtime.get("providerFace"))
        and isinstance(runtime.get("providerSlotId"), str)
        and bool(runtime.get("providerSlotId"))
        and isinstance(runtime.get("agentTimeoutSeconds"), int)
        and 1 <= runtime.get("agentTimeoutSeconds") <= 86_400
        and runtime.get("listenHost") == "127.0.0.1"
        and runtime.get("guestHost") == "10.0.2.101"
        and runtime.get("requireTrialHeader") is True
        and runtime.get("credentialBoundary") == "launcher-host-only"
        and isinstance(runtime.get("codexSha256"), str)
        and len(runtime.get("codexSha256")) == 64
        and all(
            character in "0123456789abcdef"
            for character in runtime.get("codexSha256")
        )
    )
    checks.append(AuditCheck(
        "codex_kimi_runtime_manifest",
        runtime_valid,
        "pinned Codex 0.149.1 + loopback-only required-header proxy",
    ))
    if not isinstance(runtime, dict):
        return checks
    agents = config.get("agents") or []
    agent = agents[0] if isinstance(agents, list) and len(agents) == 1 \
        and isinstance(agents[0], dict) else {}
    kwargs = agent.get("kwargs") if isinstance(agent, dict) else {}
    expected_kwargs = {
        "codex_binary": runtime.get("codexBinary"),
        "codex_sha256": runtime.get("codexSha256"),
        "proxy_trial_metrics_dir": runtime.get("trialMetricsDir"),
        "proxy_service_name": runtime.get("guestServiceName"),
        "timeout_sec": runtime.get("agentTimeoutSeconds"),
        "reasoning_effort": manifest.get("reasoning_effort"),
        "version": CODEX_VERSION,
    }
    agent_valid = (
        agent.get("name") == CODEX_KIMI_AGENT
        and agent.get("model_name") == "kimi-k3"
        and kwargs == expected_kwargs
        and not agent.get("env")
        and not agent.get("extra_allowed_hosts")
        and manifest.get("models") == ["kimi-k3"]
        and manifest.get("secret_env_names") in ([], None)
    )
    checks.append(AuditCheck(
        "codex_kimi_agent_pin",
        agent_valid,
        "single kimi-k3 agent with exact binary/schema inputs and no guest env",
    ))
    environment = config.get("environment") or {}
    environment_kwargs = environment.get("kwargs") or {}
    routes = environment_kwargs.get("loopback_service_forwards") or []
    route = routes[0] if isinstance(routes, list) and len(routes) == 1 \
        and isinstance(routes[0], dict) else {}
    route_valid = (
        environment.get("import_path")
        == "rootless_vm.harbor_environment:RootlessQemuEnvironment"
        and route.get("name") == runtime.get("guestServiceName")
        and route.get("guest_host") == runtime.get("guestHost")
        and route.get("guest_port") == runtime.get("guestPort")
        and route.get("host_port") == runtime.get("listenPort")
    )
    checks.append(AuditCheck(
        "codex_kimi_single_control_route",
        route_valid,
        "one fixed guest endpoint relayed to one host-loopback port",
    ))
    expected_names = [
        runtime.get("upstreamBaseUrlEnv"), runtime.get("upstreamApiKeyEnv")
    ]
    boundary_valid = (
        all(isinstance(name, str) and name for name in expected_names)
        and manifest.get("host_only_secret_env_names") == expected_names
    )
    control_files = [
        run_dir / "manifest.json",
        run_dir / "job-config.json",
        run_dir / "command.json",
        run_dir / "launcher.log",
    ]
    leaked_names: list[str] = []
    scanned_names: list[str] = []
    if boundary_valid:
        import os

        for name in expected_names:
            value = os.environ.get(str(name), "").encode("utf-8")
            if value:
                scanned_names.append(str(name))
                if any(_contains_bytes(path, value) for path in control_files):
                    leaked_names.append(str(name))
    checks.append(AuditCheck(
        "codex_kimi_no_credential_persistence",
        boundary_valid and not leaked_names,
        "no guest credential fields; exact-value scans=" + str(scanned_names)
        if not leaked_names else f"leaked environment names={leaked_names}",
    ))
    metrics_root = Path(str(runtime.get("trialMetricsDir") or ""))
    metrics_owned = (
        metrics_root == run_dir / "codex-kimi-proxy" / "trials"
        and metrics_root.is_dir()
        and not metrics_root.is_symlink()
        and metrics_root.stat().st_mode & 0o077 == 0
    )
    checks.append(AuditCheck(
        "codex_kimi_private_metrics_repository",
        metrics_owned,
        "run-owned owner-only per-trial proxy shards",
    ))
    return checks


def _codex_kimi_trial_evidence_checks(
    manifest: dict[str, Any], trial_dirs: list[Path], expected: int
) -> list[AuditCheck]:
    if manifest.get("agent") != CODEX_KIMI_AGENT:
        return []
    runtime = manifest.get("codex_kimi_runtime") or {}
    expected_sha = str(runtime.get("codexSha256") or "")
    valid = 0
    tokens: set[str] = set()
    errors: list[str] = []
    for trial_dir in trial_dirs:
        agent_dir = trial_dir / "agent"
        evidence = agent_dir / "codex-kimi-evidence"
        raw = evidence / "codex-events.jsonl"
        metrics = evidence / "proxy-metrics.jsonl"
        trajectory = agent_dir / "trajectory.json"
        try:
            projection = project_codex_trial(
                raw_trajectory=raw,
                proxy_metrics=metrics,
            )
            atif = _load_json(trajectory)
            atif_agent = atif.get("agent")
            if not isinstance(atif_agent, dict):
                raise ValueError("ATIF agent metadata is invalid")
            extra = atif_agent.get("extra") or {}
            if not isinstance(extra, dict):
                raise ValueError("ATIF extra metadata is invalid")
            if extra.get("binary_sha256") != expected_sha \
                    or extra.get("trial_token") != projection["trialToken"]:
                raise ValueError("ATIF pin/token does not match raw evidence")
            if projection["trialToken"] in tokens:
                raise ValueError("duplicate trial token")
            tokens.add(projection["trialToken"])
            valid += 1
        except (OSError, ValueError, CodexProjectionError) as exc:
            if len(errors) < 5:
                errors.append(f"{trial_dir.name}:{type(exc).__name__}")
    return [AuditCheck(
        "codex_kimi_reconciled_trial_evidence",
        valid == expected and len(tokens) == expected,
        f"valid={valid}, unique_tokens={len(tokens)}, expected={expected}"
        + (f", errors={errors}" if errors else ""),
    )]


def _tofu_kimi_config_checks(
    run_dir: Path, manifest: dict[str, Any], config: dict[str, Any]
) -> list[AuditCheck]:
    """Prove the production candidate uses host-only exclusive authority."""

    formal = manifest.get("agent") == TOFU_KIMI_AGENT
    runtime = manifest.get("tofu_kimi_runtime")
    if not formal and runtime is None:
        return []
    parsed = None
    try:
        parsed = TofuKimiCandidateSettings.from_manifest_record(runtime)
    except (TypeError, ValueError):
        pass
    runtime_valid = (
        formal
        and parsed is not None
        and runtime.get("schema") == TOFU_KIMI_RUNTIME_SCHEMA
        and runtime.get("providerFace") == manifest.get("provider_face")
        and runtime.get("providerSlotId") == manifest.get("provider_slot_id")
        and runtime.get("experimentArm") == manifest.get("experiment_arm")
        and runtime.get("agentVersion") == manifest.get("agent_version")
        and runtime.get("credentialBoundary") == "harbor-host-only"
        and runtime.get("guestCredentialValues") is False
    )
    checks = [AuditCheck(
        "tofu_kimi_runtime_manifest",
        runtime_valid,
        "production AgentRuntime + frozen config digest + host-only provider",
    )]
    if parsed is None:
        return checks
    agents = config.get("agents") or []
    agent = agents[0] if isinstance(agents, list) and len(agents) == 1 \
        and isinstance(agents[0], dict) else {}
    expected_kwargs = {
        "reasoning_effort": manifest.get("reasoning_effort"),
        "version": parsed.agent_version,
        **parsed.agent_kwargs(),
    }
    agent_valid = (
        agent.get("name") == TOFU_KIMI_AGENT
        and agent.get("model_name") == "kimi-k3"
        and agent.get("kwargs") == expected_kwargs
        and not agent.get("env")
        and not agent.get("extra_allowed_hosts")
        and manifest.get("models") == ["kimi-k3"]
        and manifest.get("secret_env_names") in ([], None)
    )
    checks.append(AuditCheck(
        "tofu_kimi_production_agent_pin",
        agent_valid,
        "one production runtime, exact public config, no guest environment",
    ))
    environment = config.get("environment") or {}
    environment_kwargs = environment.get("kwargs") or {}
    routes = environment_kwargs.get("loopback_service_forwards") or []
    checks.append(AuditCheck(
        "tofu_kimi_no_guest_control_route",
        environment.get("import_path")
        == "rootless_vm.harbor_environment:RootlessQemuEnvironment"
        and routes == [],
        "guest receives only the Harbor exec channel, never provider routing",
    ))
    expected_names = list(parsed.credential_environment_names)
    boundary_valid = (
        manifest.get("host_only_secret_env_names") == expected_names
        and runtime.get("upstreamBaseUrlEnv") == expected_names[0]
        and runtime.get("upstreamApiKeyEnv") == expected_names[1]
    )
    control_files = [
        run_dir / "manifest.json", run_dir / "job-config.json",
        run_dir / "command.json", run_dir / "launcher.log",
    ]
    leaked: list[str] = []
    scanned: list[str] = []
    if boundary_valid:
        import os
        for name in expected_names:
            value = os.environ.get(name, "").encode("utf-8")
            if value:
                scanned.append(name)
                if any(_contains_bytes(path, value) for path in control_files):
                    leaked.append(name)
    checks.append(AuditCheck(
        "tofu_kimi_no_credential_persistence",
        boundary_valid and not leaked,
        "host-only exact-value scans=" + str(scanned)
        if not leaked else f"leaked environment names={leaked}",
    ))
    return checks


def _tofu_kimi_trial_evidence_checks(
    manifest: dict[str, Any], trial_dirs: list[Path], expected: int,
) -> list[AuditCheck]:
    """Reconcile every formal candidate trial and scan its private boundary."""

    if manifest.get("agent") != TOFU_KIMI_AGENT:
        return []
    runtime = manifest.get("tofu_kimi_runtime")
    try:
        settings = TofuKimiCandidateSettings.from_manifest_record(runtime)
    except (TypeError, ValueError):
        return [AuditCheck(
            "tofu_kimi_reconciled_trial_evidence", False,
            "formal runtime manifest is invalid",
        )]
    valid = 0
    task_ids: set[str] = set()
    errors: list[str] = []
    leaked_names: set[str] = set()
    import os

    secret_values = {
        name: os.environ.get(name, "").encode("utf-8")
        for name in settings.credential_environment_names
        if os.environ.get(name, "")
    }
    runtime_record = settings.manifest_record()
    for trial_dir in trial_dirs:
        agent_dir = trial_dir / "agent"
        evidence_dir = agent_dir / "tofu-kimi-evidence"
        native = evidence_dir / "events.jsonl"
        runtime_evidence = evidence_dir / "runtime-evidence.json"
        tool_audit = evidence_dir / "tool-audit.json"
        trajectory = agent_dir / "trajectory.json"
        artifact_paths = (native, runtime_evidence, tool_audit, trajectory)
        try:
            projection = project_tofu_trial(
                native_events=native,
                runtime_evidence=runtime_evidence,
                tool_audit=tool_audit,
                runtime_config=dict(settings.runtime_config),
                expected_runtime_config_digest=settings.runtime_config_sha256,
                expected_prompt_contract_digest=runtime_record[
                    "promptContractSha256"],
                expected_tool_schema_digest=runtime_record[
                    "toolSchemaSha256"],
            )
            runtime_value = _load_json(runtime_evidence)
            task_id = str(runtime_value.get("taskId") or "")
            if not task_id or task_id in task_ids:
                raise ValueError("runtime task identity is missing or duplicated")
            task_ids.add(task_id)
            atif = _load_json(trajectory)
            atif_agent = atif.get("agent")
            extra = atif_agent.get("extra") \
                if isinstance(atif_agent, dict) else None
            if not isinstance(atif_agent, dict) \
                    or atif_agent.get("name") != "tofu-kimi-runtime" \
                    or atif_agent.get("version") != settings.agent_version \
                    or atif_agent.get("model_name") != "kimi-k3" \
                    or not isinstance(extra, dict) \
                    or extra.get("runtime_config_sha256") \
                    != settings.runtime_config_sha256 \
                    or extra.get("prompt_contract_sha256") \
                    != runtime_record["promptContractSha256"] \
                    or extra.get("tool_schema_sha256") \
                    != runtime_record["toolSchemaSha256"]:
                raise ValueError("ATIF candidate binding drifted")
            steps = atif.get("steps")
            if not isinstance(steps, list) or not steps \
                    or not isinstance(steps[-1], dict) \
                    or steps[-1].get("message") != projection["finalOutput"]:
                raise ValueError("ATIF final output drifted")
            for name, secret in secret_values.items():
                if any(_contains_bytes(path, secret) for path in artifact_paths):
                    leaked_names.add(name)
            valid += 1
        except (OSError, ValueError, TofuProjectionError) as exc:
            if len(errors) < 5:
                errors.append(f"{trial_dir.name}:{type(exc).__name__}")
    return [
        AuditCheck(
            "tofu_kimi_reconciled_trial_evidence",
            valid == expected and len(task_ids) == expected,
            f"valid={valid}, unique_tasks={len(task_ids)}, expected={expected}"
            + (f", errors={errors}" if errors else ""),
        ),
        AuditCheck(
            "tofu_kimi_no_trial_credential_persistence",
            valid == expected and not leaked_names,
            "no host credential values in candidate evidence"
            if not leaked_names else f"leaked environment names={sorted(leaked_names)}",
        ),
    ]


def _audit_harbor(run_dir: Path, manifest: dict[str, Any], allow_errors: bool) -> list[AuditCheck]:
    checks = _artifact_checks(run_dir)
    config_path = Path(str(manifest.get("job_config") or run_dir / "job-config.json"))
    config = _load_json(config_path)
    checks.extend(_codex_kimi_config_checks(run_dir, manifest, config))
    checks.extend(_tofu_kimi_config_checks(run_dir, manifest, config))
    environment = config.get("environment") or {}
    import_path = environment.get("import_path")
    backend = environment.get("type")
    if import_path == "rootless_vm.harbor_environment:RootlessQemuEnvironment":
        backend = "rootless-qemu"
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
    elif backend == "rootless-qemu":
        kwargs = environment.get("kwargs") or {}
        runtime_shape = (
            isinstance(kwargs, dict)
            and bool(kwargs.get("base_disk"))
            and bool(kwargs.get("base_disk_sha256"))
            and bool(kwargs.get("image_store"))
            and bool(kwargs.get("state_root"))
            and bool(kwargs.get("prepared_cache_root"))
            and bool(kwargs.get("qemu_path"))
            and bool(kwargs.get("qemu_img_path"))
        )
        checks.append(
            AuditCheck(
                "rootless_qemu_runtime",
                runtime_shape,
                "digest-pinned base + image store + disposable VM state",
            )
        )
        if manifest.get("agent") == CODEX_KIMI_AGENT \
                or manifest.get("sandbox_identity") is not None:
            try:
                expected_sandbox = rootless_sandbox_identity(config)
            except (OSError, TypeError, ValueError):
                expected_sandbox = None
            checks.append(AuditCheck(
                "rootless_sandbox_identity",
                expected_sandbox is not None
                and manifest.get("sandbox_identity") == expected_sandbox,
                "base/QEMU hashes plus frozen resource and network controls",
            ))
        disclosed = (
            manifest.get("local_execution") is True
            and manifest.get("network_namespace_isolation") is True
            and manifest.get("strict_cgroup_isolation") is False
            and manifest.get("vm_isolation") is True
            and manifest.get("host_mounts") is False
        )
        checks.append(
            AuditCheck(
                "local_isolation_disclosed",
                disclosed,
                "QEMU/TCG VM + namespaces/seccomp; guest cgroups and host RLIMITs",
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
    formal_identity = manifest.get("agent") in {
        CODEX_KIMI_AGENT, TOFU_KIMI_AGENT,
    }
    if formal_identity or manifest.get("harness_identity") is not None:
        harbor_binary = Path(str(manifest.get("harbor_binary") or ""))
        expected_harbor_sha = str(manifest.get("harbor_binary_sha256") or "")
        harbor_binary_valid = False
        try:
            harbor_binary_valid = (
                not harbor_binary.is_symlink()
                and harbor_binary.is_file()
                and len(expected_harbor_sha) == 64
                and _file_sha256(harbor_binary) == expected_harbor_sha
            )
        except OSError:
            harbor_binary_valid = False
        checks.append(AuditCheck(
            "pinned_harbor_binary",
            harbor_binary_valid,
            f"path={harbor_binary}, sha256={expected_harbor_sha}",
        ))
        expected_harness_identity = {
            "name": "harbor",
            "version": manifest.get("harbor_version"),
            "binarySha256": expected_harbor_sha,
            "sourceCommit": HARBOR_COMMIT,
            "runnerFrameworkVersion": manifest.get("framework_version"),
            "runnerProjectRevision": manifest.get("project_revision"),
            "runnerProjectDirty": manifest.get("project_dirty"),
        }
        checks.append(AuditCheck(
            "formal_harness_identity",
            manifest.get("harness_identity") == expected_harness_identity,
            "version + executable hash + pinned source/runner revision",
        ))
    if formal_identity:
        checks.append(AuditCheck(
            "formal_clean_runner_revision",
            manifest.get("project_dirty") is False
            and manifest.get("project_revision") not in {None, "", "unknown"},
            "formal release evidence requires a clean pinned runner commit",
        ))
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
    checks.extend(
        _codex_kimi_trial_evidence_checks(manifest, trial_dirs, expected)
    )
    checks.extend(
        _tofu_kimi_trial_evidence_checks(manifest, trial_dirs, expected)
    )
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
