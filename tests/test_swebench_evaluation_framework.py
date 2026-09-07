from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evaluations.swebench import images, preflight
from evaluations.swebench.artifacts import (
    create_run_dir,
    harden_artifact_tree,
    output_guard_status,
    prepare_output_root,
    validate_run_id,
)
from evaluations.swebench.audit import audit_run
from evaluations.swebench.constants import (
    BENCHMARKS,
    BenchmarkDefinition,
    swebench_verified_task_digests,
    terminal_bench_21_task_digests,
)
from evaluations.swebench.cli import build_parser
from evaluations.swebench.harbor_runner import (
    HarborRunSpec,
    build_job_config,
    start_harbor_run,
)
from evaluations.swebench.images import load_definitions
from evaluations.swebench.official import (
    group_predictions,
    load_predictions,
    normalized_predictions_sha256,
)
from evaluations.swebench.process import prepare_runtime_environment
from evaluations.swebench.rootless_qemu import (
    RootlessQemuSettings,
    load_image_store_index,
)
from lib.project_mod.config import IGNORE_DIRS


pytestmark = pytest.mark.unit


def test_artifact_root_is_private_and_self_ignoring(tmp_path):
    root = prepare_output_root(tmp_path / "evals")
    run_dir = create_run_dir(root, "safe-run")

    assert (root / ".gitignore").read_text().startswith("*")
    assert (root / ".ignore").read_text().startswith("*")
    assert (run_dir / ".gitignore").read_text().startswith("*")
    assert root.stat().st_mode & 0o077 == 0


def test_artifact_hardening_preserves_user_bits_and_skips_symlinks(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    nested = root / "nested"
    nested.mkdir(mode=0o755)
    artifact = nested / "result.json"
    artifact.write_text("{}")
    artifact.chmod(0o644)
    external = tmp_path / "external"
    external.write_text("keep")
    external.chmod(0o644)
    (nested / "external-link").symlink_to(external)

    harden_artifact_tree(root)

    assert nested.stat().st_mode & 0o777 == 0o700
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert external.stat().st_mode & 0o777 == 0o644


def test_project_scanner_excludes_all_evaluation_artifact_directories():
    assert {
        ".eval-runs",
        "eval-runs",
        "evaluation_results",
        "sb-cli-reports",
        "jobs",
        "trials",
    } <= IGNORE_DIRS


def test_artifact_root_refuses_to_adopt_nonempty_directory(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "user-file").write_text("keep me")

    with pytest.raises(ValueError, match="non-empty"):
        prepare_output_root(occupied)


def test_output_guard_rejects_arbitrary_project_subdirectory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    assert output_guard_status(project / ".eval-runs", project)[0]
    assert output_guard_status(project / "eval-runs", project)[0]
    assert not output_guard_status(project / "ordinary-source", project)[0]
    assert output_guard_status(tmp_path / "external", project)[0]


def test_run_id_validation_rejects_paths():
    assert validate_run_id("model-a_2026.08") == "model-a_2026.08"
    with pytest.raises(ValueError):
        validate_run_id("../escape")


def test_harbor_config_creates_one_agent_config_per_model_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-never-be-persisted")
    spec = HarborRunSpec(
        agent="codex",
        models=("openai/model-a", "openai/model-b"),
        backend="modal",
        output_root=tmp_path,
        task_ids=("django__django-11099",),
        concurrency=8,
        agent_concurrency=2,
        secret_env=("OPENAI_API_KEY",),
    )

    config = build_job_config(spec, run_dir=tmp_path / "run", run_id="run")
    rendered = json.dumps(config)

    assert [agent["model_name"] for agent in config["agents"]] == [
        "openai/model-a",
        "openai/model-b",
    ]
    assert all(agent["n_concurrent"] == 2 for agent in config["agents"])
    assert all(agent["env"] == {"OPENAI_API_KEY": "${OPENAI_API_KEY}"} for agent in config["agents"])
    assert "should-never-be-persisted" not in rendered
    assert config["environment"] == {
        "type": "modal",
        "force_build": False,
        "delete": True,
    }
    assert config["datasets"][0]["task_names"] == ["django__django-11099"]


def test_harbor_spec_rejects_duplicate_models_and_mixed_filters(tmp_path):
    duplicate = HarborRunSpec(
        agent="codex",
        models=("same", "same"),
        backend="modal",
        output_root=tmp_path,
    )
    with pytest.raises(ValueError, match="unique"):
        duplicate.validate()

    mixed = HarborRunSpec(
        agent="codex",
        models=("one",),
        backend="modal",
        output_root=tmp_path,
        task_ids=("task",),
        limit=1,
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        mixed.validate()

    unsafe = HarborRunSpec(
        agent="codex",
        models=("one",),
        backend="local",
        output_root=tmp_path,
    )
    with pytest.raises(ValueError, match="non-isolating"):
        unsafe.validate()

    drifting = HarborRunSpec(
        agent="codex",
        models=("one",),
        backend="modal",
        output_root=tmp_path,
        benchmark="swebench-verified@latest",
    )
    with pytest.raises(ValueError, match="unsupported benchmark"):
        drifting.validate()

    parallel_local = HarborRunSpec(
        agent="codex",
        models=("one",),
        backend="singularity",
        output_root=tmp_path,
        concurrency=2,
    )
    with pytest.raises(ValueError, match="concurrency=1"):
        parallel_local.validate()


def test_singularity_config_is_local_serial_and_reuses_immutable_images(tmp_path):
    spec = HarborRunSpec(
        agent="codex",
        models=("openai/model",),
        backend="singularity",
        output_root=tmp_path,
        concurrency=1,
    )

    config = build_job_config(spec, run_dir=tmp_path / "run", run_id="local")

    assert config["n_concurrent_trials"] == 1
    assert config["environment"] == {
        "type": "singularity",
        "force_build": False,
        "delete": True,
        "kwargs": {
            "singularity_image_cache_dir": str(
                (tmp_path / ".image-cache" / "singularity").resolve()
            ),
            "singularity_force_pull": False,
        },
    }

    _, run_dir = start_harbor_run(spec, dry_run=True)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["local_execution"] is True
    assert manifest["network_namespace_isolation"] is False
    assert manifest["strict_cgroup_isolation"] is False


def test_rootless_qemu_config_uses_custom_vm_environment_and_host_agent(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qemu = runtime / "qemu-system-x86_64"
    qemu_img = runtime / "qemu-img"
    for executable in (qemu, qemu_img):
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"trusted-base")
    store = tmp_path / "images"
    store.mkdir(mode=0o700)
    definition = BENCHMARKS["swebench-verified"]
    (store / "index.json").write_text(json.dumps({
        "schema": 1,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": definition.task_count,
        "images": {
            f"image-{index}": {
                "tasks": [
                    "swe-bench/django__django-11099"
                    if index == 0
                    else f"swe-bench/fixture-{index}"
                ]
            }
            for index in range(definition.task_count)
        },
    }))
    settings = RootlessQemuSettings(
        base_disk=base,
        image_store=store,
        qemu_path=qemu,
        qemu_img_path=qemu_img,
    )
    spec = HarborRunSpec(
        agent="rootless_vm.harbor_tofu_agent:TofuHostAgent",
        models=("deepseek-v4-flash-meituan",),
        backend="rootless-qemu",
        output_root=tmp_path / "evals",
        task_ids=("swe-bench/django__django-11099",),
        concurrency=4,
        agent_concurrency=2,
        rootless_qemu=settings,
    )

    _, run_dir = start_harbor_run(spec, dry_run=True)
    config = json.loads((run_dir / "job-config.json").read_text())
    environment = config["environment"]
    assert environment["import_path"] == (
        "rootless_vm.harbor_environment:RootlessQemuEnvironment"
    )
    assert environment["delete"] is True
    assert environment["kwargs"]["image_store"] == str(store.resolve())
    assert environment["kwargs"]["base_disk_sha256"]
    assert config["agents"][0]["n_concurrent"] == 2
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["local_execution"] is True
    assert manifest["network_namespace_isolation"] is True
    assert manifest["strict_cgroup_isolation"] is False
    assert manifest["vm_isolation"] is True
    assert manifest["host_mounts"] is False

    unsafe_agent = HarborRunSpec(
        agent="codex",
        models=("model",),
        backend="rootless-qemu",
        output_root=tmp_path,
        rootless_qemu=settings,
    )
    with pytest.raises(ValueError, match="credential-safe agents"):
        unsafe_agent.validate()


def test_rootless_definition_cache_is_pinned_and_parses_dockerfile_base(tmp_path):
    definition = BenchmarkDefinition(
        key="fixture",
        dataset="org/dataset@sha256:dataset",
        task_count=1,
        dataset_source_revision="sha256:dataset",
        default_attempts=1,
        official_min_attempts=1,
        source_url="https://example.invalid/source",
        source_commit="commit",
    )
    root = tmp_path / "definitions"
    root.mkdir(mode=0o700)
    task = root / "org" / "task" / ("a" * 64)
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(
        "[task]\nname = 'org/task'\n"
        "[environment]\nos = 'linux'\ncpus = 1\nmemory_mb = 4096\n"
    )
    (task / "environment" / "Dockerfile").write_text(
        "FROM example/base:pinned\nWORKDIR /testbed\n"
    )
    relative = task.relative_to(root).as_posix()
    (root / "definitions.json").write_text(json.dumps({
        "schema": 1,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": 1,
        "tasks": [{"name": "org/task", "ref": "sha256:" + "a" * 64, "path": relative}],
    }))

    tasks = load_definitions(definition, root)

    assert len(tasks) == 1
    assert tasks[0].base_image == "example/base:pinned"
    assert tasks[0].dockerfile is True
    assert tasks[0].memory_mib == 4096


def test_official_definition_cache_rejects_same_count_task_digest_drift(
    tmp_path, monkeypatch
):
    expected_ref = "sha256:" + "a" * 64
    monkeypatch.setattr(
        images,
        "swebench_verified_task_digests",
        lambda: {"swe-bench/org__repo-1": expected_ref},
    )
    definition = BenchmarkDefinition(
        key="swebench-verified",
        dataset="org/dataset@sha256:dataset",
        task_count=1,
        dataset_source_revision="sha256:dataset",
        default_attempts=1,
        official_min_attempts=1,
        source_url="https://example.invalid/source",
        source_commit="commit",
    )
    root = tmp_path / "definitions"
    root.mkdir(mode=0o700)
    (root / "definitions.json").write_text(json.dumps({
        "schema": 1,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": 1,
        "tasks": [{
            "name": "swe-bench/org__repo-1",
            "ref": "sha256:" + "b" * 64,
            "path": "unused",
        }],
    }))

    with pytest.raises(ValueError, match="task digest lock mismatch"):
        load_definitions(definition, root)


def test_partial_rootless_store_is_valid_only_for_explicit_prepared_tasks(tmp_path):
    definition = BENCHMARKS["swebench-verified"]
    store = tmp_path / "partial-store"
    store.mkdir(mode=0o700)
    task = "swe-bench/psf__requests-1142"
    (store / "index.json").write_text(json.dumps({
        "schema": 1,
        "benchmark": definition.key,
        "dataset": definition.dataset,
        "dataset_source_revision": definition.dataset_source_revision,
        "task_count": definition.task_count,
        "images": {"example/image:latest": {"tasks": [task]}},
    }))

    _, index = load_image_store_index(store, definition, required_tasks=(task,))

    assert len(index["images"]) == 1
    with pytest.raises(ValueError, match="incomplete"):
        load_image_store_index(store, definition)
    with pytest.raises(ValueError, match="missing 1 requested"):
        load_image_store_index(
            store,
            definition,
            required_tasks=("swe-bench/missing",),
        )


def test_prepare_rootless_cli_has_bounded_phase_defaults():
    args = build_parser().parse_args(["prepare-rootless", "--phase", "definitions"])

    assert args.benchmark == "swebench-verified"
    assert args.definition_workers == 4
    assert args.asset_workers == 4
    assert args.cache_workers == 2
    assert args.crane == "auto"
    assert args.genisoimage is None


def test_rootless_qemu_is_default_and_apptainer_gets_singularity_alias(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TOFU_EVAL_BACKEND", raising=False)
    assert build_parser().parse_args(["doctor"]).backend == "rootless-qemu"
    assert build_parser().parse_args(
        ["doctor", "--task", "swe-bench/example"]
    ).tasks == ["swe-bench/example"]

    runtime_bin = tmp_path / "runtime-bin"
    runtime_bin.mkdir()
    apptainer = runtime_bin / "apptainer"
    apptainer.write_text("#!/bin/sh\nexit 0\n")
    apptainer.chmod(0o755)
    monkeypatch.setenv("PATH", str(runtime_bin))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    env = prepare_runtime_environment("singularity", run_dir)

    alias = run_dir / ".runtime-bin" / "singularity"
    assert alias.resolve() == apptainer.resolve()
    assert env["PATH"].split(":", 1)[0] == str(alias.parent)


def test_singularity_preflight_requires_writable_capacity(tmp_path, monkeypatch):
    runtime = tmp_path / "bin" / "apptainer"
    runtime.parent.mkdir()
    runtime.write_text("")
    runtime.chmod(0o755)
    monkeypatch.setattr(preflight, "singularity_runtime", lambda: str(runtime))
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/unshare")

    outputs = iter(
        [
            subprocess.CompletedProcess([], 0, "1.5.3\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "10240\n", ""),
        ]
    )
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: next(outputs))
    assert preflight._backend_check("singularity").status == "pass"

    too_small = iter(
        [
            subprocess.CompletedProcess([], 0, "1.5.3\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "64\n", ""),
        ]
    )
    monkeypatch.setattr(
        preflight.subprocess, "run", lambda *args, **kwargs: next(too_small)
    )
    check = preflight._backend_check("singularity")
    assert check.status == "fail"
    assert "10240 MiB" in check.detail


def test_software_benchmark_task_catalogs_are_digest_pinned():
    swe_digests = swebench_verified_task_digests()
    assert len(swe_digests) == 500
    assert swe_digests["swe-bench/psf__requests-1142"] == (
        "sha256:24359408df5948c741ac080425763ca4e1250b0ad1e7079e5f865e10db491fe4"
    )


def test_terminal_bench_21_config_is_digest_pinned_and_uses_five_attempts(tmp_path):
    task_digests = terminal_bench_21_task_digests()
    assert len(task_digests) == 89
    assert task_digests["terminal-bench/write-compressor"] == (
        "sha256:d9ddd9a8e925e2c566b37b2492cbf995afecefe58874e4043ef78d7f3c892c7e"
    )
    spec = HarborRunSpec(
        agent="codex",
        models=("openai/gpt-5",),
        backend="daytona",
        output_root=tmp_path,
        benchmark="terminal-bench-2.1",
        reasoning_effort="high",
        agent_version="1.2.3",
    )

    config = build_job_config(spec, run_dir=tmp_path / "run", run_id="tb21")

    assert config["n_attempts"] == 5
    assert config["datasets"] == [{
        "name": "terminal-bench/terminal-bench-2-1",
        "ref": "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a",
    }]
    assert config["environment"]["kwargs"] == {
        "snapshot_template_name": "{name}-tb-2-1"
    }
    assert config["agents"][0]["kwargs"] == {
        "reasoning_effort": "high",
        "version": "1.2.3",
    }

    _, run_dir = start_harbor_run(spec, dry_run=True)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["expected_tasks"] == 89
    assert manifest["expected_trials"] == 445
    assert manifest["leaderboard_trial_shape"] is True
    assert manifest["upload_enabled"] is False


def test_prediction_loader_splits_models_and_rejects_same_model_task_duplicate(tmp_path):
    path = tmp_path / "predictions.jsonl"
    rows = [
        {"instance_id": "task-1", "model_name_or_path": "a", "model_patch": "patch-a"},
        {"instance_id": "task-1", "model_name_or_path": "b", "model_patch": "patch-b"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    assert set(group_predictions(load_predictions(path))) == {"a", "b"}

    path.write_text(json.dumps([rows[0], rows[0]]))
    with pytest.raises(ValueError, match="duplicate prediction"):
        load_predictions(path)

    path.write_text(json.dumps([{**rows[0], "model_name_or_path": None}]))
    with pytest.raises(ValueError, match="must be strings"):
        load_predictions(path)


def test_harbor_audit_proves_cardinality_and_flags_errors(tmp_path):
    run_dir = create_run_dir(tmp_path / "evals", "audit-run")
    config_path = run_dir / "job-config.json"
    config_path.write_text(json.dumps({
        "environment": {"type": "modal", "delete": True},
        "n_attempts": 1,
        "datasets": [{
            "name": "swe-bench/swe-bench-verified",
            "ref": "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341",
        }],
        "agents": [
            {"name": "codex", "model_name": "a"},
            {"name": "codex", "model_name": "b"},
        ],
    }))
    manifest = {
        "kind": "harbor-agent-evaluation",
        "status": "succeeded",
        "run_id": "audit-run",
        "benchmark": "swebench-verified",
        "models": ["a", "b"],
        "dataset": (
            "swe-bench/swe-bench-verified@"
            "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341"
        ),
        "dataset_source_revision": "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341",
        "benchmark_source_commit": "ea2fee78517f2e591bad69fcf1e6731f9c23ec99",
        "harbor_source_commit": "ea2fee78517f2e591bad69fcf1e6731f9c23ec99",
        "upload_enabled": False,
        "expected_tasks": 1,
        "attempts_per_task": 1,
        "expected_trials": 2,
        "job_config": str(config_path),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    result_dir = run_dir / "jobs" / "audit-run"
    result_dir.mkdir(parents=True)
    for model in ("a", "b"):
        trial_dir = result_dir / f"trial-{model}"
        trial_dir.mkdir()
        (trial_dir / "config.json").write_text(json.dumps({
            "agent": {"model_name": model},
            "task": {"name": "task-1"},
        }))
        (trial_dir / "result.json").write_text(json.dumps({}))
    result_path = result_dir / "result.json"
    result_path.write_text(json.dumps({
        "n_total_trials": 2,
        "stats": {
            "n_completed_trials": 2,
            "n_errored_trials": 0,
            "n_pending_trials": 0,
            "n_running_trials": 0,
        },
    }))

    assert audit_run(run_dir)["ok"]

    payload = json.loads(result_path.read_text())
    payload["stats"]["n_errored_trials"] = 1
    result_path.write_text(json.dumps(payload))
    assert not audit_run(run_dir)["ok"]
    assert audit_run(run_dir, allow_errors=True)["ok"]


def test_terminal_bench_audit_allows_k_attempts_and_requires_rewarded_trajectory(tmp_path):
    run_dir = create_run_dir(tmp_path / "evals", "tb21-audit")
    config_path = run_dir / "job-config.json"
    config_path.write_text(json.dumps({
        "environment": {"type": "daytona", "delete": True},
        "n_attempts": 5,
        "datasets": [{
            "name": "terminal-bench/terminal-bench-2-1",
            "ref": "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a",
        }],
        "agents": [{"name": "codex", "model_name": "model"}],
    }))
    manifest = {
        "kind": "harbor-agent-evaluation",
        "status": "succeeded",
        "run_id": "tb21-audit",
        "benchmark": "terminal-bench-2.1",
        "models": ["model"],
        "dataset": (
            "terminal-bench/terminal-bench-2-1@"
            "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
        ),
        "dataset_source_revision": "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a",
        "benchmark_source_commit": "7131e4375048a0e408a8fb404b5f499d726b695b",
        "harbor_source_commit": "ea2fee78517f2e591bad69fcf1e6731f9c23ec99",
        "upload_enabled": False,
        "expected_tasks": 1,
        "attempts_per_task": 5,
        "expected_trials": 5,
        "job_config": str(config_path),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    result_dir = run_dir / "jobs" / "tb21-audit"
    result_dir.mkdir(parents=True)
    for attempt in range(5):
        trial_dir = result_dir / f"trial-{attempt}"
        trial_dir.mkdir()
        (trial_dir / "config.json").write_text(json.dumps({
            "agent": {"model_name": "model"},
            "task": {
                "name": "terminal-bench/write-compressor",
                "ref": "sha256:d9ddd9a8e925e2c566b37b2492cbf995afecefe58874e4043ef78d7f3c892c7e",
                "source": "terminal-bench/terminal-bench-2-1",
            },
        }))
        reward = 1 if attempt == 0 else 0
        (trial_dir / "result.json").write_text(json.dumps({
            "verifier_result": {"rewards": {"reward": reward}}
        }))
        if reward:
            (trial_dir / "agent").mkdir()
            (trial_dir / "agent" / "trajectory.json").write_text("{}")
    (result_dir / "result.json").write_text(json.dumps({
        "n_total_trials": 5,
        "stats": {
            "n_completed_trials": 5,
            "n_errored_trials": 0,
            "n_pending_trials": 0,
            "n_running_trials": 0,
        },
    }))

    assert audit_run(run_dir)["ok"]

    tampered_config_path = result_dir / "trial-4" / "config.json"
    tampered_config = json.loads(tampered_config_path.read_text())
    tampered_config["task"]["ref"] = "sha256:tampered"
    tampered_config_path.write_text(json.dumps(tampered_config))
    digest_report = audit_run(run_dir)
    assert not digest_report["ok"]
    assert not next(
        check
        for check in digest_report["checks"]
        if check["name"] == "canonical_task_digests"
    )["ok"]
    tampered_config["task"]["ref"] = (
        "sha256:d9ddd9a8e925e2c566b37b2492cbf995afecefe58874e4043ef78d7f3c892c7e"
    )
    tampered_config_path.write_text(json.dumps(tampered_config))

    (result_dir / "trial-0" / "agent" / "trajectory.json").unlink()
    report = audit_run(run_dir)
    assert not report["ok"]
    assert not next(
        check
        for check in report["checks"]
        if check["name"] == "rewarded_trials_have_trajectories"
    )["ok"]


def test_official_audit_requires_complete_error_free_report(tmp_path):
    run_dir = create_run_dir(tmp_path / "evals", "official-audit")
    model_dir = run_dir / "models" / "model-key"
    model_dir.mkdir(parents=True)
    predictions = [
        {"instance_id": "task-1", "model_name_or_path": "model", "model_patch": "patch-1"},
        {"instance_id": "task-2", "model_name_or_path": "model", "model_patch": ""},
    ]
    (model_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in predictions) + "\n"
    )
    report_path = model_dir / "model.run-123.json"
    report_path.write_text(json.dumps({
        "submitted_instances": 2,
        "completed_instances": 1,
        "empty_patch_instances": 1,
        "error_instances": 0,
    }))
    (run_dir / "manifest.json").write_text(json.dumps({
        "kind": "official-patch-evaluation",
        "status": "succeeded",
        "backend": "modal",
        "dataset": "SWE-bench/SWE-bench_Verified",
        "swebench_version": "4.1.0",
        "prediction_count": 2,
        "normalized_predictions_sha256": normalized_predictions_sha256(predictions),
        "groups": {
            "model-key": {
                "status": "succeeded",
                "instances": 2,
                "backend_run_id": "run-123",
            }
        },
    }))

    assert audit_run(run_dir)["ok"]

    tampered = [*predictions]
    tampered[0] = {**tampered[0], "model_patch": "changed-after-launch"}
    (model_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in tampered) + "\n"
    )
    tamper_audit = audit_run(run_dir)
    assert not tamper_audit["ok"]
    assert not next(
        check for check in tamper_audit["checks"] if check["name"] == "prediction_digest"
    )["ok"]
    (model_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in predictions) + "\n"
    )

    report = json.loads(report_path.read_text())
    report["error_instances"] = 1
    report_path.write_text(json.dumps(report))
    assert not audit_run(run_dir)["ok"]
