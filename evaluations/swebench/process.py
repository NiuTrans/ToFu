from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO


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
    """Create per-run command aliases required by a Harbor backend."""
    if backend != "singularity":
        return {}
    runtime = singularity_runtime()
    if runtime is None:
        raise ValueError("singularity/apptainer executable not found")
    if Path(runtime).name == "singularity":
        return {}
    shim_dir = run_dir / ".runtime-bin"
    shim_dir.mkdir(mode=0o700, exist_ok=True)
    shim = shim_dir / "singularity"
    if shim.is_symlink() and shim.resolve() != Path(runtime).resolve():
        shim.unlink()
    if not shim.exists():
        shim.symlink_to(Path(runtime).resolve())
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def run_streaming(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a child in its own process group while teeing merged output."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
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
