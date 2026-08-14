"""One-command Harbor launcher for the rootless QEMU environment."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarborRunSpec:
    harbor: str
    task_path: Path
    base_disk: Path
    base_disk_sha256: str
    image_iso: Path
    image_iso_sha256: str
    image_reference: str
    state_root: Path
    prepared_cache_root: Path
    jobs_dir: Path
    model: str = "deepseek-v4-flash-meituan"
    python_runtime_image: str | None = None
    job_name: str | None = None
    oracle: bool = False


def _private_output_dir(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"private output path must not be a symbolic link: {expanded}")
    resolved = expanded.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"private output path must be a directory: {resolved}")
        if resolved.stat().st_mode & 0o077:
            raise PermissionError(
                f"private output path must not be group/world accessible: {resolved}"
            )
    else:
        resolved.mkdir(parents=True, mode=0o700)
    return resolved


def _resolved_executable(value: str) -> str:
    candidate = shutil.which(value)
    if candidate is None:
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            candidate = str(path.resolve())
    if candidate is None:
        raise ValueError(f"Harbor executable not found: {value}")
    return candidate


def harbor_argv(spec: HarborRunSpec) -> list[str]:
    task_path = spec.task_path.expanduser().resolve(strict=True)
    if not task_path.is_dir():
        raise ValueError(f"task path must be a directory: {task_path}")
    base_disk = spec.base_disk.expanduser().resolve(strict=True)
    image_iso = spec.image_iso.expanduser().resolve(strict=True)
    if not base_disk.is_file() or not image_iso.is_file():
        raise ValueError("base disk and image ISO must be regular files")
    state_root = _private_output_dir(spec.state_root)
    cache_root = _private_output_dir(spec.prepared_cache_root)
    jobs_dir = _private_output_dir(spec.jobs_dir)
    argv = [
        _resolved_executable(spec.harbor),
        "run",
        "--path",
        str(task_path),
        "--agent",
        (
            "oracle"
            if spec.oracle
            else "rootless_vm.harbor_tofu_agent:TofuHostAgent"
        ),
        "--env",
        "rootless_vm.harbor_environment:RootlessQemuEnvironment",
        "--environment-kwarg",
        f"base_disk={base_disk}",
        "--environment-kwarg",
        f"base_disk_sha256={spec.base_disk_sha256}",
        "--environment-kwarg",
        f"image_iso={image_iso}",
        "--environment-kwarg",
        f"image_iso_sha256={spec.image_iso_sha256}",
        "--environment-kwarg",
        f"image_reference={spec.image_reference}",
        "--environment-kwarg",
        f"state_root={state_root}",
        "--environment-kwarg",
        f"prepared_cache_root={cache_root}",
        "--jobs-dir",
        str(jobs_dir),
        "--n-concurrent",
        "1",
        "--yes",
    ]
    if not spec.oracle:
        if not spec.model.strip():
            raise ValueError("model must not be empty")
        argv += ["--model", spec.model]
    if spec.python_runtime_image:
        argv += [
            "--environment-kwarg",
            f"python_runtime_image={spec.python_runtime_image}",
        ]
    if spec.job_name:
        argv += ["--job-name", spec.job_name]
    return argv


def run_harbor(spec: HarborRunSpec) -> int:
    """Run Harbor without copying provider credentials into child arguments."""

    return subprocess.run(harbor_argv(spec), check=False).returncode
