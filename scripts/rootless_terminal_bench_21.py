#!/usr/bin/env python3
"""Prepare and run all Terminal-Bench 2.1 tasks without host Docker or root.

Registry payloads are pulled by immutable manifest digest and embedded as opaque
files in read-only ISOs.  They are parsed and expanded only inside the rootless
QEMU guest.  The resulting image store can feed one concurrent Harbor job while
keeping each trial in a disposable VM.
"""

from __future__ import annotations

import argparse
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

DATASET_COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
TASK_COUNT = 89
ASSET_SCHEMA = 2
INDEX_SCHEMA = 1
_PRINT_LOCK = threading.Lock()
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


@dataclass(frozen=True)
class Task:
    name: str
    path: Path
    image: str
    cpus: int
    memory_mib: int
    agent_timeout_sec: float
    verifier_timeout_sec: float


def _load_tasks(tasks_root_value: str | os.PathLike[str]) -> list[Task]:
    tasks_root = Path(tasks_root_value).expanduser().resolve(strict=True)
    repository = tasks_root.parent
    revision = _run(["git", "-C", str(repository), "rev-parse", "HEAD"], timeout=20).strip()
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
        memory_mib = int(config.get("environment", {}).get("memory_mb") or 2048)
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
            )
        )
    if len(tasks) != TASK_COUNT:
        raise ValueError(f"expected {TASK_COUNT} tasks, found {len(tasks)}")
    return tasks


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
    genisoimage: Path,
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

    manifest_digest = _run(
        [str(crane), "digest", "--platform", "linux/amd64", task.image],
        timeout=180,
    ).strip()
    if not manifest_digest.startswith("sha256:"):
        raise RuntimeError(f"registry returned invalid digest for {task.image}")
    repository = _repository_without_tag(task.image)
    pinned = f"{repository}@{manifest_digest}"
    manifest = json.loads(
        _run(
            [str(crane), "manifest", "--platform", "linux/amd64", pinned],
            timeout=180,
        )
    )
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
        if not isinstance(descriptor, dict) or descriptor.get("digest") != manifest_digest:
            raise RuntimeError(f"OCI index digest mismatch: {task.image}")
        descriptor["annotations"] = {
            "org.opencontainers.image.ref.name": task.image
        }
        _atomic_json(index_path, index)
        _run(
            [str(archive_tool), "-cf", str(tar_path), "-C", str(oci_path), "."],
            timeout=7200,
        )
        tar_digest = _sha256(tar_path)
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
    tasks = _load_tasks(args.tasks_root)
    assets_root = _private_dir(args.assets_root)
    crane = _executable(args.crane)
    archive_tool = _executable(args.archive_tool)
    genisoimage = _executable(args.genisoimage)
    completed: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _prepare_one,
                task,
                assets_root=assets_root,
                crane=crane,
                archive_tool=archive_tool,
                genisoimage=genisoimage,
            ): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            completed[task.image] = future.result()
            _say(f"PROGRESS {len(completed)}/{len(tasks)}")
    images = {
        task.image: {
            "iso": f"{task.name}/task-image.iso",
            "sha256": completed[task.image]["iso_sha256"],
            "loaded_image_reference": completed[task.image][
                "loaded_image_reference"
            ],
            "registry_digest": completed[task.image]["registry_digest"],
            "task": task.name,
        }
        for task in tasks
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


def write_config(args: argparse.Namespace) -> int:
    tasks = _load_tasks(args.tasks_root)
    trial_concurrency, agent_concurrency = _resolve_concurrency(
        args.concurrency, args.agent_concurrency
    )
    if args.max_rounds < 1:
        raise ValueError("max rounds must be positive")
    if args.max_output_tokens < 256:
        raise ValueError("max output tokens must be at least 256")
    if not 1024 <= args.context_checkpoint_tokens <= 1_000_000:
        raise ValueError(
            "context checkpoint tokens must be between 1024 and 1000000"
        )
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
    if args.task:
        requested = set(args.task)
        available = {task.name for task in tasks}
        missing = sorted(requested - available)
        if missing:
            raise ValueError(f"unknown Terminal-Bench tasks: {missing}")
        tasks = [task for task in tasks if task.name in requested]
    # Harbor's verifier currently calls BaseEnvironment.exec() without passing
    # its computed phase timeout. Keep the environment's inner watchdog at
    # least as large as the selected tasks' scaled verifier budgets; otherwise
    # it silently kills a valid verifier early and Harbor reports a misleading
    # RewardFileNotFoundError.
    default_exec_timeout_sec = max(
        900.0,
        max(task.verifier_timeout_sec for task in tasks)
        * verifier_timeout_multiplier,
    )
    if default_exec_timeout_sec > 86400:
        raise ValueError("scaled verifier timeout exceeds the 24 hour safety cap")
    control_root = _private_dir(args.control_root)
    assets_root = _private_dir(args.assets_root)
    state_root = _private_dir(args.state_root)
    cache_root = _private_dir(args.cache_root)
    jobs_dir = _private_dir(args.jobs_dir)
    base_disk = Path(args.base_disk).expanduser().resolve(strict=True)
    qemu = _executable(args.qemu)
    qemu_img = _executable(args.qemu_img)
    if not (assets_root / "index.json").is_file():
        raise ValueError("image store is not prepared")
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
                "virtual_time_shift": args.virtual_time_shift,
            },
        },
        "agents": [
            {
                "name": "rootless_vm.harbor_tofu_agent:TofuHostAgent",
                "model_name": args.model,
                # Keep model/API pressure below the number of live trials so
                # slow pure-TCG verifiers can overlap with later agent work.
                "n_concurrent": agent_concurrency,
                "kwargs": {
                    "reasoning_effort": args.reasoning_effort,
                    "temperature": args.temperature,
                    "top_p": args.top_p,
                    "max_rounds": args.max_rounds,
                    "max_output_tokens": args.max_output_tokens,
                    "context_checkpoint_tokens": args.context_checkpoint_tokens,
                    "dispatch_max_retries": args.dispatch_max_retries,
                    # Unlike Harbor's per-job n_concurrent value, this
                    # filesystem-backed gate is shared by every retry job.
                    "global_dispatch_concurrency": args.global_dispatch_concurrency,
                    "dispatch_gate_dir": str(control_root / "dispatch-gate"),
                    "command_timeout_sec": min(
                        1800, round(120 * args.runtime_timeout_multiplier)
                    ),
                    # Tool-requested timeouts describe native-runtime work.
                    # Translate them to pure-TCG wall time just like Harbor's
                    # phase budgets, while retaining the hard 1800s ceiling.
                    "command_timeout_multiplier": args.runtime_timeout_multiplier,
                },
            }
        ],
        "tasks": [{"path": str(task.path)} for task in tasks],
    }
    output = control_root / f"{args.job_name}.json"
    _atomic_json(output, config)
    print(output)
    return 0


def prepare_cache(args: argparse.Namespace) -> int:
    from rootless_vm.image_cache import PreparedImageCache, PreparedImageSpec
    from rootless_vm.image_store import resolve_image_store
    from rootless_vm.qemu import QemuRuntime

    tasks = _load_tasks(args.tasks_root)
    assets_root = _private_dir(args.assets_root)
    cache_root = _private_dir(args.cache_root)
    base_disk = Path(args.base_disk).expanduser().resolve(strict=True)
    runtime = QemuRuntime.discover(args.qemu, args.qemu_img)
    base_sha256 = _sha256(base_disk)

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
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
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
        raise ValueError(f"Harbor config contains unknown task paths: {sorted(missing)}")


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


_NETWORK_MARKERS = (
    "failed to download",
    "failed to fetch",
    "tunnel error",
    "temporary failure resolving",
    "could not resolve host",
    "connection reset by peer",
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


def _transcript_audit(
    trial_dir: Path,
) -> tuple[list[str], int, dict[str, int]]:
    transcript = trial_dir / "agent" / "tofu-host-transcript.json"
    try:
        rows = json.loads(transcript.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], 0, {
            "dispatches": 0,
            "429_retries": 0,
            "max_429_retries": 0,
            "gate_wait_ms": 0,
        }
    models: set[str] = set()
    raw_reasoning = 0
    dispatches = 0
    retries_429 = 0
    max_retries_429 = 0
    gate_wait_ms = 0
    if not isinstance(rows, list):
        return [], 0, {
            "dispatches": 0,
            "429_retries": 0,
            "max_429_retries": 0,
            "gate_wait_ms": 0,
        }
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
            gate_wait_ms += int(dispatch.get("gate_wait_ms") or 0)
        assistant = row.get("assistant")
        reasoning = (
            assistant.get("reasoning_content")
            if isinstance(assistant, dict)
            else None
        )
        if isinstance(reasoning, str) and reasoning:
            raw_reasoning += 1
    return sorted(models), raw_reasoning, {
        "dispatches": dispatches,
        "429_retries": retries_429,
        "max_429_retries": max_retries_429,
        "gate_wait_ms": gate_wait_ms,
    }


def _route_audit(trial_dir: Path) -> tuple[list[str], int]:
    models, raw_reasoning, _metrics = _transcript_audit(trial_dir)
    return models, raw_reasoning


def _classify_trial(
    trial: dict[str, Any],
    trial_dir: Path,
    expected_model: str,
) -> dict[str, Any]:
    verifier_result = trial.get("verifier_result") or {}
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else {}
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    reward_value = float(reward) if isinstance(reward, (int, float)) else None
    exception = trial.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    exception_message = str(exception.get("exception_message") or "")
    verifier_text = _tail_text(trial_dir / "verifier" / "test-stdout.txt")
    verifier_report_exists = (trial_dir / "verifier" / "ctrf.json").is_file()
    audit_text = _tail_text(trial_dir / "agent" / "tofu-host-transcript.json")
    audit_edge_text = _edge_text(
        trial_dir / "agent" / "tofu-host-transcript.json"
    )
    exception_text = _tail_text(trial_dir / "exception.txt")
    combined = "\n".join((exception_type, exception_message, exception_text, verifier_text))
    lowered = combined.lower()
    served_models, raw_reasoning, dispatch_audit = _transcript_audit(trial_dir)
    verifier_timing = trial.get("verifier") or {}
    verifier_elapsed = _seconds_between(
        verifier_timing.get("started_at") if isinstance(verifier_timing, dict) else None,
        verifier_timing.get("finished_at") if isinstance(verifier_timing, dict) else None,
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
    failure_signatures = []
    for line in verifier_text.splitlines():
        stripped = line.strip()
        if re.match(r"^(FAILED|ERROR)\b", stripped):
            failure_signatures.append(stripped[:500])
    failure_signatures = failure_signatures[-8:]

    reason = ""
    if raw_reasoning:
        classification = "privacy_violation"
        reason = "raw reasoning was persisted in the audit transcript"
    elif served_models and served_models != [expected_model]:
        classification = "routing_violation"
        reason = f"served models were {served_models!r}"
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
        and "nohup make" in audit_edge_text.lower()
        and "make proof" in audit_edge_text.lower()
        and "[c]oqc" in audit_edge_text.lower()
        and agent_timeout_multiplier < 8
    ):
        classification = "environment_timing_sensitive"
        reason = (
            "the requested CompCert proof build was still running when the "
            "agent wall clock expired under contended pure TCG; rerun all "
            "attempts for this task at low VM load"
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
    elif any(marker in lowered for marker in _NETWORK_MARKERS) and (
        reward_value is None or not verifier_report_exists
    ):
        classification = "infrastructure_network"
        reason = "verifier dependency bootstrap failed before a test report existed"
    elif any(marker in lowered for marker in _API_MARKERS) and reward_value is None:
        classification = "infrastructure_api"
        reason = "model gateway or inference scheduler failed transiently"
    elif "prompttoolongerror" in lowered:
        classification = "harness_context_limit"
        reason = (
            "legacy Tofu history exceeded the provider context window before "
            "the task finished; retry with proactive context checkpoints"
        )
    elif (
        "rewardfilenotfounderror" in lowered
        and verifier_elapsed is not None
        and 890 <= verifier_elapsed <= 930
    ):
        classification = "infrastructure_timeout"
        reason = "environment exec hit its legacy 900 second inner watchdog"
    elif (
        "cancel-async-tasks" in str(trial.get("task_name", ""))
        and re.search(
            r">\s*assert\s+stdout\.count\([\"']Task started\.[\"']\)\s*"
            r"==\s*2\s*\nE\s+AssertionError:\s+assert\s+0\s*==\s*2",
            verifier_text,
            re.IGNORECASE,
        )
    ):
        classification = "environment_timing_sensitive"
        reason = "fixed 500ms signal fired before Python started under pure TCG"
    elif not served_models:
        classification = "routing_unverified"
        reason = "no audited model dispatch record was persisted"
    elif reward_value is not None and agent_metadata.get("exit_reason") == "no_progress":
        classification = "model_timeout"
        reason = (
            "agent exhausted the repeated-progress recovery budget before producing "
            "a verifier-passing artifact"
        )
    elif reward_value is not None:
        classification = "model_semantic"
        reason = "verifier ran and returned a non-passing reward"
    elif "verifiertimeouterror" in lowered:
        classification = "infrastructure_timeout"
        reason = "verifier exceeded the local backend time budget"
    else:
        classification = "harness_error"
        reason = exception_type or "trial completed without a numeric reward"

    return {
        "trial": str(trial.get("trial_name") or trial_dir.name),
        "task": str(trial.get("task_name") or "unknown"),
        "source": str(trial_dir),
        "reward": reward_value,
        "classification": classification,
        "reason": reason,
        "exception_type": exception_type or None,
        "verifier_elapsed_sec": (
            round(verifier_elapsed, 3) if verifier_elapsed is not None else None
        ),
        "served_models": served_models,
        "raw_reasoning_records": raw_reasoning,
        "dispatches": dispatch_audit["dispatches"],
        "dispatch_429_retries": dispatch_audit["429_retries"],
        "max_dispatch_429_retries": dispatch_audit["max_429_retries"],
        "dispatch_gate_wait_sec": round(dispatch_audit["gate_wait_ms"] / 1000, 3),
        "agent_exit_reason": agent_metadata.get("exit_reason"),
        "agent_rounds": agent_metadata.get("rounds"),
        "agent_command_count": agent_metadata.get("command_count"),
        "agent_recovery_count": agent_metadata.get("recovery_count"),
        "agent_context_checkpoint_count": agent_metadata.get(
            "context_checkpoint_count"
        ),
        "agent_validation_reuse_count": agent_metadata.get(
            "validation_reuse_count"
        ),
        "agent_timeout_multiplier": agent_timeout_multiplier,
        "verifier_timeout_multiplier": verifier_timeout_multiplier,
        "input_tokens": agent_result.get("n_input_tokens")
        if isinstance(agent_result, dict)
        else None,
        "output_tokens": agent_result.get("n_output_tokens")
        if isinstance(agent_result, dict)
        else None,
        "failure_signatures": failure_signatures,
    }


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
    for row in observations:
        label = row["classification"]
        counts[label] = counts.get(label, 0) + 1
    payload = {
        "trials": len(observations),
        "classifications": dict(sorted(counts.items())),
        "audited_route_trials": sum(
            row["served_models"] == [args.expected_model] for row in observations
        ),
        "route_pure": bool(observations) and all(
            row["served_models"] == [args.expected_model] for row in observations
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
        "max_dispatch_429_retries": max(
            (
                int(row.get("max_dispatch_429_retries") or 0)
                for row in observations
            ),
            default=0,
        ),
        "dispatch_gate_wait_sec": round(
            sum(float(row.get("dispatch_gate_wait_sec") or 0) for row in observations),
            3,
        ),
        "details": observations,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def score(args: argparse.Namespace) -> int:
    if args.expected_tasks < 1 or args.expected_attempts < 1:
        raise ValueError("expected task and attempt counts must be positive")
    observations = _observations(args)
    tasks: dict[str, list[dict[str, Any]]] = {}
    invalid_counts: dict[str, int] = {}
    raw_reward = 0.0
    for row in observations:
        raw_reward += float(row["reward"] or 0.0)
        if row["classification"] in _VALID_SCORE_LABELS:
            tasks.setdefault(row["task"], []).append(row)
        else:
            label = row["classification"]
            invalid_counts[label] = invalid_counts.get(label, 0) + 1
    expected_names: set[str] | None = None
    if args.tasks_root:
        expected_names = {
            f"terminal-bench/{task.name}" for task in _load_tasks(args.tasks_root)
        }
        if len(expected_names) != args.expected_tasks:
            raise ValueError(
                "expected task count does not match the pinned dataset checkout"
            )
    valid_trials = sum(len(rows) for rows in tasks.values())
    valid_reward = sum(
        float(row["reward"] or 0.0)
        for rows in tasks.values()
        for row in rows
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
    coverage_complete = (
        len(tasks) == args.expected_tasks
        and all(len(rows) == args.expected_attempts for rows in tasks.values())
        and not unexpected_tasks
        and (expected_names is None or set(tasks) == expected_names)
    )
    payload = {
        "observed_trials": len(observations),
        "raw_score_percent": (
            100 * raw_reward / len(observations) if observations else None
        ),
        "valid_trials": valid_trials,
        "expected_trials": expected_trials,
        "coverage_complete": coverage_complete,
        "surplus_valid_trials": dict(sorted(surplus.items())),
        "unexpected_tasks": unexpected_tasks,
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
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def plan_retries(args: argparse.Namespace) -> int:
    """Report unbiased retry groups needed to reach exact k-shot coverage."""

    if args.expected_attempts < 1:
        raise ValueError("expected attempt count must be positive")
    expected_names = {
        f"terminal-bench/{task.name}" for task in _load_tasks(args.tasks_root)
    }
    observations = _observations(args)
    valid_counts: dict[str, int] = {}
    invalid_counts: dict[str, int] = {}
    invalid_by_task: dict[str, set[str]] = {}
    unscored_agent_timeout_tasks: set[str] = set()
    for row in observations:
        if row["classification"] in _VALID_SCORE_LABELS:
            task = row["task"]
            valid_counts[task] = valid_counts.get(task, 0) + 1
        else:
            label = row["classification"]
            invalid_counts[label] = invalid_counts.get(label, 0) + 1
            invalid_by_task.setdefault(row["task"], set()).add(label)
            if (
                label == "infrastructure_timeout"
                and str(row.get("exception_type") or "").lower()
                == "agenttimeouterror"
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
            "max_concurrent_trials": 16,
            "agent_concurrency": 4,
        },
        "verifier_heavy": {
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 8,
            "max_concurrent_trials": 8,
            "agent_concurrency": 4,
        },
        "tcg_low_load": {
            "agent_timeout_multiplier": 8,
            "verifier_timeout_multiplier": 8,
            "max_concurrent_trials": 2,
            "agent_concurrency": 2,
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
            if "environment_nested_emulation" in labels:
                profile = "nested_emulation"
            elif (
                "environment_timing_sensitive" in labels
                or name in unscored_agent_timeout_tasks
            ):
                profile = "tcg_low_load"
            elif "infrastructure_timeout" in labels:
                profile = "verifier_heavy"
            else:
                profile = "standard"
            profile_groups.setdefault((profile, attempts), []).append(name)
    payload = {
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
        "retry_profiles": [
            {
                "profile": profile,
                "attempts": attempts,
                "tasks": names,
                **profile_specs[profile],
            }
            for (profile, attempts), names in sorted(profile_groups.items())
        ],
        "surplus_valid_trials": dict(sorted(surplus.items())),
        "unexpected_tasks": unexpected_tasks,
        "invalid_trials": dict(sorted(invalid_counts.items())),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-assets")
    prepare.add_argument("--tasks-root", required=True)
    prepare.add_argument("--assets-root", required=True)
    prepare.add_argument("--crane", required=True)
    prepare.add_argument("--archive-tool", required=True)
    prepare.add_argument("--genisoimage", required=True)
    prepare.add_argument("--workers", type=int, default=4, choices=range(1, 17))
    prepare.set_defaults(func=prepare_assets)

    cache = subparsers.add_parser("prepare-cache")
    cache.add_argument("--tasks-root", required=True)
    cache.add_argument("--assets-root", required=True)
    cache.add_argument("--cache-root", required=True)
    cache.add_argument("--base-disk", required=True)
    cache.add_argument("--qemu", required=True)
    cache.add_argument("--qemu-img", required=True)
    cache.add_argument("--workers", type=int, default=4, choices=range(1, 17))
    cache.add_argument("--prepare-timeout-sec", type=float, default=3600.0)
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
    config.add_argument("--concurrency", type=int, default=8)
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
    config.add_argument("--model", default="deepseek-v4-flash-meituan")
    config.add_argument(
        "--reasoning-effort", choices=("low", "high", "max"), default="max"
    )
    config.add_argument("--temperature", type=float, default=1.0)
    config.add_argument("--top-p", type=float, default=0.95)
    config.add_argument("--max-rounds", type=int, default=4096)
    config.add_argument("--max-output-tokens", type=int, default=32768)
    config.add_argument(
        "--context-checkpoint-tokens",
        type=int,
        default=300000,
        help="compact the agent history before the provider context limit",
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

    scorer = subparsers.add_parser("score")
    scorer.add_argument("jobs", nargs="+")
    scorer.add_argument("--expected-model", default="deepseek-v4-flash-meituan")
    scorer.add_argument("--expected-tasks", type=int, default=TASK_COUNT)
    scorer.add_argument("--expected-attempts", type=int, default=5)
    scorer.add_argument(
        "--tasks-root",
        help="validate exact task identities against the pinned dataset checkout",
    )
    scorer.set_defaults(func=score)

    planner = subparsers.add_parser("plan-retries")
    planner.add_argument("jobs", nargs="+")
    planner.add_argument("--tasks-root", required=True)
    planner.add_argument("--expected-model", default="deepseek-v4-flash-meituan")
    planner.add_argument("--expected-attempts", type=int, default=5)
    planner.set_defaults(func=plan_retries)

    analyzer = subparsers.add_parser("analyze")
    analyzer.add_argument("jobs", nargs="+")
    analyzer.add_argument("--expected-model", default="deepseek-v4-flash-meituan")
    analyzer.set_defaults(func=analyze)
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
