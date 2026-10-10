"""Evaluation host storage policy, shared by current and future execution adapters.

The checkout filesystem is the default; on DolphinFS, every writable evaluation
root must stay on DolphinFS. Temporary storage is never host /tmp, /var/tmp or
/dev/shm, even when inherited TMPDIR points there. Guest paths are not host paths.
Explicit private artifact roots are owned by their existing artifact lifecycle.
"""
from __future__ import annotations

from pathlib import Path

from rootless_vm.host_paths import (
    _local_scratch_base,
    _local_storage_opt_in,
    child_storage_environment,
    validate_host_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DOLPHINFS = Path("/mnt/dolphinfs")
STORAGE_POLICY = "checkout-volume-private-v1"


def validate_output_path(path: Path) -> Path:
    candidate = validate_host_path(path.expanduser().absolute())
    resolved = candidate.resolve()
    if not _local_storage_opt_in() and PROJECT_ROOT.is_relative_to(_DOLPHINFS) \
            and not resolved.is_relative_to(_DOLPHINFS):
        raise ValueError("this checkout requires evaluation host paths under /mnt/dolphinfs")
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError("evaluation root must be a dedicated directory, not root or HOME")
    return resolved


def _local_scratch_tmp(run_dir: Path) -> Path:
    """Private HOME-directory scratch tmp for the socket-incapable-volume opt-in.

    The run's durable artifacts stay on the checkout volume; only the
    transient scratch that must support pathname Unix sockets (this host's
    DolphinFS cannot create them, ENOSYS) moves to user-private HOME storage.
    """
    import os
    import stat as _stat
    root = _local_scratch_base() / "runs" / run_dir.name / "tmp"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.geteuid() or _stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError(
            f"local evaluation scratch must be owner-private 0700: {root}")
    return root


def runtime_environment(run_dir: Path) -> dict[str, str]:
    """Bound child writes to one run; no global environment/tempfile cache edits.

    At most a fixed set of directories is created. Their retention follows the
    run (no shared mutable package cache or automatic deletion of failure costs).
    Immutable verified image caches are separately lifecycle-owned.
    """
    environment = child_storage_environment(validate_output_path(run_dir))
    if _local_storage_opt_in():
        scratch = str(_local_scratch_tmp(run_dir))
        for key in ("TMPDIR", "TMP", "TEMP", "ROOTLESS_VM_TMPDIR"):
            environment[key] = scratch
    return environment


def storage_record(run_dir: Path) -> dict[str, object]:
    return {"policy": STORAGE_POLICY, "environment": runtime_environment(run_dir)}


def validate_storage_record(run_dir: Path, record: object) -> None:
    # Old evidence remains readable, but cannot silently resume with a different
    # storage boundary. New runs always persist this record before dispatch.
    if not isinstance(record, dict) or record != storage_record(run_dir):
        raise ValueError("run storage policy is missing or drifted; prepare a new run")
