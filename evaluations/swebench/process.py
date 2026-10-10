from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from evaluations.paths import runtime_environment, validate_output_path


def capture_resource_environment() -> dict[str, str]:
    """Freeze only public resource knobs, never arbitrary host environment."""
    from runtime_guards import (
        RESOURCE_BUDGET_ENV_KEYS, install_runtime_resource_defaults,
        probe_system_resources,
    )
    environment = os.environ.copy()
    manifest = install_runtime_resource_defaults(
        environment, snapshot=probe_system_resources(environment))
    return {'TOFU_RESOURCE_PROFILE': manifest['resource_profile'],
            **{key: environment[key] for key in sorted(RESOURCE_BUDGET_ENV_KEYS)
               if key in environment}}


def recorded_resource_environment(manifest: Mapping) -> dict[str, str]:
    """Validate a registered budget before reusing it on resume."""
    from runtime_guards import RESOURCE_BUDGET_ENV_KEYS
    value = manifest.get('runtime_resource_environment', {})
    if not isinstance(value, dict):
        raise ValueError('runtime resource environment must be an object')
    allowed = RESOURCE_BUDGET_ENV_KEYS | {'TOFU_RESOURCE_PROFILE'}
    if set(value) - allowed:
        raise ValueError('runtime resource environment contains an unknown key')
    for key, raw in value.items():
        if not isinstance(raw, str) or len(raw) > 32:
            raise ValueError('runtime resource environment requires bounded strings')
        if key == 'TOFU_RESOURCE_PROFILE':
            if raw not in {'balanced', 'throughput'}:
                raise ValueError('unknown runtime resource profile')
        else:
            try:
                int(raw)
            except ValueError as exc:
                raise ValueError('runtime resource environment requires integer budgets') from exc
    return dict(value)


def resolve_executable(executable: str) -> str | None:
    """Resolve a command, including scripts next to the active venv Python."""
    if "/" in executable:
        path = Path(executable).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(executable)
    if found:
        return found
    sibling = Path(sys.executable).with_name(executable)
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return None


def singularity_runtime() -> str | None:
    """Return a Singularity-compatible executable, preferring its native name."""
    return resolve_executable("singularity") or resolve_executable("apptainer")


def prepare_runtime_environment(backend: str, run_dir: Path) -> dict[str, str]:
    """Isolate child caches/temp on the run volume and add backend aliases."""
    environment = runtime_environment(run_dir)
    if backend != "singularity":
        return environment
    runtime = singularity_runtime()
    if runtime is None:
        raise ValueError("singularity/apptainer executable not found")
    if Path(runtime).name == "singularity":
        return environment
    shim_dir = run_dir / ".runtime-bin"
    shim_dir.mkdir(mode=0o700, exist_ok=True)
    shim = shim_dir / "singularity"
    if shim.is_symlink() and shim.resolve() != Path(runtime).resolve():
        shim.unlink()
    if not shim.exists():
        shim.symlink_to(Path(runtime).resolve())
    return {**environment, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def run_streaming(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
    unset_env: Sequence[str] = (),
) -> int:
    """Run a child in its own process group while teeing merged output."""
    cwd = validate_output_path(cwd)
    log_path = validate_output_path(log_path)
    storage_environment = runtime_environment(cwd)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    for name in unset_env:
        child_env.pop(str(name), None)
    child_env.update(storage_environment)
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            _copy_lines(process.stdout, log)
            return process.wait()
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGINT)
            try:
                return process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    return process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    return process.wait()


def _copy_lines(source: TextIO, log: TextIO) -> None:
    for line in iter(source.readline, ""):
        log.write(line)
        log.flush()
        sys.stdout.write(line)
        sys.stdout.flush()
