"""Trusted jailed-command adapter to the existing project command lifecycle.

The caller owns a private, freshly prepared jail and frozen toolchain digests.
This internal port does not allocate project authority, publish file effects,
or expose a model tool. It reuses the namespace/chroot launcher and command
runner; there is no second task loop or host-command fallback.
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .host_paths import validate_host_path
from .pidfd import require_process_handles
from .qemu_launcher import _sanitized_environment


@dataclass(frozen=True)
class ProjectCommandSpec:
    jail: Path
    argv: tuple[str, ...]
    toolchain: tuple[tuple[str, str], ...]
    deadline_epoch: float
    timeout_seconds: float
    memory_bytes: int
    file_bytes: int
    file_descriptors: int
    cpu_seconds: int


def _guest_path(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if (not path.is_absolute() or value != str(path) or '..' in path.parts
            or '\0' in value or len(path.parts) < 2):
        raise ValueError('guest paths must be canonical absolute file paths')
    return path.parts[1:]


def _check_pinned_file(root: int, path: str, digest: str) -> None:
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('toolchain requires complete SHA-256 digests')
    parts = _guest_path(path)
    directory = os.dup(root)
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError('toolchain must contain private regular copies')
            with os.fdopen(os.dup(descriptor), 'rb') as content:
                actual = hashlib.file_digest(content, 'sha256')
            if actual.hexdigest() != digest:
                raise ValueError('pinned toolchain digest changed')
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _launch_arguments(spec: ProjectCommandSpec) -> tuple[str, ...]:
    if not isinstance(spec, ProjectCommandSpec):
        raise TypeError('jailed commands require a trusted ProjectCommandSpec')
    if (not isinstance(spec.argv, tuple) or not spec.argv
            or any(not isinstance(x, str) or '\0' in x for x in spec.argv)):
        raise ValueError('guest command requires structured arguments')
    _guest_path(spec.argv[0])
    for value in (spec.deadline_epoch, spec.timeout_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError('jailed commands require finite positive deadlines')
    for value in (spec.memory_bytes, spec.file_bytes, spec.file_descriptors, spec.cpu_seconds):
        if type(value) is not int or value <= 0:
            raise ValueError('jailed commands require explicit positive resource limits')
    jail = validate_host_path(spec.jail)
    metadata = jail.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise PermissionError('command jail must be an owned private directory')
    if (not isinstance(spec.toolchain, tuple) or any(
            not isinstance(pin, tuple) or len(pin) != 2 for pin in spec.toolchain)):
        raise ValueError('toolchain manifest must be immutable path/digest pairs')
    pins = dict(spec.toolchain)
    if len(pins) != len(spec.toolchain) or spec.argv[0] not in pins:
        raise ValueError('toolchain must pin the exact guest executable once')
    root = os.open(jail, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for path, digest in spec.toolchain:
            _check_pinned_file(root, path, digest)
    finally:
        os.close(root)
    unshare, prlimit = shutil.which('unshare'), shutil.which('prlimit')
    if not unshare or not prlimit:
        raise RuntimeError('required namespace supervision is unavailable')
    require_process_handles()
    return (
        str(Path(prlimit).resolve()), '--core=0:0',
        f'--as={spec.memory_bytes}:{spec.memory_bytes}',
        f'--fsize={spec.file_bytes}:{spec.file_bytes}',
        f'--nofile={spec.file_descriptors}:{spec.file_descriptors}',
        f'--cpu={spec.cpu_seconds}:{spec.cpu_seconds}', '--',
        sys.executable, str(Path(__file__).with_name('pdeath_exec.py').resolve()),
        '--project-supervisor', str(os.getpid()),
        str(Path(unshare).resolve()), '--user', '--map-root-user', '--pid',
        '--net', '--ipc', '--uts', '--fork', sys.executable,
        str(Path(__file__).with_name('qemu_launcher.py').resolve()),
        '--project-command', str(jail), *spec.argv,
    )


def run_project_command(spec: ProjectCommandSpec, *, task=None, runtime_context=None,
                        on_chunk=None, on_spawn=None) -> str:
    """Execute one trusted specification; retain all file effects in its jail.

    Limits are per-process CPU/address space/file size/descriptors plus an
    absolute wall deadline. They are not aggregate fork-tree memory/disk/PID
    bounds. Programme/tool admission must supply those before unattended use.
    """
    from lib.project_mod.run_command import run_command_process
    from lib.project_mod.workspace_lease import workspace_write_lease

    if not isinstance(spec, ProjectCommandSpec):
        raise TypeError('jailed commands require a trusted ProjectCommandSpec')
    with workspace_write_lease(spec.jail):
        arguments = _launch_arguments(spec)
        timeout = min(spec.timeout_seconds, spec.deadline_epoch - time.time())
        if timeout <= 0:
            raise TimeoutError('original command deadline has expired')
        return run_command_process(
            arguments, command='[isolated project command]', timeout=timeout,
            base=str(spec.jail), task=task, runtime_context=runtime_context,
            on_chunk=on_chunk, on_spawn=on_spawn,
            child_env=_sanitized_environment(), namespace_supervisor=True,
            deadline_epoch=spec.deadline_epoch)
