#!/usr/bin/env python3
"""Prepare and run all Terminal-Bench 2.1 tasks without host Docker or root.

Registry payloads are pulled by immutable manifest digest and embedded as opaque
files in read-only ISOs.  They are parsed and expanded only inside the rootless
QEMU guest.  The resulting image store can feed one concurrent Harbor job while
keeping each trial in a disposable VM.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import hashlib
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rootless_vm.harness_profiles import (  # noqa: E402
    HarnessProfile,
    harness_profile,
    harness_profile_ids,
    harness_profiles,
    profile_for_agent,
)
from rootless_vm.trajectory import (  # noqa: E402
    host_audit_to_atif,
    validate_atif,
    write_collected_trajectory,
)
from runtime_guards import probe_system_resources  # noqa: E402

DATASET_COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
TASK_COUNT = 89
LEADERBOARD_MINIMUM_ATTEMPTS = 5
TASK_CHECKSUMS_PATH = (
    PROJECT_ROOT / "evaluations" / "terminal_bench_21_task_checksums.json"
)
ASSET_SCHEMA = 2
INDEX_SCHEMA = 1
_PRINT_LOCK = threading.Lock()
_DEFAULT_TRIAL_CONCURRENCY_HARD_CEILING = 4
_QEMU_HOST_OVERHEAD_MIB = 512
_HOST_MEMORY_RESERVE_MIN_MIB = 2 * 1024
_HOST_DISK_RESERVE_MIN_MIB = 8 * 1024
_VALID_SCORE_LABELS = frozenset(
    {"passed", "model_semantic", "model_timeout", "model_environment_damage"}
)


def _say(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_dir(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"private directory must not be a symlink: {candidate}")
    if candidate.exists():
        path = candidate.resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"directory is group/world accessible: {path}")
    else:
        candidate.mkdir(parents=True, mode=0o700)
        path = candidate.resolve(strict=True)
    return path


def _executable(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"not an executable: {path}")
    return path


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _run(command: list[str], *, timeout: float | None = None) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"command failed ({result.returncode}): {detail}")
    return result.stdout


def _repository_without_tag(reference: str) -> str:
    reference = reference.split("@", 1)[0]
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    return reference[:colon] if colon > slash else reference


def _registry_candidates(image: str, mirrors: tuple[str, ...]) -> tuple[str, ...]:
    """Return explicit Docker Hub mirror references followed by the origin."""

    first_component, separator, remainder = image.partition("/")
    if separator and (
        "." in first_component
        or ":" in first_component
        or first_component == "localhost"
    ):
        if first_component not in {"docker.io", "index.docker.io"}:
            return (image,)
        docker_hub_path = remainder
    else:
        docker_hub_path = image
    candidates: list[str] = []
    for mirror in mirrors:
        normalized = mirror.strip().rstrip("/")
        if (
            not normalized
            or "://" in normalized
            or not re.fullmatch(
                r"[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._/-]+)?", normalized
            )
        ):
            raise ValueError(f"invalid registry mirror: {mirror!r}")
        candidates.append(f"{normalized}/{docker_hub_path}")
    candidates.append(image)
    return tuple(dict.fromkeys(candidates))


def _resolve_registry_image(
    crane: Path,
    image: str,
    mirrors: tuple[str, ...],
) -> tuple[str, str, dict[str, Any]]:
    errors: list[str] = []
    for candidate in _registry_candidates(image, mirrors):
        try:
            manifest_digest = _run(
                [str(crane), "digest", "--platform", "linux/amd64", candidate],
                timeout=180,
            ).strip()
            if not manifest_digest.startswith("sha256:"):
                raise RuntimeError(f"registry returned invalid digest for {candidate}")
            pinned = f"{_repository_without_tag(candidate)}@{manifest_digest}"
            manifest = json.loads(
                _run(
                    [str(crane), "manifest", "--platform", "linux/amd64", pinned],
                    timeout=180,
                )
            )
            if candidate != image:
                _say(f"MIRROR {image} via {candidate.split('/', 1)[0]}")
            return candidate, pinned, manifest
        except (RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(
        f"could not resolve registry image {image}; " + " | ".join(errors)[-4000:]
    )


@dataclass(frozen=True)
class Task:
    name: str
    path: Path
    image: str
    cpus: int
    memory_mib: int
    agent_timeout_sec: float
    verifier_timeout_sec: float
    storage_mib: int = 10 * 1024


def _load_tasks(tasks_root_value: str | os.PathLike[str]) -> list[Task]:
    tasks_root = Path(tasks_root_value).expanduser().resolve(strict=True)
    repository = tasks_root.parent
    revision = _run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], timeout=20
    ).strip()
    if revision != DATASET_COMMIT:
        raise ValueError(
            f"Terminal-Bench checkout must be {DATASET_COMMIT}, found {revision}"
        )
    dirty = _run(
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            tasks_root.name,
        ],
        timeout=20,
    ).strip()
    if dirty:
        raise ValueError("Terminal-Bench task checkout must be clean")
    tasks: list[Task] = []
    for path in sorted(tasks_root.iterdir()):
        config_path = path / "task.toml"
        if not path.is_dir() or not config_path.is_file():
            continue
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        task_name = config.get("task", {}).get("name")
        image = config.get("environment", {}).get("docker_image")
        expected_name = f"terminal-bench/{path.name}"
        if task_name != expected_name or not isinstance(image, str) or not image:
            raise ValueError(f"invalid task metadata: {path}")
        cpus = int(config.get("environment", {}).get("cpus") or 1)
        environment = config.get("environment", {})
        memory_mib = int(environment.get("memory_mb") or 2048)
        storage_mib = int(environment.get("storage_mb") or 10 * 1024)
        agent_timeout_sec = float(config.get("agent", {}).get("timeout_sec") or 900)
        verifier_timeout_sec = float(
            config.get("verifier", {}).get("timeout_sec") or 900
        )
        tasks.append(
            Task(
                path.name,
                path.resolve(),
                image,
                cpus,
                memory_mib,
                agent_timeout_sec,
                verifier_timeout_sec,
                storage_mib,
            )
        )
    if len(tasks) != TASK_COUNT:
        raise ValueError(f"expected {TASK_COUNT} tasks, found {len(tasks)}")
    return tasks


def _load_frozen_task_checksums(tasks: list[Task]) -> dict[str, str]:
    """Load Harbor-compatible content hashes for the pinned TB2.1 checkout."""

    try:
        payload = json.loads(TASK_CHECKSUMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Terminal-Bench task checksum manifest is unavailable"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("Terminal-Bench task checksum manifest is invalid")
    checksums = payload.get("task_checksums")
    if payload.get("dataset_commit") != DATASET_COMMIT or not isinstance(
        checksums, dict
    ):
        raise ValueError("Terminal-Bench task checksum manifest is invalid")
    expected_names = {task.name for task in tasks}
    if set(checksums) != expected_names:
        raise ValueError(
            "Terminal-Bench task checksum manifest has wrong task identities"
        )
    for name, checksum in checksums.items():
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError(f"invalid frozen task checksum for {name}")
    return {f"terminal-bench/{name}": checksum for name, checksum in checksums.items()}


def _valid_asset(task: Task, task_dir: Path) -> dict[str, Any] | None:
    metadata_path = task_dir / "asset.json"
    iso = task_dir / "task-image.iso"
    if metadata_path.is_symlink() or iso.is_symlink():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("schema") != ASSET_SCHEMA:
        return None
    if metadata.get("task") != task.name or metadata.get("image") != task.image:
        return None
    expected = metadata.get("iso_sha256")
    if not isinstance(expected, str) or not iso.is_file():
        return None
    if _sha256(iso) != expected:
        return None
    return metadata


def _prepare_one(
    task: Task,
    *,
    assets_root: Path,
    crane: Path,
    archive_tool: Path,
    genisoimage: Path | None,
    registry_mirrors: tuple[str, ...],
) -> dict[str, Any]:
    started = time.monotonic()
    task_dir = assets_root / task.name
    if task_dir.is_symlink():
        raise ValueError(f"asset directory must not be a symlink: {task_dir}")
    task_dir.mkdir(mode=0o700, exist_ok=True)
    existing = _valid_asset(task, task_dir)
    if existing is not None:
        _say(f"ASSET HIT {task.name}")
        return existing

    registry_source, pinned, manifest = _resolve_registry_image(
        crane,
        task.image,
        registry_mirrors,
    )
    manifest_digest = pinned.rsplit("@", 1)[1]
    config_digest = manifest.get("config", {}).get("digest")
    if not isinstance(config_digest, str) or not config_digest.startswith("sha256:"):
        raise RuntimeError(f"image manifest has no config digest: {task.image}")

    token = uuid.uuid4().hex
    oci_path = task_dir / f".oci.{token}.partial"
    tar_path = task_dir / f".task-image.{token}.tar.partial"
    iso_path = task_dir / f".task-image.{token}.iso.partial"
    staging = task_dir / f".staging.{token}"
    staging.mkdir(mode=0o700)
    staged_tar = staging / "task-image.tar"
    try:
        _say(f"PULL {task.name} {manifest_digest}")
        _run(
            [
                str(crane),
                "pull",
                "--platform",
                "linux/amd64",
                "--format",
                "oci",
                pinned,
                str(oci_path),
            ],
            timeout=7200,
        )
        index_path = oci_path / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or len(descriptors) != 1:
            raise RuntimeError(f"OCI index has an unexpected shape: {task.image}")
        descriptor = descriptors[0]
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("digest") != manifest_digest
        ):
            raise RuntimeError(f"OCI index digest mismatch: {task.image}")
        descriptor["annotations"] = {"org.opencontainers.image.ref.name": task.image}
        _atomic_json(index_path, index)
        _run(
            [str(archive_tool), "-cf", str(tar_path), "-C", str(oci_path), "."],
            timeout=7200,
        )
        tar_digest = _sha256(tar_path)
        if genisoimage is None:
            from evaluations.swebench.images import write_payload_iso

            write_payload_iso(tar_path, iso_path)
        else:
            os.link(tar_path, staged_tar)
            _run(
                [
                    str(genisoimage),
                    "-quiet",
                    "-o",
                    str(iso_path),
                    "-iso-level",
                    "3",
                    "-J",
                    "-R",
                    str(staged_tar),
                ],
                timeout=7200,
            )
        iso_digest = _sha256(iso_path)
        final_iso = task_dir / "task-image.iso"
        os.replace(iso_path, final_iso)
        final_iso.chmod(0o600)
        metadata: dict[str, Any] = {
            "schema": ASSET_SCHEMA,
            "task": task.name,
            "task_path": str(task.path),
            "image": task.image,
            "registry_source": registry_source,
            "registry_digest": manifest_digest,
            "image_config_digest": config_digest,
            "loaded_image_reference": manifest_digest,
            "archive_format": "oci",
            "tar_sha256": tar_digest,
            "iso_sha256": iso_digest,
            "iso_bytes": final_iso.stat().st_size,
        }
        _atomic_json(task_dir / "asset.json", metadata)
        _say(
            f"ASSET OK {task.name} {final_iso.stat().st_size / 2**20:.1f} MiB "
            f"{time.monotonic() - started:.1f}s"
        )
        return metadata
    finally:
        for path in (staged_tar, tar_path, iso_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            staging.rmdir()
        except FileNotFoundError:
            pass
        if oci_path.exists():
            if oci_path.is_symlink() or oci_path.parent != task_dir:
                raise RuntimeError("refusing unsafe OCI staging cleanup")
            shutil.rmtree(oci_path)


def prepare_assets(args: argparse.Namespace) -> int:
    all_tasks = _load_tasks(args.tasks_root)
    tasks = _select_tasks(all_tasks, getattr(args, "task", None))
    assets_root = _private_dir(args.assets_root)
    crane = _executable(args.crane)
    archive_tool = _executable(args.archive_tool)
    genisoimage = _executable(args.genisoimage) if args.genisoimage else None
    registry_mirrors = tuple(getattr(args, "registry_mirror", ()) or ())
    worker_count = args.workers
    if worker_count is None:
        worker_count, resource_budget = _adaptive_trial_concurrency(
            tasks, assets_root
        )
        resource_budget.pop("resolved_trial_concurrency", None)
        resource_budget.update(
            {"operation": "prepare-assets", "resolved_worker_count": worker_count}
        )
        resource_budget_output = assets_root / "prepare-assets.resources.json"
        _atomic_json(resource_budget_output, resource_budget)
        _say(
            f"RESOURCE DEFAULT workers={worker_count} "
            f"evidence={resource_budget_output}"
        )
    completed: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                _prepare_one,
                task,
                assets_root=assets_root,
                crane=crane,
                archive_tool=archive_tool,
                genisoimage=genisoimage,
                registry_mirrors=registry_mirrors,
            ): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            completed[task.image] = future.result()
            _say(f"PROGRESS {len(completed)}/{len(tasks)}")
    prepared_by_image = dict(completed)
    for task in all_tasks:
        if task.image in prepared_by_image:
            continue
        metadata = _valid_asset(task, assets_root / task.name)
        if metadata is not None:
            prepared_by_image[task.image] = metadata
    images = {
        task.image: {
            "iso": f"{task.name}/task-image.iso",
            "sha256": prepared_by_image[task.image]["iso_sha256"],
            "loaded_image_reference": prepared_by_image[task.image][
                "loaded_image_reference"
            ],
            "registry_digest": prepared_by_image[task.image]["registry_digest"],
            "task": task.name,
            "agent_timeout_sec": task.agent_timeout_sec,
            "verifier_timeout_sec": task.verifier_timeout_sec,
        }
        for task in all_tasks
        if task.image in prepared_by_image
    }
    _atomic_json(assets_root / "index.json", {"schema": INDEX_SCHEMA, "images": images})
    _say(f"INDEX OK {assets_root / 'index.json'} ({len(images)} images)")
    return 0


def _resolve_concurrency(trials: int, agents: int | None) -> tuple[int, int]:
    if not 1 <= trials <= 32:
        raise ValueError("trial concurrency must be between 1 and 32")
    agent_concurrency = agents if agents is not None else trials
    if not 1 <= agent_concurrency <= trials:
        raise ValueError(
            "agent concurrency must be positive and no greater than trial concurrency"
        )
    return trials, agent_concurrency


def _adaptive_trial_concurrency(
    tasks: list[Task], probe_root: Path
) -> tuple[int, dict[str, Any]]:
    """Resolve a lean VM default from one shared launch-time resource probe."""

    if not tasks:
        raise ValueError("at least one Terminal-Bench task is required")
    probe_environment = dict(os.environ)
    probe_environment["TOFU_DATA_DIR"] = str(probe_root)
    try:
        snapshot = probe_system_resources(probe_environment)
    except Exception as exc:
        return 1, {
            "schema": 1,
            "adaptive": False,
            "fallback": "resource_probe_failed",
            "probe_error_type": type(exc).__name__,
            "resolved_trial_concurrency": 1,
            "hard_ceiling": _DEFAULT_TRIAL_CONCURRENCY_HARD_CEILING,
        }

    maximum_task_cpus = max(1, max(task.cpus for task in tasks))
    host_cpus_per_vm = min(2, maximum_task_cpus)
    cpu_slots = max(1, snapshot.effective_cpu_count // host_cpus_per_vm)

    maximum_task_memory_mib = max(512, max(task.memory_mib for task in tasks))
    memory_per_vm_mib = maximum_task_memory_mib + _QEMU_HOST_OVERHEAD_MIB
    memory_capacity_mib = snapshot.effective_memory_capacity_mb
    memory_available_mib = snapshot.effective_memory_available_mb
    if memory_capacity_mib is None or memory_available_mib is None:
        memory_slots = 1
        memory_reserve_mib = None
    else:
        memory_reserve_mib = max(
            _HOST_MEMORY_RESERVE_MIN_MIB,
            math.ceil(memory_capacity_mib * 0.25),
        )
        memory_slots = max(
            1,
            max(0, memory_available_mib - memory_reserve_mib)
            // memory_per_vm_mib,
        )

    disk_per_vm_mib = max(2 * 1024, max(task.storage_mib for task in tasks))
    disk_total_mib = snapshot.disk_total_mb
    disk_free_mib = snapshot.disk_free_mb
    if disk_total_mib is None or disk_free_mib is None:
        disk_slots = 1
        disk_reserve_mib = None
    else:
        disk_reserve_mib = max(
            _HOST_DISK_RESERVE_MIN_MIB,
            math.ceil(disk_total_mib * 0.02),
        )
        disk_slots = max(
            1,
            max(0, disk_free_mib - disk_reserve_mib) // disk_per_vm_mib,
        )

    resolved = max(
        1,
        min(
            _DEFAULT_TRIAL_CONCURRENCY_HARD_CEILING,
            cpu_slots,
            memory_slots,
            disk_slots,
        ),
    )
    return resolved, {
        "schema": 1,
        "adaptive": True,
        "probe": snapshot.as_dict(),
        "task_envelope": {
            "maximum_cpus": maximum_task_cpus,
            "maximum_memory_mib": maximum_task_memory_mib,
            "maximum_storage_mib": disk_per_vm_mib,
            "qemu_host_overhead_mib": _QEMU_HOST_OVERHEAD_MIB,
        },
        "reservations": {
            "memory_mib": memory_reserve_mib,
            "disk_mib": disk_reserve_mib,
        },
        "slot_limits": {
            "cpu": cpu_slots,
            "memory": memory_slots,
            "disk": disk_slots,
        },
        "hard_ceiling": _DEFAULT_TRIAL_CONCURRENCY_HARD_CEILING,
        "resolved_trial_concurrency": resolved,
    }


def _select_tasks(tasks: list[Task], requested_values: list[str] | None) -> list[Task]:
    if not requested_values:
        return tasks
    requested = set(requested_values)
    available = {task.name for task in tasks}
    missing = sorted(requested - available)
    if missing:
        raise ValueError(f"unknown Terminal-Bench tasks: {missing}")
    return [task for task in tasks if task.name in requested]


def write_config(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.tasks_root)
    profile = harness_profile(args.harness)
    if profile.profile_id == "codex-kimi":
        raise ValueError(
            "codex-kimi must run through `python -m evaluations.swebench run` "
            "so its host-only proxy and per-trial evidence are lifecycle-owned"
        )
    if profile.requires_guest_credentials and not args.allow_guest_credentials:
        raise PermissionError(
            f"harness {profile.profile_id!r} runs inside the task container and "
            "requires guest-visible model credentials; pass "
            "--allow-guest-credentials only with explicitly authorized, "
            "short-lived credentials"
        )
    max_rounds = (
        args.max_rounds if args.max_rounds is not None else profile.default_max_rounds
    )
    max_output_tokens = (
        args.max_output_tokens
        if args.max_output_tokens is not None
        else profile.default_max_output_tokens
    )
    context_checkpoint_tokens = (
        args.context_checkpoint_tokens
        if args.context_checkpoint_tokens is not None
        else profile.default_context_checkpoint_tokens
    )
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max rounds must be positive")
    if max_output_tokens is not None and max_output_tokens < 256:
        raise ValueError("max output tokens must be at least 256")
    if context_checkpoint_tokens is not None and not (
        1024 <= context_checkpoint_tokens <= 1_000_000
    ):
        raise ValueError("context checkpoint tokens must be between 1024 and 1000000")
    if not 1 <= args.global_dispatch_concurrency <= 32:
        raise ValueError("global dispatch concurrency must be between 1 and 32")
    if not 1 <= args.egress_global_concurrency <= 128:
        raise ValueError("egress global concurrency must be between 1 and 128")
    if not 0 <= args.top_p <= 1:
        raise ValueError("top_p must be between 0 and 1")
    if args.runtime_timeout_multiplier < 1:
        raise ValueError("runtime timeout multiplier must be at least 1")
    verifier_timeout_multiplier = (
        args.verifier_timeout_multiplier
        if args.verifier_timeout_multiplier is not None
        else args.runtime_timeout_multiplier
    )
    if verifier_timeout_multiplier < 1:
        raise ValueError("verifier timeout multiplier must be at least 1")
    tasks = _select_tasks(tasks, args.task)
    # Harbor's verifier currently calls BaseEnvironment.exec() without passing
    # its computed phase timeout. Keep the environment's inner watchdog at
    # least as large as the selected tasks' scaled verifier budgets; otherwise
    # it silently kills a valid verifier early and Harbor reports a misleading
    # RewardFileNotFoundError.
    default_exec_timeout_sec = max(
        900.0,
        max(task.verifier_timeout_sec for task in tasks) * verifier_timeout_multiplier,
    )
    if default_exec_timeout_sec > 86400:
        raise ValueError("scaled verifier timeout exceeds the 24 hour safety cap")
    control_root = _private_dir(args.control_root)
    assets_root = _private_dir(args.assets_root)
    state_root = _private_dir(args.state_root)
    cache_root = _private_dir(args.cache_root)
    jobs_dir = _private_dir(args.jobs_dir)
    resource_budget = None
    requested_trial_concurrency = args.concurrency
    if requested_trial_concurrency is None:
        requested_trial_concurrency, resource_budget = _adaptive_trial_concurrency(
            tasks, state_root
        )
    trial_concurrency, agent_concurrency = _resolve_concurrency(
        requested_trial_concurrency, args.agent_concurrency
    )
    base_disk = Path(args.base_disk).expanduser().resolve(strict=True)
    qemu = _executable(args.qemu)
    qemu_img = _executable(args.qemu_img)
    if not (assets_root / "index.json").is_file():
        raise ValueError("image store is not prepared")
    host_agent_kwargs: dict[str, Any] = {
        "reasoning_effort": args.reasoning_effort,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "dispatch_max_retries": args.dispatch_max_retries,
        "global_dispatch_concurrency": args.global_dispatch_concurrency,
        "dispatch_gate_dir": str(control_root / "dispatch-gate"),
    }
    if max_rounds is not None:
        host_agent_kwargs["max_rounds"] = max_rounds
    if max_output_tokens is not None:
        host_agent_kwargs["max_output_tokens"] = max_output_tokens
    if profile.profile_id == "tofu":
        assert context_checkpoint_tokens is not None
        host_agent_kwargs.update(
            {
                "context_checkpoint_tokens": context_checkpoint_tokens,
                "command_timeout_sec": min(
                    1800, round(120 * args.runtime_timeout_multiplier)
                ),
                "command_timeout_multiplier": args.runtime_timeout_multiplier,
            }
        )
    elif profile.profile_id == "deepseek-minimal":
        host_agent_kwargs["bash_timeout_sec"] = min(
            1800, round(300 * args.runtime_timeout_multiplier)
        )
        assert profile.default_context_window_tokens is not None
        host_agent_kwargs["context_window_tokens"] = (
            profile.default_context_window_tokens
        )

    if profile.host_dispatch:
        agent_kwargs = host_agent_kwargs
    else:
        # Installed CLI harnesses own their provider/config surface. Do not pass
        # Tofu-only retry, sampling, or checkpoint controls into the guest.
        agent_kwargs = {"reasoning_effort": args.reasoning_effort}

    config = {
        "job_name": args.job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": args.attempts,
        "n_concurrent_trials": trial_concurrency,
        # Pure TCG deliberately trades native speed for rootless isolation.
        # Scale only wall-clock budgets; task inputs, commands, and scoring stay
        # identical to the pinned Terminal-Bench dataset.
        "agent_timeout_multiplier": args.runtime_timeout_multiplier,
        "verifier_timeout_multiplier": verifier_timeout_multiplier,
        "environment_build_timeout_multiplier": 6.0,
        "retry": {"max_retries": args.max_retries},
        "environment": {
            "import_path": "rootless_vm.harbor_environment:RootlessQemuEnvironment",
            "kwargs": {
                "base_disk": str(base_disk),
                "base_disk_sha256": _sha256(base_disk),
                "image_store": str(assets_root),
                "state_root": str(state_root),
                "prepared_cache_root": str(cache_root),
                "qemu_path": str(qemu),
                "qemu_img_path": str(qemu_img),
                "egress_max_bytes": args.egress_max_gib * 1024**3,
                "egress_global_concurrency": args.egress_global_concurrency,
                "image_prepare_timeout_sec": 3600,
                "default_exec_timeout_sec": default_exec_timeout_sec,
                "verifier_timeout_multiplier": verifier_timeout_multiplier,
                "virtual_time_shift": args.virtual_time_shift,
            },
        },
        "agents": [
            {
                "name": profile.harbor_agent,
                "model_name": args.model,
                # Keep model/API pressure below the number of live trials so
                # slow pure-TCG verifiers can overlap with later agent work.
                "n_concurrent": agent_concurrency,
                "kwargs": agent_kwargs,
            }
        ],
        "tasks": [{"path": str(task.path)} for task in tasks],
    }
    output = control_root / f"{args.job_name}.json"
    if resource_budget is not None:
        resource_budget["resolved_agent_concurrency"] = agent_concurrency
        resource_budget_output = control_root / f"{args.job_name}.resources.json"
        _atomic_json(resource_budget_output, resource_budget)
        _say(
            "RESOURCE DEFAULT "
            f"trials={trial_concurrency} agents={agent_concurrency} "
            f"evidence={resource_budget_output}"
        )
    _atomic_json(output, config)
    print(output)
    return 0


def list_harnesses(_args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {"schema": 1, "harnesses": [row.to_dict() for row in harness_profiles()]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def prepare_cache(args: argparse.Namespace) -> int:
    from rootless_vm.image_cache import PreparedImageCache, PreparedImageSpec
    from rootless_vm.image_store import resolve_image_store
    from rootless_vm.qemu import QemuRuntime

    tasks = _select_tasks(_load_tasks(args.tasks_root), getattr(args, "task", None))
    assets_root = _private_dir(args.assets_root)
    cache_root = _private_dir(args.cache_root)
    base_disk = Path(args.base_disk).expanduser().resolve(strict=True)
    runtime = QemuRuntime.discover(args.qemu, args.qemu_img)
    base_sha256 = _sha256(base_disk)
    worker_count = args.workers
    if worker_count is None:
        worker_count, resource_budget = _adaptive_trial_concurrency(tasks, cache_root)
        resource_budget.pop("resolved_trial_concurrency", None)
        resource_budget.update(
            {"operation": "prepare-cache", "resolved_worker_count": worker_count}
        )
        resource_budget_output = cache_root / "prepare-cache.resources.json"
        _atomic_json(resource_budget_output, resource_budget)
        _say(
            f"RESOURCE DEFAULT workers={worker_count} "
            f"evidence={resource_budget_output}"
        )

    def one(task: Task):
        iso, iso_sha256, loaded_reference = resolve_image_store(
            str(assets_root), task.image
        )
        result = PreparedImageCache(
            PreparedImageSpec(
                runtime=runtime,
                cache_root=cache_root,
                base_disk=base_disk,
                payload_iso=iso,
                task_image=loaded_reference,
                expected_base_disk_sha256=base_sha256,
                expected_payload_iso_sha256=iso_sha256,
                memory_mib=max(512, task.memory_mib),
                cpus=max(2, task.cpus),
                boot_timeout_sec=360,
                prepare_timeout_sec=args.prepare_timeout_sec,
            )
        ).prepare()
        _say(
            f"CACHE {'HIT' if result.cache_hit else 'OK'} {task.name} "
            f"{result.disk.stat().st_size / 2**20:.1f} MiB {result.elapsed_sec:.1f}s"
        )
        return task.name

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(one, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            future.result()
            completed += 1
            _say(f"CACHE PROGRESS {completed}/{len(tasks)}")
    return 0


def _validate_config_tasks(config: Path) -> None:
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Harbor config JSON: {config}") from exc
    rows = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Harbor config must contain at least one task")
    task_paths: list[Path] = []
    for row in rows:
        raw_path = row.get("path") if isinstance(row, dict) else None
        if not isinstance(raw_path, str):
            raise ValueError("every Harbor config task must have a path")
        task_paths.append(Path(raw_path).expanduser().resolve(strict=True))
    if len(set(task_paths)) != len(task_paths):
        raise ValueError("Harbor config contains duplicate task paths")
    parents = {path.parent for path in task_paths}
    if len(parents) != 1:
        raise ValueError("Harbor config tasks must share one pinned checkout")
    available = {task.path for task in _load_tasks(parents.pop())}
    missing = set(task_paths) - available
    if missing:
        raise ValueError(
            f"Harbor config contains unknown task paths: {sorted(missing)}"
        )


def run_config(args: argparse.Namespace) -> int:
    harbor = _executable(args.harbor)
    config = Path(args.config).expanduser().resolve(strict=True)
    _validate_config_tasks(config)
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(project_root) + (os.pathsep + existing if existing else "")
    process = subprocess.Popen(
        [str(harbor), "run", "--config", str(config), "--yes"],
        cwd=project_root,
        env=env,
        start_new_session=True,
    )
    try:
        return process.wait()
    except KeyboardInterrupt:
        # Harbor may be awaiting an asyncio.to_thread QGA operation. Give it a
        # short graceful window, then bound shutdown. QEMU itself is in a
        # separate session and carries a parent-death signal, so terminating
        # Harbor also tears down every disposable VM.
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                return process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
        process.wait()
        return 130


def run_series(args: argparse.Namespace) -> int:
    configs = [Path(value).expanduser().resolve(strict=True) for value in args.config]
    if len(set(configs)) != len(configs):
        raise ValueError("run-series config paths must be unique")
    for index, config in enumerate(configs, start=1):
        _say(f"SERIES START {index}/{len(configs)} {config.name}")
        exit_code = run_config(
            argparse.Namespace(harbor=args.harbor, config=str(config))
        )
        _say(f"SERIES END {index}/{len(configs)} exit={exit_code} {config.name}")
        if exit_code:
            return exit_code
    return 0


_NETWORK_MARKERS = (
    "failed to download",
    "failed to fetch",
    "tunnel error",
    "temporary failure resolving",
    "could not resolve host",
    "connection reset by peer",
    "connection failed [ip:",
    "network is unreachable",
    "connection timed out",
    "502 bad gateway",
)
_API_MARKERS = (
    "retryableapierror",
    "readtimeout",
    "scheduler unavailable",
    "inference_engine",
    "first token timeout",
    "首包超时",
    "api http 500",
    "api http 502",
    "api http 503",
    "api http 504",
)


def _tail_text(path: Path, limit: int = 256 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _edge_text(path: Path, limit: int = 256 * 1024) -> str:
    """Read bounded leading and trailing evidence from a potentially huge log."""

    try:
        with path.open("rb") as stream:
            head = stream.read(limit)
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size <= limit:
                tail = b""
            else:
                stream.seek(max(limit, size - limit))
                tail = stream.read(limit)
        return (head + b"\n" + tail).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _verifier_failure_signatures(verifier_text: str) -> list[str]:
    """Extract actual pytest/verifier failures, excluding zero-failure summaries."""

    signatures: list[str] = []
    for line in verifier_text.splitlines():
        stripped = line.strip()
        if not re.match(r"^(FAILED|ERROR)\b", stripped):
            continue
        if re.fullmatch(
            r"FAILED\s*\(\s*0\s*/\s*\d+\s*\)\s*:\s*(?:None)?",
            stripped,
            flags=re.IGNORECASE,
        ):
            continue
        signatures.append(stripped[:500])
    return signatures[-8:]


def _seconds_between(start: Any, finish: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        return (
            datetime.fromisoformat(finish.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None


def _positive_float(value: Any, default: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _host_transcript_path(trial_dir: Path) -> Path:
    agent_dir = trial_dir / "agent"
    generic = agent_dir / "host-dispatch-transcript.json"
    return generic if generic.is_file() else agent_dir / "tofu-host-transcript.json"


def _transcript_audit(
    trial_dir: Path,
) -> tuple[list[str], int, dict[str, int]]:
    transcript = _host_transcript_path(trial_dir)
    try:
        rows = json.loads(transcript.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (
            [],
            0,
            {
                "dispatches": 0,
                "429_retries": 0,
                "max_429_retries": 0,
                "slot_wait_cycles": 0,
                "upstream_429_retries": 0,
                "gate_wait_ms": 0,
                "persistent_bash_timeouts": 0,
                "maximum_assistant_tool_calls": 0,
            },
        )
    models: set[str] = set()
    raw_reasoning = 0
    dispatches = 0
    retries_429 = 0
    max_retries_429 = 0
    slot_wait_cycles = 0
    upstream_429_retries = 0
    gate_wait_ms = 0
    persistent_bash_timeouts = 0
    maximum_assistant_tool_calls = 0
    if not isinstance(rows, list):
        return (
            [],
            0,
            {
                "dispatches": 0,
                "429_retries": 0,
                "max_429_retries": 0,
                "slot_wait_cycles": 0,
                "upstream_429_retries": 0,
                "gate_wait_ms": 0,
                "persistent_bash_timeouts": 0,
                "maximum_assistant_tool_calls": 0,
            },
        )
    for row in rows:
        if not isinstance(row, dict):
            continue
        usage = row.get("usage")
        dispatch = usage.get("_dispatch") if isinstance(usage, dict) else None
        model = dispatch.get("model") if isinstance(dispatch, dict) else None
        if isinstance(model, str) and model:
            models.add(model)
        if isinstance(dispatch, dict):
            dispatches += 1
            retries = int(dispatch.get("429_retries") or 0)
            retries_429 += retries
            max_retries_429 = max(max_retries_429, retries)
            slot_wait_cycles += int(dispatch.get("slot_wait_cycles") or 0)
            upstream_429_retries += int(dispatch.get("upstream_429_retries") or 0)
            gate_wait_ms += int(dispatch.get("gate_wait_ms") or 0)
        assistant = row.get("assistant")
        assistant_tool_calls = (
            assistant.get("tool_calls") if isinstance(assistant, dict) else None
        )
        if isinstance(assistant_tool_calls, list):
            maximum_assistant_tool_calls = max(
                maximum_assistant_tool_calls, len(assistant_tool_calls)
            )
        reasoning = (
            assistant.get("reasoning_content") if isinstance(assistant, dict) else None
        )
        if isinstance(reasoning, str) and reasoning:
            raw_reasoning += 1
        result_text = row.get("result")
        if isinstance(result_text, str) and re.search(
            r"\[command timed out after \d+ seconds\]", result_text
        ):
            persistent_bash_timeouts += 1
    return (
        sorted(models),
        raw_reasoning,
        {
            "dispatches": dispatches,
            "429_retries": retries_429,
            "max_429_retries": max_retries_429,
            "slot_wait_cycles": slot_wait_cycles,
            "upstream_429_retries": upstream_429_retries,
            "gate_wait_ms": gate_wait_ms,
            "persistent_bash_timeouts": persistent_bash_timeouts,
            "maximum_assistant_tool_calls": maximum_assistant_tool_calls,
        },
    )


def _route_audit(trial_dir: Path) -> tuple[list[str], int]:
    models, raw_reasoning, _metrics = _transcript_audit(trial_dir)
    return models, raw_reasoning


def _root_cause_attribution(row: dict[str, Any]) -> dict[str, Any]:
    """Map a detailed trial classification to one accountable failure layer."""

    label = str(row.get("classification") or "harness_error")
    if label == "passed":
        layer, confidence, retry_scope = "none", 1.0, "none"
    elif label.startswith("model_"):
        layer, confidence, retry_scope = "model", 0.82, "new_model_attempt"
    elif label.startswith("harness_") or label == "harness_error":
        layer, confidence, retry_scope = "harness", 0.92, "fix_harness"
    elif label == "infrastructure_api":
        layer, confidence, retry_scope = "provider", 0.95, "retry_infrastructure"
    elif label.startswith("infrastructure_verifier"):
        layer, confidence, retry_scope = "verifier", 0.93, "retry_infrastructure"
    elif label.startswith("infrastructure_network"):
        layer, confidence, retry_scope = "network", 0.94, "retry_infrastructure"
    elif label.startswith("infrastructure_"):
        layer, confidence, retry_scope = "infrastructure", 0.9, "retry_infrastructure"
    elif label.startswith("environment_"):
        layer, confidence, retry_scope = "environment", 0.9, "calibrated_retry"
    elif label.startswith("routing_") or label == "routing_violation":
        layer, confidence, retry_scope = "routing", 0.99, "fix_routing"
    elif label == "privacy_violation":
        layer, confidence, retry_scope = "security", 1.0, "security_investigation"
    elif label.startswith("task_"):
        layer, confidence, retry_scope = "dataset", 0.98, "fix_dataset"
    else:
        layer, confidence, retry_scope = "unknown", 0.5, "manual_review"

    evidence_codes = [f"classification:{label}"]
    if row.get("reward") is not None:
        evidence_codes.append(f"verifier_reward:{row['reward']}")
    if row.get("exception_type"):
        evidence_codes.append(f"exception:{row['exception_type']}")
    if row.get("served_models"):
        evidence_codes.append("physical_route_audited")
    if row.get("failure_signatures"):
        evidence_codes.append("verifier_failure_signature")
    return {
        "layer": layer,
        "cause": label,
        "confidence": confidence,
        "retry_scope": retry_scope,
        "summary": row.get("reason") or label,
        "evidence_codes": evidence_codes,
    }


def _classify_trial(
    trial: dict[str, Any],
    trial_dir: Path,
    expected_model: str,
) -> dict[str, Any]:
    verifier_result = trial.get("verifier_result") or {}
    rewards = (
        verifier_result.get("rewards") if isinstance(verifier_result, dict) else {}
    )
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    reward_value = float(reward) if isinstance(reward, (int, float)) else None
    exception = trial.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    exception_message = str(exception.get("exception_message") or "")
    verifier_text = _tail_text(trial_dir / "verifier" / "test-stdout.txt")
    verifier_stdout_exists = (trial_dir / "verifier" / "test-stdout.txt").is_file()
    verifier_report_exists = (trial_dir / "verifier" / "ctrf.json").is_file()
    operator_control: dict[str, Any] = {}
    try:
        loaded_control = json.loads(
            (trial_dir / "infrastructure-control.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        loaded_control = None
    if (
        isinstance(loaded_control, dict)
        and loaded_control.get("schema") == 1
        and loaded_control.get("trial")
        == str(trial.get("trial_name") or trial_dir.name)
        and loaded_control.get("action") == "qmp_quit"
        and isinstance(loaded_control.get("cause"), str)
    ):
        operator_control = loaded_control
    transcript_path = _host_transcript_path(trial_dir)
    audit_text = _tail_text(transcript_path)
    audit_edge_text = _edge_text(transcript_path)
    exception_text = _tail_text(trial_dir / "exception.txt")
    combined = "\n".join(
        (exception_type, exception_message, exception_text, verifier_text)
    )
    lowered = combined.lower()
    verifier_last_line = next(
        (line.strip() for line in reversed(verifier_text.splitlines()) if line.strip()),
        "",
    )
    served_models, raw_reasoning, dispatch_audit = _transcript_audit(trial_dir)
    verifier_timing = trial.get("verifier") or {}
    verifier_elapsed = _seconds_between(
        verifier_timing.get("started_at")
        if isinstance(verifier_timing, dict)
        else None,
        verifier_timing.get("finished_at")
        if isinstance(verifier_timing, dict)
        else None,
    )
    agent_timing = trial.get("agent_execution") or {}
    agent_elapsed = _seconds_between(
        agent_timing.get("started_at") if isinstance(agent_timing, dict) else None,
        agent_timing.get("finished_at") if isinstance(agent_timing, dict) else None,
    )
    agent_result = trial.get("agent_result") or {}
    trial_config = trial.get("config") or {}
    if not isinstance(trial_config, dict):
        trial_config = {}
    agent_timeout_multiplier = _positive_float(
        trial_config.get("agent_timeout_multiplier")
        or trial_config.get("timeout_multiplier")
    )
    verifier_timeout_multiplier = _positive_float(
        trial_config.get("verifier_timeout_multiplier")
        or trial_config.get("timeout_multiplier")
    )
    agent_metadata = (
        agent_result.get("metadata") if isinstance(agent_result, dict) else {}
    )
    if not isinstance(agent_metadata, dict):
        agent_metadata = {}
    agent_info = trial.get("agent_info") or {}
    if not isinstance(agent_info, dict):
        agent_info = {}
    agent_config = trial_config.get("agent") or {}
    if not isinstance(agent_config, dict):
        agent_config = {}
    agent_kwargs = agent_config.get("kwargs") or {}
    if not isinstance(agent_kwargs, dict):
        agent_kwargs = {}
    environment_config = trial_config.get("environment") or {}
    if not isinstance(environment_config, dict):
        environment_config = {}
    environment_kwargs = environment_config.get("kwargs") or {}
    if not isinstance(environment_kwargs, dict):
        environment_kwargs = {}
    failure_signatures = _verifier_failure_signatures(verifier_text)

    reason = ""
    if raw_reasoning:
        classification = "privacy_violation"
        reason = "raw reasoning was persisted in the audit transcript"
    elif served_models and served_models != [expected_model]:
        classification = "routing_violation"
        reason = f"served models were {served_models!r}"
    elif exception_type == "CancelledError" and reward_value is None:
        classification = "infrastructure_cancelled"
        reason = (
            "the Harbor run was cancelled before a numeric verifier result; "
            "this is an interrupted trial, not a model score or network diagnosis"
        )
    elif operator_control and reward_value is None:
        classification = "infrastructure_operator_terminated"
        reason = (
            "the disposable VM was explicitly ended after an audited "
            + str(operator_control["cause"]).replace("_", " ")
            + "; regenerate this trial slot"
        )
    elif served_models == [expected_model] and reward_value == 1.0:
        classification = "passed"
        reason = "verifier reward is 1"
    elif (
        served_models == [expected_model]
        and reward_value is not None
        and agent_metadata.get("exit_reason") == "round_limit"
    ):
        classification = "harness_round_limit"
        reason = (
            "legacy Tofu round cap stopped the agent before Harbor's task time "
            "budget; retry with the non-binding round cap"
        )
    elif (
        "compile-compcert" in str(trial.get("task_name", ""))
        and served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and "make proof" in audit_edge_text.lower()
        and "coqc" in audit_edge_text.lower()
        and agent_timeout_multiplier < 8
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "the requested CompCert proof build was still running when the "
            "agent wall clock expired under contended pure TCG; rerun all "
            "attempts for this task at low VM load"
        )
    elif (
        "caffe-cifar-10" in str(trial.get("task_name", ""))
        and served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value == 0.0
        and verifier_report_exists
        and "training did not complete 500 iterations" in lowered
        and "caffe train" in audit_edge_text.lower()
        and re.search(r"iteration\s+3\d\d\b", audit_edge_text.lower())
        and agent_timeout_multiplier < 8
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "the required CIFAR-10 training reached at least iteration 300 but "
            "the agent wall clock expired before iteration 500 under pure TCG; "
            "rerun this task at low VM load with the calibrated timeout"
        )
    elif (
        "mcmc-sampling-stan" in str(trial.get("task_name", ""))
        and served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value is None
        and verifier_elapsed is not None
        and verifier_elapsed >= 7100
        and "100000" in audit_edge_text
        and re.search(r"iteration:\s*[67]0000\s*/\s*100000", audit_edge_text.lower())
        and agent_timeout_multiplier < 8
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "the required 100,000-iteration Stan sampling was still making "
            "progress at 60-70%, and the independent verifier rerun also "
            "exhausted the same pure-TCG wall clock without a reward"
        )
    elif (
        any(
            task in str(trial.get("task_name", ""))
            for task in ("qemu-startup", "qemu-alpine-ssh")
        )
        and served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and "qemu-system-x86_64" in audit_edge_text.lower()
        and (
            "6665 open" in audit_edge_text.lower()
            or (
                "alpine init" in audit_edge_text.lower()
                and "openrc" in audit_edge_text.lower()
            )
        )
        and agent_timeout_multiplier < 16
    ):
        classification = "environment_nested_emulation"
        reason = (
            "the benchmark's inner QEMU was still running and showed guest boot "
            "or forwarded-port progress when the outer QEMU/TCG agent clock expired; "
            "TCG-on-TCG is not timing-equivalent to the Docker reference"
        )
    elif (
        "install-windows-3.11" in str(trial.get("task_name", ""))
        and served_models == [expected_model]
        and reward_value is None
        and "verifiertimeouterror" in lowered
        and "qemu running" in audit_edge_text.lower()
        and "gui screen rendered" in audit_edge_text.lower()
        and "all checks passed" in audit_edge_text.lower()
        and verifier_timeout_multiplier < 16
    ):
        classification = "environment_nested_emulation"
        reason = (
            "the submitted Windows guest had a live QEMU, VNC/web ports, monitor, "
            "keyboard control, and rendered GUI, but its verifier exhausted the "
            "outer pure-TCG clock; retry this TCG-on-TCG task alone"
        )
    elif (
        "hf-model-inference" in str(trial.get("task_name", ""))
        and served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and not verifier_report_exists
        and "connection reset by peer" in audit_edge_text.lower()
        and "huggingface" in audit_edge_text.lower()
        and any(marker in lowered for marker in _NETWORK_MARKERS)
    ):
        classification = "infrastructure_network"
        reason = (
            "the required Hugging Face model route remained unavailable through "
            "the restricted proxy, and verifier dependency download failed on "
            "the same reset before tests could start"
        )
    elif (
        served_models == [expected_model]
        and "addtestsdirerror" in lowered
        and "read-only file system" in exception_text.lower()
        and "emergency_ro" in audit_edge_text.lower()
    ):
        classification = "infrastructure_storage"
        reason = (
            "the qcow2 host file limit was derived from a smaller task storage "
            "hint instead of the inherited backing size, so guest write I/O "
            "forced ext4 into emergency read-only mode before verifier upload"
        )
    elif (
        served_models == [expected_model]
        and "addtestsdirerror" in lowered
        and "guest-file-write" in exception_text.lower()
        and "timeouterror" in exception_text.lower()
    ):
        classification = "infrastructure_transfer"
        reason = (
            "the verifier test bundle timed out while crossing the QEMU guest "
            "agent channel, before any independent tests could run"
        )
    elif (
        served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value is None
        and verifier_elapsed is not None
    ):
        classification = "infrastructure_timeout"
        reason = (
            "agent time expired, but the subsequent verifier also ended without "
            "a numeric reward; the final workspace is unscored and must be retried"
        )
    elif (
        served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value is not None
        and not verifier_report_exists
        and any(marker in lowered for marker in _NETWORK_MARKERS)
        and "/tests/test.sh" in lowered
    ):
        classification = "infrastructure_network"
        reason = (
            "agent time expired, but the independent verifier then failed its "
            "dependency/bootstrap network before producing a test report; the "
            "wrapper's numeric zero is not a semantic verdict on the workspace"
        )
    elif (
        served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value is not None
        and agent_elapsed is not None
        and int(dispatch_audit.get("gate_wait_ms") or 0) >= 120_000
        and int(dispatch_audit.get("gate_wait_ms") or 0)
        >= agent_elapsed * 1000 * 0.05
    ):
        classification = "environment_dispatch_contention"
        reason = (
            "the local global-dispatch gate consumed "
            f"{int(dispatch_audit['gate_wait_ms']) / 1000:.3f} seconds inside "
            "the benchmark agent wall-clock budget; retry without an artificial "
            "model-request queue before assigning a timeout score"
        )
    elif (
        served_models == [expected_model]
        and "agenttimeouterror" in lowered
        and reward_value is not None
    ):
        classification = "model_timeout"
        reason = "agent exhausted the benchmark time budget"
        # reward=1 was accepted above. A numeric non-pass proves that the
        # final workspace failed; without a numeric verifier result the trial
        # remains infrastructure-invalid instead of guessing zero.
        reward_value = 0.0
    elif (
        "configure-git-webserver" in str(trial.get("task_name", ""))
        and "web server returned http 403" in lowered
        and "unix-socket" in audit_text.lower()
        and "0.0.0.0:8080" in audit_text
        and "hello world" in audit_text.lower()
    ):
        classification = "infrastructure_network"
        reason = (
            "legacy rootless proxy settings intercepted a verified localhost "
            "service request"
        )
    elif (
        "dpkg was interrupted" in lowered
        and reward_value is not None
        and not verifier_report_exists
        and "exit_code=124" in audit_text
        and "apt-get install" in audit_text.lower()
        and agent_metadata.get("command_timeout_multiplier") is None
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "legacy pure-TCG command watchdog did not scale the model-requested "
            "package-install timeout"
        )
    elif (
        "dpkg was interrupted" in lowered
        and reward_value is not None
        and not verifier_report_exists
    ):
        classification = "model_environment_damage"
        reason = (
            "agent left dpkg interrupted, so verifier dependencies and tests "
            "could not start"
        )
    elif (
        "build-cython-ext" in str(trial.get("task_name", ""))
        and reward_value is not None
        and verifier_report_exists
        and "repository tests failed" in lowered
        and "file or directory not found" in lowered
        and "git clone" in audit_edge_text.lower()
        and "connect tunnel failed, response 407" in audit_edge_text.lower()
    ):
        classification = "infrastructure_network"
        reason = (
            "legacy authenticated egress proxy blocked Git/libcurl, forcing a "
            "source archive without the repository test directory"
        )
    elif (
        "reshard-c4-data" in str(trial.get("task_name", ""))
        and reward_value is not None
        and verifier_report_exists
        and "couldn't find cache for allenai/c4" in lowered
        and "couldn't be found on the hugging face hub" in lowered
    ):
        classification = "infrastructure_network"
        reason = (
            "the verifier's external C4 fixture download failed and datasets "
            "fell back to an incompatible cached shard before exercising the "
            "submitted compression scripts"
        )
    elif (
        "large-scale-text-editing" in str(trial.get("task_name", ""))
        and reward_value is not None
        and verifier_report_exists
        and "timed out after 600 seconds" in lowered
        and re.search(r"vim exit=0 elapsed=\d+s", audit_edge_text.lower())
        and "byte-for-byte match" in audit_edge_text.lower()
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "the unchanged Vim transformation completed and matched byte-for-byte "
            "during agent validation, but verifier's fixed 600 second subprocess "
            "deadline expired under contended pure TCG"
        )
    elif (
        reward_value is not None
        and "source: not found" in lowered
        and "uvx: not found" in lowered
        and "/tests/test.sh" in lowered
    ):
        classification = "environment_shell_mismatch"
        reason = (
            "legacy direct-runc adapter used POSIX sh for Harbor's Bash main-service "
            "contract, so the verifier bootstrap could not source uv's environment"
        )
    elif (
        reward_value is not None
        and verifier_report_exists
        and "failed to resolve 'localhost'" in lowered
    ):
        classification = "infrastructure_network"
        reason = (
            "legacy direct-runc environment omitted Docker's runtime /etc/hosts "
            "mapping, so the verifier could not resolve localhost"
        )
    elif (
        "rewardfilenotfounderror" in lowered
        and reward_value is None
        and not verifier_report_exists
        and verifier_elapsed is not None
        and verifier_elapsed >= 600
        and re.match(
            r"(?i)^(?:downloading|fetching|resolving)\s+",
            verifier_last_line,
        )
    ):
        classification = "infrastructure_network"
        reason = (
            "verifier dependency bootstrap spent at least ten minutes and "
            "ended mid-download without a test report or reward"
        )
    elif (
        "rewardfilenotfounderror" in lowered
        and reward_value is None
        and not verifier_report_exists
        and verifier_elapsed is not None
        and verifier_elapsed >= 600
        and re.search(
            r"(?im)^(?:=+\s*test session starts\s*=+|collected\s+\d+\s+items?\b|"
            r"\.{0,2}/?tests/[^\n]*test[^\n]*\.py\b)",
            verifier_text,
        )
    ):
        classification = "infrastructure_verifier_stall"
        reason = (
            "verifier dependency bootstrap completed and pytest started, but "
            "the pure-TCG run ended without a test report or reward"
        )
    elif any(marker in lowered for marker in _NETWORK_MARKERS) and (
        reward_value is None or not verifier_report_exists
    ):
        classification = "infrastructure_network"
        reason = "verifier dependency bootstrap failed before a test report existed"
    elif any(marker in lowered for marker in _API_MARKERS) and reward_value is None:
        classification = "infrastructure_api"
        reason = "model gateway or inference scheduler failed transiently"
    elif (
        "rewardfilenotfounderror" in lowered
        and reward_value is None
        and not verifier_report_exists
        and verifier_elapsed is not None
        and verifier_elapsed >= 600
        and not 890 <= verifier_elapsed <= 930
    ):
        classification = "infrastructure_verifier_stall"
        reason = (
            "verifier bootstrap or test execution ran for at least ten minutes "
            "without a test report or reward and without a proven network error"
        )
    elif "prompttoolongerror" in lowered:
        classification = "harness_context_limit"
        reason = (
            "the harness requested more input-plus-completion tokens than the "
            "physical provider context window; correct request budgeting or "
            "context management before retrying"
        )
    elif (
        "rewardfilenotfounderror" in lowered
        and verifier_elapsed is not None
        and 890 <= verifier_elapsed <= 930
    ):
        classification = "infrastructure_timeout"
        reason = "environment exec hit its legacy 900 second inner watchdog"
    elif "cancel-async-tasks" in str(trial.get("task_name", "")) and re.search(
        r">\s*assert\s+stdout\.count\([\"']Task started\.[\"']\)\s*"
        r"==\s*2\s*\nE\s+AssertionError:\s+assert\s+0\s*==\s*2",
        verifier_text,
        re.IGNORECASE,
    ):
        classification = "environment_timing_sensitive"
        reason = "fixed 500ms signal fired before Python started under pure TCG"
    elif not served_models:
        classification = "routing_unverified"
        reason = "no audited model dispatch record was persisted"
    elif (
        reward_value is not None and agent_metadata.get("exit_reason") == "no_progress"
    ):
        classification = "model_timeout"
        reason = (
            "agent exhausted the repeated-progress recovery budget before producing "
            "a verifier-passing artifact"
        )
    elif reward_value is not None:
        classification = "model_semantic"
        reason = "verifier ran and returned a non-passing reward"
    elif "verifiertimeouterror" in lowered and not verifier_stdout_exists:
        classification = "infrastructure_verifier_stall"
        reason = (
            "verifier reached the outer phase deadline without producing its "
            "redirected stdout; retry at the normal budget instead of assuming "
            "that a longer model or verifier budget changes the score"
        )
    elif "verifiertimeouterror" in lowered:
        classification = "infrastructure_timeout"
        reason = "verifier exceeded the local backend time budget"
    else:
        classification = "harness_error"
        reason = exception_type or "trial completed without a numeric reward"

    configured_agent_identifier = agent_config.get("name") or agent_info.get("name")
    configured_profile = profile_for_agent(
        str(configured_agent_identifier) if configured_agent_identifier else None
    )
    underlying_classification: str | None = None
    legacy_timeout_cleanup_bug = (
        str(agent_info.get("version") or "") == "1.0.1"
        and int(dispatch_audit.get("persistent_bash_timeouts") or 0) > 0
        and configured_profile is not None
        and configured_profile.profile_id == "deepseek-minimal"
    )
    if legacy_timeout_cleanup_bug and classification not in {
        "privacy_violation",
        "routing_violation",
        "infrastructure_cancelled",
        "infrastructure_operator_terminated",
    }:
        underlying_classification = classification
        classification = "harness_timeout_process_leak"
        reason = (
            "DeepSeek Minimal host agent 1.0.1 reset only the persistent Bash "
            "leader after a command deadline; foreground descendants in the "
            "same guest process group could continue mutating the workspace, "
            "so this trial is invalid and must be regenerated with fixed "
            "process-group cleanup"
        )
    result = {
        "trial": str(trial.get("trial_name") or trial_dir.name),
        "task": str(trial.get("task_name") or "unknown"),
        "source": str(trial_dir),
        "reward": reward_value,
        "classification": classification,
        "underlying_classification": underlying_classification,
        "reason": reason,
        "exception_type": exception_type or None,
        "agent_elapsed_sec": (
            round(agent_elapsed, 3) if agent_elapsed is not None else None
        ),
        "verifier_elapsed_sec": (
            round(verifier_elapsed, 3) if verifier_elapsed is not None else None
        ),
        "served_models": served_models,
        "raw_reasoning_records": raw_reasoning,
        "dispatches": dispatch_audit["dispatches"],
        "dispatch_429_retries": dispatch_audit["429_retries"],
        "max_dispatch_429_retries": dispatch_audit["max_429_retries"],
        "dispatch_slot_wait_cycles": dispatch_audit["slot_wait_cycles"],
        "dispatch_upstream_429_retries": dispatch_audit["upstream_429_retries"],
        "dispatch_gate_wait_sec": round(dispatch_audit["gate_wait_ms"] / 1000, 3),
        "persistent_bash_timeouts": dispatch_audit["persistent_bash_timeouts"],
        "maximum_assistant_tool_calls": dispatch_audit[
            "maximum_assistant_tool_calls"
        ],
        "agent_exit_reason": agent_metadata.get("exit_reason"),
        "agent_rounds": agent_metadata.get("rounds"),
        "agent_command_count": agent_metadata.get("command_count"),
        "agent_recovery_count": agent_metadata.get("recovery_count"),
        "agent_context_checkpoint_count": agent_metadata.get(
            "context_checkpoint_count"
        ),
        "agent_validation_reuse_count": agent_metadata.get("validation_reuse_count"),
        "agent_timeout_multiplier": agent_timeout_multiplier,
        "verifier_timeout_multiplier": verifier_timeout_multiplier,
        "agent_name": agent_info.get("name"),
        "agent_version": agent_info.get("version"),
        "harness_profile": (
            configured_profile.profile_id if configured_profile is not None else None
        ),
        "credential_boundary": (
            configured_profile.credential_boundary
            if configured_profile is not None
            else None
        ),
        "configured_model": agent_config.get("model_name"),
        "reasoning_effort": agent_kwargs.get("reasoning_effort"),
        "temperature": agent_kwargs.get("temperature"),
        "top_p": agent_kwargs.get("top_p"),
        "max_rounds": agent_kwargs.get("max_rounds"),
        "context_checkpoint_tokens": agent_kwargs.get("context_checkpoint_tokens"),
        "max_output_tokens": agent_kwargs.get("max_output_tokens"),
        "context_window_tokens": agent_kwargs.get("context_window_tokens"),
        "environment_import_path": environment_config.get("import_path"),
        "base_disk_sha256": environment_kwargs.get("base_disk_sha256"),
        "task_checksum": trial.get("task_checksum"),
        "input_tokens": agent_result.get("n_input_tokens")
        if isinstance(agent_result, dict)
        else None,
        "output_tokens": agent_result.get("n_output_tokens")
        if isinstance(agent_result, dict)
        else None,
        "failure_signatures": failure_signatures,
        "operator_control": (
            {
                "action": operator_control.get("action"),
                "cause": operator_control.get("cause"),
            }
            if operator_control
            else None
        ),
    }
    result["attribution"] = _root_cause_attribution(result)
    return result


_PROVENANCE_REQUIREMENTS = (
    ("expected_agent_name", "agent_name"),
    ("expected_agent_version", "agent_version"),
    ("expected_reasoning_effort", "reasoning_effort"),
    ("expected_temperature", "temperature"),
    ("expected_top_p", "top_p"),
    ("expected_max_rounds", "max_rounds"),
    (
        "expected_context_checkpoint_tokens",
        "context_checkpoint_tokens",
    ),
    ("expected_max_output_tokens", "max_output_tokens"),
    ("expected_context_window_tokens", "context_window_tokens"),
    ("expected_environment_import_path", "environment_import_path"),
    ("expected_base_disk_sha256", "base_disk_sha256"),
)


def _profile_provenance_defaults(profile: HarnessProfile) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "expected_agent_name": profile.agent_name,
        "expected_agent_version": profile.agent_version,
        "expected_reasoning_effort": "max",
        "expected_environment_import_path": (
            "rootless_vm.harbor_environment:RootlessQemuEnvironment"
        ),
    }
    if profile.host_dispatch:
        defaults.update(
            {
                "expected_temperature": 1.0,
                "expected_top_p": 0.95,
                "expected_max_rounds": profile.default_max_rounds,
                "expected_max_output_tokens": profile.default_max_output_tokens,
                "expected_context_window_tokens": (
                    profile.default_context_window_tokens
                ),
            }
        )
    if profile.profile_id == "tofu":
        defaults["expected_context_checkpoint_tokens"] = (
            profile.default_context_checkpoint_tokens
        )
    return defaults


def _provenance_mismatches(row: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Return score-affecting harness settings that differ from the run contract."""

    profile_defaults: dict[str, Any] = {}
    if hasattr(args, "harness"):
        profile_defaults = _profile_provenance_defaults(harness_profile(args.harness))
    mismatches = []
    for argument, field in _PROVENANCE_REQUIREMENTS:
        expected = getattr(args, argument, None)
        if expected is None:
            expected = profile_defaults.get(argument)
        if expected is None:
            continue
        actual = row.get(field)
        if field == "agent_version" and _agent_version_score_compatible(
            row, str(expected)
        ):
            continue
        if actual != expected:
            mismatches.append(f"{field}: expected {expected!r}, found {actual!r}")
    return mismatches


def _agent_version_score_compatible(
    row: dict[str, Any], expected_version: str
) -> bool:
    """Accept the one proven path-compatible Minimal cleanup revision.

    Version 1.0.2 changes timeout cleanup and removes a legacy 16-call
    truncation. A 1.0.1 trial is reusable only when its structured audit proves
    that neither changed path was entered.
    """

    actual_version = str(row.get("agent_version") or "")
    if actual_version == expected_version:
        return True
    return (
        expected_version == "1.0.2"
        and actual_version == "1.0.1"
        and row.get("harness_profile") == "deepseek-minimal"
        and int(row.get("dispatches") or 0) > 0
        and int(row.get("persistent_bash_timeouts") or 0) == 0
        # Agent 1.0.1 retained at most 16 calls. A recorded maximum below the
        # boundary proves that the removed truncation path was not entered.
        and int(row.get("maximum_assistant_tool_calls") or 0) < 16
    )


def _expected_agent_version(args: argparse.Namespace) -> str | None:
    expected = getattr(args, "expected_agent_version", None)
    if expected is not None:
        return str(expected)
    harness = getattr(args, "harness", None)
    if harness is None:
        return None
    value = harness_profile(harness).agent_version
    return str(value) if value is not None else None


def _compatible_legacy_agent_trial_count(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> int:
    expected = _expected_agent_version(args)
    if expected is None:
        return 0
    return sum(
        1
        for row in rows
        if str(row.get("agent_version") or "") != expected
        and _agent_version_score_compatible(row, expected)
    )


def _task_checksum_mismatch(
    row: dict[str, Any], expected: dict[str, str]
) -> str | None:
    checksum = expected.get(row["task"])
    if checksum is None:
        return None
    actual = row.get("task_checksum")
    if actual == checksum:
        return None
    return f"task_checksum: expected {checksum!r}, found {actual!r}"


def _observations(args: argparse.Namespace) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    resolved_jobs: set[Path] = set()
    for job_value in args.jobs:
        job = Path(job_value).expanduser().resolve(strict=True)
        if job in resolved_jobs:
            raise ValueError(f"duplicate job directory: {job}")
        resolved_jobs.add(job)
        for result_path in sorted(job.glob("*/result.json")):
            try:
                value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and "task_name" in value:
                observations.append(
                    _classify_trial(value, result_path.parent, args.expected_model)
                )
    return observations


def analyze(args: argparse.Namespace) -> int:
    observations = _observations(args)
    counts: dict[str, int] = {}
    root_causes: dict[str, int] = {}
    harness_counts: dict[str, int] = {}
    for row in observations:
        label = row["classification"]
        counts[label] = counts.get(label, 0) + 1
        layer = str((row.get("attribution") or {}).get("layer") or "unknown")
        root_causes[layer] = root_causes.get(layer, 0) + 1
        harness = str(row.get("harness_profile") or "unknown")
        harness_counts[harness] = harness_counts.get(harness, 0) + 1
    routed = [row for row in observations if row["served_models"]]
    exact_routes = [
        row for row in routed if row["served_models"] == [args.expected_model]
    ]
    mismatched_routes = len(routed) - len(exact_routes)
    audited_routes_pure = bool(routed) and mismatched_routes == 0
    payload = {
        "trials": len(observations),
        "classifications": dict(sorted(counts.items())),
        "root_cause_layers": dict(sorted(root_causes.items())),
        "harnesses": dict(sorted(harness_counts.items())),
        "audited_route_trials": len(routed),
        "exact_route_trials": len(exact_routes),
        "unaudited_route_trials": len(observations) - len(routed),
        "mismatched_route_trials": mismatched_routes,
        "audited_routes_pure": audited_routes_pure,
        "route_pure": (
            bool(observations)
            and len(routed) == len(observations)
            and audited_routes_pure
        ),
        "raw_reasoning_records": sum(
            row["raw_reasoning_records"] for row in observations
        ),
        "provider_dispatches": sum(
            int(row.get("dispatches") or 0) for row in observations
        ),
        "provider_429_retries": sum(
            int(row.get("dispatch_429_retries") or 0) for row in observations
        ),
        "local_slot_wait_cycles": sum(
            int(row.get("dispatch_slot_wait_cycles") or 0) for row in observations
        ),
        "upstream_429_retries": sum(
            int(row.get("dispatch_upstream_429_retries") or 0) for row in observations
        ),
        "max_dispatch_429_retries": max(
            (int(row.get("max_dispatch_429_retries") or 0) for row in observations),
            default=0,
        ),
        "dispatch_gate_wait_sec": round(
            sum(float(row.get("dispatch_gate_wait_sec") or 0) for row in observations),
            3,
        ),
        "details": observations,
    }
    output_value = getattr(args, "output", None)
    if output_value:
        output = Path(output_value).expanduser().resolve()
        _private_dir(output.parent)
        _atomic_json(output, payload)
        print(f"ANALYSIS ARTIFACT {output}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _trial_profile(
    trial: dict[str, Any], observation: dict[str, Any]
) -> HarnessProfile | None:
    config = trial.get("config") if isinstance(trial.get("config"), dict) else {}
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    agent_info = (
        trial.get("agent_info") if isinstance(trial.get("agent_info"), dict) else {}
    )
    identifier = agent.get("name") or agent_info.get("name")
    profile = profile_for_agent(str(identifier) if identifier else None)
    if profile is None and observation.get("harness_profile"):
        profile = harness_profile(str(observation["harness_profile"]))
    return profile


def _fallback_host_trajectory(
    trial_dir: Path,
    trial: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    transcript_path = _host_transcript_path(trial_dir)
    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(transcript, list):
        return None
    profile = _trial_profile(trial, observation)
    profile_id = profile.profile_id if profile is not None else "unknown"
    system_prompt = (
        "You are a helpful software engineer assistant."
        if profile_id == "deepseek-minimal"
        else "[system prompt unavailable in legacy host audit]"
    )
    instruction = trial.get("instruction") or trial.get("task_description")
    if not isinstance(instruction, str) or not instruction:
        instruction = (
            "[instruction unavailable in legacy host audit for "
            + str(trial.get("task_name") or trial_dir.name)
            + "]"
        )
    return host_audit_to_atif(
        transcript,
        instruction=instruction,
        system_prompt=system_prompt,
        # A historical trial remains attributable to the version it actually
        # recorded even after the local harness registry advances.
        agent_name=str(
            observation.get("agent_name")
            or (profile.agent_name if profile is not None else "unknown")
        ),
        agent_version=str(
            observation.get("agent_version")
            or (
                profile.agent_version
                if profile is not None and profile.agent_version is not None
                else "unknown"
            )
        ),
        model_name=str(observation.get("configured_model") or "unknown"),
        tool_definitions=[],
        session_id=str(trial.get("trial_name") or trial_dir.name),
        credential_boundary=(
            profile.credential_boundary if profile is not None else "unknown"
        ),
        harness_profile=profile_id,
    )


def _artifact_id(observation: dict[str, Any]) -> str:
    task = re.sub(r"[^A-Za-z0-9._-]+", "-", str(observation["task"]))[-80:]
    trial = re.sub(r"[^A-Za-z0-9._-]+", "-", str(observation["trial"]))[-80:]
    digest = hashlib.sha256(str(observation["source"]).encode()).hexdigest()[:12]
    return f"{task}__{trial}__{digest}"


def collect_trajectories(args: argparse.Namespace) -> int:
    """Create a privacy-safe ATIF bundle plus per-trial root-cause records."""

    output_root = _private_dir(args.output_root)
    manifest_path = output_root / "manifest.jsonl"
    summary_path = output_root / "summary.json"
    if manifest_path.exists() or summary_path.exists():
        raise ValueError("trajectory output root already contains a collection")
    trials_root = output_root / "trials"
    if trials_root.exists():
        raise ValueError("trajectory output root already contains trial artifacts")
    trials_root.mkdir(mode=0o700)
    observations = _observations(args)
    manifest_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    classifications: dict[str, int] = {}
    root_causes: dict[str, int] = {}
    for observation in observations:
        trial_dir = Path(observation["source"])
        trial = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
        artifact_id = _artifact_id(observation)
        artifact_root = trials_root / artifact_id
        artifact_root.mkdir(mode=0o700, exist_ok=False)
        source_trajectory = trial_dir / "agent" / "trajectory.json"
        trajectory_status = "missing"
        trajectory_error: str | None = None
        trajectory: dict[str, Any] | None = None
        if source_trajectory.is_file():
            try:
                loaded = json.loads(source_trajectory.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("trajectory root must be a JSON object")
                validate_atif(loaded)
                trajectory = loaded
                trajectory_status = "native_atif"
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                trajectory_error = str(exc)
                trajectory_status = "invalid_atif"
        if trajectory is None:
            fallback = _fallback_host_trajectory(trial_dir, trial, observation)
            if fallback is not None:
                trajectory = fallback
                trajectory_status = "projected_host_audit"
                trajectory_error = None
        collected_path: str | None = None
        if trajectory is not None:
            target = artifact_root / "trajectory.json"
            host_transcript: list[dict[str, Any]] | None = None
            try:
                loaded_transcript = json.loads(
                    _host_transcript_path(trial_dir).read_text(encoding="utf-8")
                )
                if isinstance(loaded_transcript, list) and all(
                    isinstance(row, dict) for row in loaded_transcript
                ):
                    host_transcript = loaded_transcript
            except (OSError, json.JSONDecodeError):
                pass
            write_collected_trajectory(
                target, trajectory, host_transcript=host_transcript
            )
            collected_path = str(target.relative_to(output_root))

        attribution_payload = {
            "schema": 1,
            "trial": observation["trial"],
            "task": observation["task"],
            "reward": observation["reward"],
            "harness_profile": observation.get("harness_profile"),
            "credential_boundary": observation.get("credential_boundary"),
            "attribution": observation["attribution"],
            "failure_signatures": observation.get("failure_signatures") or [],
            "exception_type": observation.get("exception_type"),
        }
        attribution_path = artifact_root / "attribution.json"
        _atomic_json(attribution_path, attribution_payload)
        row = {
            "schema": 1,
            "artifact_id": artifact_id,
            "trial": observation["trial"],
            "task": observation["task"],
            "source_trial": str(trial_dir),
            "harness_profile": observation.get("harness_profile"),
            "model": observation.get("configured_model"),
            "reward": observation["reward"],
            "classification": observation["classification"],
            "root_cause_layer": observation["attribution"]["layer"],
            "trajectory_status": trajectory_status,
            "trajectory_error": trajectory_error,
            "trajectory_path": collected_path,
            "attribution_path": str(attribution_path.relative_to(output_root)),
        }
        manifest_rows.append(row)
        status_counts[trajectory_status] = status_counts.get(trajectory_status, 0) + 1
        label = observation["classification"]
        classifications[label] = classifications.get(label, 0) + 1
        layer = observation["attribution"]["layer"]
        root_causes[layer] = root_causes.get(layer, 0) + 1

    temporary = output_root / ".manifest.jsonl.partial"
    temporary.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in manifest_rows
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, manifest_path)
    _atomic_json(
        summary_path,
        {
            "schema": 1,
            "trials": len(manifest_rows),
            "trajectory_status": dict(sorted(status_counts.items())),
            "classifications": dict(sorted(classifications.items())),
            "root_cause_layers": dict(sorted(root_causes.items())),
            "manifest": manifest_path.name,
            "reasoning_policy": "explicit reasoning removed from collected copies",
        },
    )
    print(summary_path)
    return 0


def score(args: argparse.Namespace) -> int:
    if args.expected_tasks < 1 or args.expected_attempts < 1:
        raise ValueError("expected task and attempt counts must be positive")
    expected_names: set[str] | None = None
    expected_checksums: dict[str, str] = {}
    if args.tasks_root:
        expected_task_rows = _load_tasks(args.tasks_root)
        expected_names = {f"terminal-bench/{task.name}" for task in expected_task_rows}
        if len(expected_names) != args.expected_tasks:
            raise ValueError(
                "expected task count does not match the pinned dataset checkout"
            )
        if len(expected_task_rows) == TASK_COUNT:
            expected_checksums = _load_frozen_task_checksums(expected_task_rows)
    observations = _observations(args)
    tasks: dict[str, list[dict[str, Any]]] = {}
    invalid_counts: dict[str, int] = {}
    provenance_violations: dict[str, int] = {}
    raw_reward = 0.0
    for row in observations:
        raw_reward += float(row["reward"] or 0.0)
        if row["classification"] in _VALID_SCORE_LABELS:
            mismatches = _provenance_mismatches(row, args)
            task_mismatch = _task_checksum_mismatch(row, expected_checksums)
            if task_mismatch or mismatches:
                label = (
                    "task_provenance_violation"
                    if task_mismatch
                    else "harness_provenance_violation"
                )
                invalid_counts[label] = invalid_counts.get(label, 0) + 1
                all_mismatches = list(mismatches)
                if task_mismatch:
                    all_mismatches.append(task_mismatch)
                for mismatch in all_mismatches:
                    provenance_violations[mismatch] = (
                        provenance_violations.get(mismatch, 0) + 1
                    )
            else:
                tasks.setdefault(row["task"], []).append(row)
        else:
            label = row["classification"]
            invalid_counts[label] = invalid_counts.get(label, 0) + 1
    valid_trials = sum(len(rows) for rows in tasks.values())
    valid_reward = sum(
        float(row["reward"] or 0.0) for rows in tasks.values() for row in rows
    )
    expected_trials = args.expected_tasks * args.expected_attempts
    surplus = {
        name: len(rows) - args.expected_attempts
        for name, rows in tasks.items()
        if len(rows) > args.expected_attempts
    }
    unexpected_tasks = (
        sorted(set(tasks) - expected_names) if expected_names is not None else []
    )
    task_checksum_sets: dict[str, set[str]] = {}
    for name, rows in tasks.items():
        for row in rows:
            checksum = row.get("task_checksum")
            if isinstance(checksum, str) and checksum:
                task_checksum_sets.setdefault(name, set()).add(checksum)
    task_checksum_conflicts = {
        name: sorted(checksums)
        for name, checksums in task_checksum_sets.items()
        if len(checksums) > 1
    }
    coverage_complete = (
        len(tasks) == args.expected_tasks
        and all(len(rows) == args.expected_attempts for rows in tasks.values())
        and not unexpected_tasks
        and not task_checksum_conflicts
        and (expected_names is None or set(tasks) == expected_names)
    )
    payload = {
        "evaluation_mode": (
            "leaderboard_candidate"
            if args.expected_attempts >= LEADERBOARD_MINIMUM_ATTEMPTS
            else "smoke"
        ),
        "leaderboard_minimum_attempts": LEADERBOARD_MINIMUM_ATTEMPTS,
        "leaderboard_attempt_contract_met": (
            args.expected_attempts >= LEADERBOARD_MINIMUM_ATTEMPTS
        ),
        "observed_trials": len(observations),
        "raw_score_percent": (
            100 * raw_reward / len(observations) if observations else None
        ),
        "valid_trials": valid_trials,
        "expected_trials": expected_trials,
        "coverage_complete": coverage_complete,
        "surplus_valid_trials": dict(sorted(surplus.items())),
        "unexpected_tasks": unexpected_tasks,
        "task_checksum_conflicts": dict(sorted(task_checksum_conflicts.items())),
        "provenance_violations": dict(sorted(provenance_violations.items())),
        "compatible_legacy_agent_trials": _compatible_legacy_agent_trial_count(
            [row for rows in tasks.values() for row in rows], args
        ),
        "provisional_valid_score_percent": (
            100 * valid_reward / valid_trials if valid_trials else None
        ),
        "score_percent": (
            100 * valid_reward / expected_trials if coverage_complete else None
        ),
        "invalid_trials": dict(sorted(invalid_counts.items())),
        "per_task": {
            name: {
                "valid_attempts": len(rows),
                "mean": (
                    sum(float(row["reward"] or 0.0) for row in rows) / len(rows)
                    if rows
                    else None
                ),
            }
            for name, rows in sorted(tasks.items())
        },
    }
    output_value = getattr(args, "output", None)
    if output_value:
        output = Path(output_value).expanduser().resolve()
        _private_dir(output.parent)
        _atomic_json(output, payload)
        print(f"SCORE ARTIFACT {output}", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _build_retry_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Build unbiased retry groups needed to reach exact k-shot coverage."""

    if args.expected_attempts < 1:
        raise ValueError("expected attempt count must be positive")
    expected_tasks = _load_tasks(args.tasks_root)
    tasks_by_name = {f"terminal-bench/{task.name}": task for task in expected_tasks}
    expected_names = set(tasks_by_name)
    expected_checksums = (
        _load_frozen_task_checksums(expected_tasks)
        if len(expected_tasks) == TASK_COUNT
        else {}
    )
    observations = _observations(args)
    valid_counts: dict[str, int] = {}
    invalid_counts: dict[str, int] = {}
    provenance_violations: dict[str, int] = {}
    invalid_by_task: dict[str, set[str]] = {}
    unscored_agent_timeout_tasks: set[str] = set()
    compatible_legacy_rows: list[dict[str, Any]] = []
    for row in observations:
        if row["classification"] in _VALID_SCORE_LABELS:
            mismatches = _provenance_mismatches(row, args)
            task_mismatch = _task_checksum_mismatch(row, expected_checksums)
            if task_mismatch or mismatches:
                label = (
                    "task_provenance_violation"
                    if task_mismatch
                    else "harness_provenance_violation"
                )
                invalid_counts[label] = invalid_counts.get(label, 0) + 1
                invalid_by_task.setdefault(row["task"], set()).add(label)
                all_mismatches = list(mismatches)
                if task_mismatch:
                    all_mismatches.append(task_mismatch)
                for mismatch in all_mismatches:
                    provenance_violations[mismatch] = (
                        provenance_violations.get(mismatch, 0) + 1
                    )
            else:
                task = row["task"]
                valid_counts[task] = valid_counts.get(task, 0) + 1
                compatible_legacy_rows.append(row)
        else:
            label = row["classification"]
            invalid_counts[label] = invalid_counts.get(label, 0) + 1
            invalid_by_task.setdefault(row["task"], set()).add(label)
            underlying_label = row.get("underlying_classification")
            if isinstance(underlying_label, str) and underlying_label:
                invalid_by_task[row["task"]].add(underlying_label)
            if (
                label == "infrastructure_timeout"
                and str(row.get("exception_type") or "").lower() == "agenttimeouterror"
            ):
                unscored_agent_timeout_tasks.add(row["task"])

    unexpected_tasks = sorted(set(valid_counts) - expected_names)
    surplus = {
        name: count - args.expected_attempts
        for name, count in valid_counts.items()
        if name in expected_names and count > args.expected_attempts
    }
    grouped: dict[int, list[str]] = {}
    for name in sorted(expected_names):
        missing = max(0, args.expected_attempts - valid_counts.get(name, 0))
        if missing:
            grouped.setdefault(missing, []).append(name)
    profile_specs = {
        "standard": {
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 4,
            "max_concurrent_trials": 32,
            "agent_concurrency": 16,
        },
        "verifier_heavy": {
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 8,
            "max_concurrent_trials": 8,
            "agent_concurrency": 8,
        },
        "tcg_low_load": {
            "agent_timeout_multiplier": 8,
            "verifier_timeout_multiplier": 8,
            "max_concurrent_trials": 2,
            "agent_concurrency": 2,
        },
        "tcg_clock_calibrated": {
            "agent_timeout_multiplier": 8,
            "verifier_timeout_multiplier": 8,
            "max_concurrent_trials": 1,
            "agent_concurrency": 1,
            "virtual_time_shift": 0,
        },
        "nested_emulation": {
            "agent_timeout_multiplier": 16,
            "verifier_timeout_multiplier": 16,
            "max_concurrent_trials": 1,
            "agent_concurrency": 1,
        },
    }
    profile_groups: dict[tuple[str, int], list[str]] = {}
    for attempts, names in grouped.items():
        for name in names:
            labels = invalid_by_task.get(name, set())
            timeout_cleanup_nested_tasks = {
                "terminal-bench/install-windows-3.11",
                "terminal-bench/qemu-alpine-ssh",
                "terminal-bench/qemu-startup",
            }
            if "environment_nested_emulation" in labels or (
                "harness_timeout_process_leak" in labels
                and name in timeout_cleanup_nested_tasks
            ):
                profile = "nested_emulation"
            elif (
                name == "terminal-bench/cancel-async-tasks"
                and "environment_timing_sensitive" in labels
            ):
                # Its official verifier sends SIGINT after a fixed 500 ms.
                # Pure TCG needs an instruction-counted guest clock; lowering
                # host load alone cannot make Python start in that guest-time
                # window reproducibly.
                profile = "tcg_clock_calibrated"
            elif (
                "environment_timing_sensitive" in labels
                or "environment_dispatch_contention" in labels
                or name in unscored_agent_timeout_tasks
            ):
                profile = "tcg_low_load"
            elif labels & {
                "infrastructure_timeout",
                "infrastructure_verifier_stall",
            }:
                profile = "verifier_heavy"
            else:
                profile = "standard"
            profile_groups.setdefault((profile, attempts), []).append(name)
    retry_profiles = []
    for (profile, attempts), names in sorted(profile_groups.items()):
        spec = dict(profile_specs[profile])
        # Keep retry plans executable under the same hard 24-hour watchdog as
        # write_config(). Some TB2.1 tasks already grant a four-hour verifier
        # budget, so blindly applying the verifier-heavy 8x profile would
        # produce a plan that configuration validation must reject.
        max_verifier_timeout = max(
            tasks_by_name[name].verifier_timeout_sec for name in names
        )
        safe_multiplier = max(1, math.floor(86400 / max_verifier_timeout))
        spec["verifier_timeout_multiplier"] = min(
            spec["verifier_timeout_multiplier"], safe_multiplier
        )
        retry_profiles.append(
            {
                "profile": profile,
                "attempts": attempts,
                "tasks": names,
                **spec,
            }
        )
    return {
        "complete": not grouped and not surplus and not unexpected_tasks,
        "expected_attempts": args.expected_attempts,
        "valid_trials": sum(valid_counts.values()),
        "missing_valid_trials": sum(
            attempts * len(names) for attempts, names in grouped.items()
        ),
        "retry_groups": [
            {"attempts": attempts, "tasks": names}
            for attempts, names in sorted(grouped.items())
        ],
        "retry_profiles": retry_profiles,
        "surplus_valid_trials": dict(sorted(surplus.items())),
        "unexpected_tasks": unexpected_tasks,
        "invalid_trials": dict(sorted(invalid_counts.items())),
        "provenance_violations": dict(sorted(provenance_violations.items())),
        "compatible_legacy_agent_trials": _compatible_legacy_agent_trial_count(
            compatible_legacy_rows, args
        ),
    }


def plan_retries(args: argparse.Namespace) -> int:
    """Report unbiased retry groups needed to reach exact k-shot coverage."""

    payload = _build_retry_plan(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _retry_template(args: argparse.Namespace) -> dict[str, Any]:
    template_path = Path(args.template).expanduser().resolve(strict=True)
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid Harbor template JSON: {template_path}") from exc
    if not isinstance(template, dict):
        raise ValueError("Harbor retry template must be a JSON object")
    agents = template.get("agents")
    if not isinstance(agents, list) or len(agents) != 1:
        raise ValueError("Harbor retry template must configure exactly one agent")
    agent = agents[0]
    if not isinstance(agent, dict) or agent.get("model_name") != args.expected_model:
        raise ValueError("Harbor retry template model differs from the score contract")
    template_trial_concurrency = template.get("n_concurrent_trials")
    if (
        not isinstance(template_trial_concurrency, int)
        or isinstance(template_trial_concurrency, bool)
        or template_trial_concurrency < 1
    ):
        raise ValueError("Harbor retry template has invalid trial concurrency")
    template_agent_concurrency = agent.get("n_concurrent")
    if (
        not isinstance(template_agent_concurrency, int)
        or isinstance(template_agent_concurrency, bool)
        or not 1 <= template_agent_concurrency <= template_trial_concurrency
    ):
        raise ValueError("Harbor retry template has invalid agent concurrency")
    environment = template.get("environment")
    if not isinstance(environment, dict) or not isinstance(
        environment.get("kwargs"), dict
    ):
        raise ValueError("Harbor retry template is missing environment kwargs")
    jobs_dir_value = template.get("jobs_dir")
    if not isinstance(jobs_dir_value, str):
        raise ValueError("Harbor retry template is missing jobs_dir")
    _private_dir(jobs_dir_value)
    return template


def write_retry_configs(args: argparse.Namespace) -> int:
    """Freeze the current ledger's exact missing trials into runnable configs."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", args.job_prefix):
        raise ValueError(
            "job prefix must be 1-101 safe filename characters and contain no path"
        )
    plan = _build_retry_plan(args)
    if plan["surplus_valid_trials"] or plan["unexpected_tasks"]:
        raise ValueError(
            "cannot generate an exact retry series from a ledger with surplus "
            "or unexpected valid trials"
        )
    template = _retry_template(args)
    output_root = _private_dir(args.output_root)
    task_rows = _load_tasks(args.tasks_root)
    tasks = {f"terminal-bench/{task.name}": task for task in task_rows}
    jobs_dir = Path(template["jobs_dir"]).expanduser().resolve(strict=True)
    manifest = output_root / f"{args.job_prefix}-manifest.json"
    if manifest.exists() or manifest.is_symlink():
        raise ValueError(f"refusing to overwrite retry manifest: {manifest}")
    configs: list[str] = []
    job_names: list[str] = []
    pending: list[tuple[Path, dict[str, Any]]] = []
    for index, profile in enumerate(plan["retry_profiles"], start=1):
        profile_name = str(profile["profile"])
        attempts = int(profile["attempts"])
        job_name = f"{args.job_prefix}-{index:02d}-{profile_name}-a{attempts}"
        output = output_root / f"{job_name}.json"
        if output.exists() or output.is_symlink() or (jobs_dir / job_name).exists():
            raise ValueError(f"refusing to overwrite retry job: {job_name}")
        selected = [tasks[name] for name in profile["tasks"]]
        trial_concurrency = min(
            int(profile["max_concurrent_trials"]),
            int(template["n_concurrent_trials"]),
            attempts * len(selected),
        )
        agent_concurrency = min(
            int(profile["agent_concurrency"]),
            int(template["agents"][0]["n_concurrent"]),
            trial_concurrency,
        )
        agent_multiplier = float(profile["agent_timeout_multiplier"])
        verifier_multiplier = float(profile["verifier_timeout_multiplier"])
        default_exec_timeout_sec = max(
            900.0,
            max(task.verifier_timeout_sec for task in selected) * verifier_multiplier,
        )
        if default_exec_timeout_sec > 86400:
            raise ValueError("scaled verifier timeout exceeds the 24 hour safety cap")

        config = copy.deepcopy(template)
        config["job_name"] = job_name
        config["n_attempts"] = attempts
        config["n_concurrent_trials"] = trial_concurrency
        config["agent_timeout_multiplier"] = agent_multiplier
        config["verifier_timeout_multiplier"] = verifier_multiplier
        config["retry"] = {"max_retries": 0}
        config["tasks"] = [{"path": str(task.path)} for task in selected]
        config["agents"][0]["n_concurrent"] = agent_concurrency
        agent_kwargs = config["agents"][0].setdefault("kwargs", {})
        agent_identifier = config["agents"][0].get("name")
        selected_harness = (
            harness_profile("tofu")
            if agent_identifier is None
            else profile_for_agent(agent_identifier)
        )
        if selected_harness is None:
            raise ValueError("Harbor retry template uses an unregistered harness")
        if selected_harness.profile_id == "tofu":
            agent_kwargs["command_timeout_multiplier"] = agent_multiplier
            agent_kwargs["command_timeout_sec"] = min(
                1800, round(120 * agent_multiplier)
            )
        elif selected_harness.profile_id == "deepseek-minimal":
            agent_kwargs["bash_timeout_sec"] = min(1800, round(300 * agent_multiplier))
        environment_kwargs = config["environment"]["kwargs"]
        environment_kwargs["default_exec_timeout_sec"] = default_exec_timeout_sec
        environment_kwargs["verifier_timeout_multiplier"] = verifier_multiplier
        environment_kwargs["virtual_time_shift"] = profile.get("virtual_time_shift")
        configs.append(str(output))
        job_names.append(job_name)
        pending.append((output, config))

    created: list[Path] = []
    try:
        for output, config in pending:
            _atomic_json(output, config)
            created.append(output)
        _atomic_json(
            manifest,
            {
                "schema": 1,
                "expected_attempts": plan["expected_attempts"],
                "valid_trials": plan["valid_trials"],
                "missing_valid_trials": plan["missing_valid_trials"],
                "invalid_trials": plan["invalid_trials"],
                "provenance_violations": plan["provenance_violations"],
                "compatible_legacy_agent_trials": plan.get(
                    "compatible_legacy_agent_trials", 0
                ),
                "source_jobs": [
                    str(Path(value).expanduser().resolve(strict=True))
                    for value in args.jobs
                ],
                "job_names": job_names,
                "configs": configs,
            },
        )
    except Exception:
        for output in created:
            output.unlink(missing_ok=True)
        raise
    print(manifest)
    for config in configs:
        print(config)
    return 0


def run_until_complete(args: argparse.Namespace) -> int:
    """Run audited replacement waves until the ledger has exact coverage."""

    if not 1 <= args.max_waves <= 100:
        raise ValueError("max waves must be between 1 and 100")
    jobs = [str(Path(value).expanduser().resolve(strict=True)) for value in args.jobs]
    jobs_dir = Path(_retry_template(args)["jobs_dir"]).expanduser().resolve(strict=True)
    for wave in range(args.max_waves + 1):
        args.jobs = jobs
        plan = _build_retry_plan(args)
        _say(
            "AUTOFILL PLAN "
            f"wave={wave} valid={plan['valid_trials']} "
            f"missing={plan['missing_valid_trials']}"
        )
        if plan["surplus_valid_trials"] or plan["unexpected_tasks"]:
            raise ValueError("autofill ledger has surplus or unexpected valid trials")
        if plan["complete"]:
            _say(f"AUTOFILL COMPLETE valid={plan['valid_trials']}")
            return 0
        if wave == args.max_waves:
            raise RuntimeError(
                f"exact coverage still missing after {args.max_waves} waves"
            )
        wave_prefix = f"{args.job_prefix}-w{wave + 1:02d}"
        writer_args = copy.copy(args)
        writer_args.job_prefix = wave_prefix
        write_retry_configs(writer_args)
        manifest_path = Path(args.output_root).expanduser().resolve(strict=True) / (
            f"{wave_prefix}-manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        configs = manifest.get("configs")
        job_names = manifest.get("job_names")
        if not isinstance(configs, list) or not configs:
            raise RuntimeError("incomplete autofill plan emitted no configs")
        if not isinstance(job_names, list) or len(job_names) != len(configs):
            raise RuntimeError("autofill manifest has inconsistent job names")
        exit_code = run_series(argparse.Namespace(harbor=args.harbor, config=configs))
        if exit_code:
            return exit_code
        new_jobs = [jobs_dir / str(name) for name in job_names]
        missing_jobs = [path for path in new_jobs if not path.is_dir()]
        if missing_jobs:
            raise RuntimeError(f"autofill jobs were not created: {missing_jobs}")
        jobs.extend(str(path.resolve(strict=True)) for path in new_jobs)


def _add_provenance_arguments(parser: argparse.ArgumentParser) -> None:
    """Pin score-affecting settings for one comparable harness ledger."""

    parser.add_argument("--harness", choices=harness_profile_ids(), default="tofu")
    parser.add_argument("--expected-agent-name")
    parser.add_argument("--expected-agent-version")
    parser.add_argument("--expected-reasoning-effort")
    parser.add_argument("--expected-temperature", type=float)
    parser.add_argument("--expected-top-p", type=float)
    parser.add_argument("--expected-max-rounds", type=int)
    parser.add_argument("--expected-context-checkpoint-tokens", type=int)
    parser.add_argument("--expected-max-output-tokens", type=int)
    parser.add_argument("--expected-context-window-tokens", type=int)
    parser.add_argument("--expected-environment-import-path")
    parser.add_argument("--expected-base-disk-sha256")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    harnesses = subparsers.add_parser(
        "list-harnesses", help="print harness identity and credential contracts"
    )
    harnesses.set_defaults(func=list_harnesses)
    prepare = subparsers.add_parser("prepare-assets")
    prepare.add_argument("--tasks-root", required=True)
    prepare.add_argument("--assets-root", required=True)
    prepare.add_argument("--crane", required=True)
    prepare.add_argument(
        "--registry-mirror",
        action="append",
        default=[],
        help=(
            "Docker Hub mirror host to try before the origin; repeatable and "
            "recorded in each asset manifest"
        ),
    )
    prepare.add_argument("--archive-tool", required=True)
    prepare.add_argument(
        "--genisoimage",
        help="optional executable; omit to use pinned pycdlib without root",
    )
    prepare.add_argument("--workers", type=int, choices=range(1, 17))
    prepare.add_argument("--task", action="append", help="prepare one named task")
    prepare.set_defaults(func=prepare_assets)

    cache = subparsers.add_parser("prepare-cache")
    cache.add_argument("--tasks-root", required=True)
    cache.add_argument("--assets-root", required=True)
    cache.add_argument("--cache-root", required=True)
    cache.add_argument("--base-disk", required=True)
    cache.add_argument("--qemu", required=True)
    cache.add_argument("--qemu-img", required=True)
    cache.add_argument("--workers", type=int, choices=range(1, 17))
    cache.add_argument("--prepare-timeout-sec", type=float, default=3600.0)
    cache.add_argument("--task", action="append", help="prepare one named task")
    cache.set_defaults(func=prepare_cache)

    config = subparsers.add_parser("write-config")
    config.add_argument("--tasks-root", required=True)
    config.add_argument("--assets-root", required=True)
    config.add_argument("--control-root", required=True)
    config.add_argument("--state-root", required=True)
    config.add_argument("--cache-root", required=True)
    config.add_argument("--jobs-dir", required=True)
    config.add_argument("--base-disk", required=True)
    config.add_argument("--qemu", required=True)
    config.add_argument("--qemu-img", required=True)
    config.add_argument("--job-name", required=True)
    config.add_argument("--attempts", type=int, default=1)
    config.add_argument(
        "--concurrency",
        type=int,
        help=(
            "live trial VM cap; omitted uses one CPU/memory/headroom/disk probe "
            "with a conservative four-VM ceiling"
        ),
    )
    config.add_argument(
        "--agent-concurrency",
        type=int,
        help="cap concurrent model agents independently of live trial VMs",
    )
    config.add_argument("--max-retries", type=int, default=2)
    config.add_argument("--egress-max-gib", type=int, default=16)
    config.add_argument(
        "--egress-global-concurrency",
        type=int,
        default=16,
        help="host-wide upstream connection cap shared across trial VMs",
    )
    config.add_argument("--model", default="deepseek-v4-flash-yourprovider")
    config.add_argument(
        "--harness",
        choices=harness_profile_ids(),
        default="tofu",
        help="agent harness profile; use list-harnesses for security boundaries",
    )
    config.add_argument(
        "--allow-guest-credentials",
        action="store_true",
        help=(
            "authorize a guest-installed harness to receive explicitly scoped, "
            "short-lived model credentials"
        ),
    )
    config.add_argument(
        "--reasoning-effort", choices=("low", "high", "max"), default="max"
    )
    config.add_argument("--temperature", type=float, default=1.0)
    config.add_argument("--top-p", type=float, default=0.95)
    config.add_argument(
        "--max-rounds",
        type=int,
        help="override the selected harness profile's safety ceiling",
    )
    config.add_argument(
        "--max-output-tokens",
        type=int,
        help="override the selected harness profile's provider output budget",
    )
    config.add_argument(
        "--context-checkpoint-tokens",
        type=int,
        help="Tofu-only history checkpoint threshold",
    )
    config.add_argument("--dispatch-max-retries", type=int, default=8)
    config.add_argument(
        "--global-dispatch-concurrency",
        type=int,
        default=4,
        help="host-wide model-call cap shared across overlapping Harbor jobs",
    )
    config.add_argument(
        "--runtime-timeout-multiplier",
        type=float,
        default=4.0,
        help="scale agent/verifier wall-clock limits for pure-TCG execution",
    )
    config.add_argument(
        "--verifier-timeout-multiplier",
        type=float,
        help=(
            "override only the verifier wall-clock scale for unusually slow "
            "pure-TCG tasks; agent/tool budgets keep the runtime multiplier"
        ),
    )
    config.add_argument(
        "--virtual-time-shift",
        type=int,
        choices=range(0, 11),
        help="use QEMU icount to calibrate sub-second guest timing under TCG",
    )
    config.add_argument("--task", action="append", help="limit to a named smoke task")
    config.set_defaults(func=write_config)

    run = subparsers.add_parser("run")
    run.add_argument("--harbor", required=True)
    run.add_argument("--config", required=True)
    run.set_defaults(func=run_config)

    series = subparsers.add_parser("run-series")
    series.add_argument("--harbor", required=True)
    series.add_argument("--config", action="append", required=True)
    series.set_defaults(func=run_series)

    scorer = subparsers.add_parser("score")
    scorer.add_argument("jobs", nargs="+")
    scorer.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    scorer.add_argument("--expected-tasks", type=int, default=TASK_COUNT)
    scorer.add_argument("--expected-attempts", type=int, default=5)
    scorer.add_argument(
        "--tasks-root",
        help="validate exact task identities against the pinned dataset checkout",
    )
    scorer.add_argument(
        "--output",
        help="atomically retain the score ledger as a private mode-0600 JSON file",
    )
    _add_provenance_arguments(scorer)
    scorer.set_defaults(func=score)

    planner = subparsers.add_parser("plan-retries")
    planner.add_argument("jobs", nargs="+")
    planner.add_argument("--tasks-root", required=True)
    planner.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    planner.add_argument("--expected-attempts", type=int, default=5)
    _add_provenance_arguments(planner)
    planner.set_defaults(func=plan_retries)

    retry_configs = subparsers.add_parser("write-retry-configs")
    retry_configs.add_argument("jobs", nargs="+")
    retry_configs.add_argument("--tasks-root", required=True)
    retry_configs.add_argument("--template", required=True)
    retry_configs.add_argument("--output-root", required=True)
    retry_configs.add_argument("--job-prefix", required=True)
    retry_configs.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    retry_configs.add_argument("--expected-attempts", type=int, default=5)
    _add_provenance_arguments(retry_configs)
    retry_configs.set_defaults(func=write_retry_configs)

    autofill = subparsers.add_parser("run-until-complete")
    autofill.add_argument("jobs", nargs="+")
    autofill.add_argument("--harbor", required=True)
    autofill.add_argument("--tasks-root", required=True)
    autofill.add_argument("--template", required=True)
    autofill.add_argument("--output-root", required=True)
    autofill.add_argument("--job-prefix", required=True)
    autofill.add_argument("--max-waves", type=int, default=20)
    autofill.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    autofill.add_argument("--expected-attempts", type=int, default=5)
    _add_provenance_arguments(autofill)
    autofill.set_defaults(func=run_until_complete)

    analyzer = subparsers.add_parser("analyze")
    analyzer.add_argument("jobs", nargs="+")
    analyzer.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    analyzer.add_argument(
        "--output",
        help="atomically retain the analysis as a private mode-0600 JSON file",
    )
    analyzer.set_defaults(func=analyze)
    collector = subparsers.add_parser(
        "collect-trajectories",
        help="copy privacy-safe ATIF traces and layered failure attributions",
    )
    collector.add_argument("jobs", nargs="+")
    collector.add_argument("--output-root", required=True)
    collector.add_argument("--expected-model", default="deepseek-v4-flash-yourprovider")
    collector.set_defaults(func=collect_trajectories)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
