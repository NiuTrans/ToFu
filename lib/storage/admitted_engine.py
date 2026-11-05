"""Keep and relaunch exactly the storage engine bytes a supervisor admitted.

Responsibility: a staged deployment replaces the certified engine path while
the admitted engine keeps running (``python -m serverctl_pkg.native_engine``,
the update flow and ``scripts/deploy_tofudb_engine.py`` all publish by
rename), and only a full application restart admits the replacement. A
supervisor that restarts its engine after a crash or a liveness recycle must
therefore never reopen that mutable path: on 2026-10-27 a live preparation
replaced the engine at 04:57, and from 06:58 every restart was refused
("artifact changed since application admission") until the restart breaker
fenced storage for 5 h 23 min. A descriptor held open since admission is no
substitute: on the FUSE volume holding the checkout, reading a renamed-over
file through its open descriptor fails with ENOENT.

Entry point: AdmittedEngine.admit(path, verify). Admission reads the selected
artifact once, stages exactly those bytes in a directory private to this user
under the system temporary directory, lets ``verify`` judge their digest (pin,
source freshness) and publishes them read-only as ``<sha256>/<name>``. The
instance holds a shared lease on that copy and a descriptor of it for its
lifetime. launch_path() re-verifies the copy on every launch and returns it;
a copy removed by a temporary-file cleaner is republished from the descriptor
first, and bytes that no longer match raise AdmittedEngineLost. Admitting a
digest collects sibling copies that no live process leases, so disk use
follows the engines in use, not the deployments.

Dependencies: standard library and serverctl_pkg.local_placement (stdlib only).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import BinaryIO, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows keeps copies without leases
    fcntl = None

from serverctl_pkg.local_placement import ensure_private_directory

_ROOT_NAME = 'tofu-admitted-engines'
_LEASE_NAME = 'lease'
_STAGING_PREFIX = '.staging-'
_READ_BYTES = 1024 * 1024
# A crashed admission leaves a staging file or a lease-less directory behind;
# anything this old belongs to no admission in progress.
_ABANDONED_SECONDS = 3600.0


class AdmittedEngineLost(RuntimeError):
    """The admitted engine bytes can no longer be produced for a launch."""


def private_root() -> Path:
    """The per-user directory holding admitted engine copies (mode 0o700)."""
    uid = os.getuid() if hasattr(os, 'getuid') else None
    root = Path(tempfile.gettempdir()) / (
        _ROOT_NAME if uid is None else f'{_ROOT_NAME}-{uid}')
    ensure_private_directory(root)
    return root


def _identity(status: os.stat_result) -> tuple[int, int, int, int]:
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _digest(stream: BinaryIO, max_bytes: int) -> str | None:
    """SHA-256 of ``stream`` from its start, or None past ``max_bytes``."""
    stream.seek(0)
    digest = hashlib.sha256()
    read = 0
    for chunk in iter(lambda: stream.read(_READ_BYTES), b''):
        read += len(chunk)
        if read > max_bytes:
            return None
        digest.update(chunk)
    return digest.hexdigest()


def _stage(source: BinaryIO, root: Path, max_bytes: int) -> tuple[str, str]:
    """Copy ``source`` into a private read-only staging file; (path, sha256)."""
    descriptor, staged = tempfile.mkstemp(dir=root, prefix=_STAGING_PREFIX)
    digest = hashlib.sha256()
    copied = 0
    try:
        with os.fdopen(descriptor, 'wb') as output:
            for chunk in iter(lambda: source.read(_READ_BYTES), b''):
                copied += len(chunk)
                if copied > max_bytes:
                    raise RuntimeError(
                        f'storage engine exceeds the {max_bytes} byte verification budget')
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staged, 0o500)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
    return staged, digest.hexdigest()


def _lease(directory: Path) -> int | None:
    """Shared lease that keeps ``directory`` from collection; None without flock."""
    if fcntl is None:
        directory.mkdir(mode=0o700, exist_ok=True)
        return None
    while True:
        directory.mkdir(mode=0o700, exist_ok=True)
        lease = directory / _LEASE_NAME
        try:
            descriptor = os.open(lease, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except FileNotFoundError:
            continue  # collected between mkdir and open
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        try:
            if _same_file(os.stat(lease), os.fstat(descriptor)):
                return descriptor
        except FileNotFoundError:
            pass
        # A collector removed this copy before the lease was granted.
        os.close(descriptor)


def _lease_is_current(directory: Path, descriptor: int | None) -> bool:
    if descriptor is None:
        return fcntl is None
    try:
        return _same_file(os.stat(directory / _LEASE_NAME), os.fstat(descriptor))
    except FileNotFoundError:
        return False


def _collect(root: Path, keep: str) -> None:
    """Remove copies that no live process leases; best effort."""
    if fcntl is None:
        return
    now = time.time()
    for entry in os.scandir(root):
        try:
            if entry.name.startswith(_STAGING_PREFIX):
                if now - entry.stat(follow_symlinks=False).st_mtime > _ABANDONED_SECONDS:
                    os.unlink(entry.path)
                continue
            if entry.name == keep or not entry.is_dir(follow_symlinks=False):
                continue
            lease = Path(entry.path) / _LEASE_NAME
            try:
                descriptor = os.open(lease, os.O_RDONLY | os.O_CLOEXEC)
            except FileNotFoundError:
                if now - entry.stat(follow_symlinks=False).st_mtime > _ABANDONED_SECONDS:
                    for child in os.scandir(entry.path):
                        os.unlink(child.path)
                    os.rmdir(entry.path)
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            try:
                for child in os.scandir(entry.path):
                    if child.name != _LEASE_NAME:
                        os.unlink(child.path)
                os.unlink(lease)
                os.rmdir(entry.path)
            finally:
                os.close(descriptor)
        except OSError:
            continue


class AdmittedEngine:
    """One admitted engine: its configured path, digest and private copy."""

    def __init__(self, path: str, sha256: str, copy: Path, handle: BinaryIO,
                 lease: int | None, max_bytes: int) -> None:
        self.path = path
        self.sha256 = sha256
        self._copy = copy
        self._handle = handle
        self._lease = lease
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    @classmethod
    def admit(cls, path: str, verify: Callable[[str], None], *,
              max_bytes: int) -> 'AdmittedEngine':
        """Stage ``path`` once, let ``verify`` judge its digest, then publish it."""
        root = private_root()
        with open(path, 'rb') as source:
            before = os.fstat(source.fileno())
            staged, sha256 = _stage(source, root, max_bytes)
            try:
                after = os.fstat(source.fileno())
                if (_identity(before) != _identity(after)
                        or _identity(after) != _identity(os.stat(path))):
                    raise RuntimeError(f'storage engine {path} changed during admission')
                verify(sha256)
                directory = root / sha256
                lease = _lease(directory)
                try:
                    copy = directory / Path(path).name
                    handle = cls._open_verified(copy, sha256, max_bytes)
                    if handle is None:
                        os.replace(staged, copy)
                        handle = open(copy, 'rb')
                except BaseException:
                    if lease is not None:
                        os.close(lease)
                    raise
            finally:
                Path(staged).unlink(missing_ok=True)
        _collect(root, keep=sha256)
        return cls(path, sha256, copy, handle, lease, max_bytes)

    @staticmethod
    def _open_verified(copy: Path, sha256: str, max_bytes: int) -> BinaryIO | None:
        """Open an already published copy when it holds exactly ``sha256``."""
        try:
            handle = open(copy, 'rb')
        except FileNotFoundError:
            return None
        if _digest(handle, max_bytes) == sha256:
            return handle
        handle.close()
        return None

    def launch_path(self) -> str:
        """An executable path holding exactly the admitted bytes, verified now."""
        with self._lock:
            if not _lease_is_current(self._copy.parent, self._lease):
                # A cleaner removed the copy's directory under its lease.
                if self._lease is not None:
                    os.close(self._lease)
                self._lease = _lease(self._copy.parent)
            try:
                published = os.stat(self._copy)
            except FileNotFoundError:
                published = None
            if published is None or not _same_file(published, os.fstat(self._handle.fileno())):
                adopted = self._open_verified(self._copy, self.sha256, self._max_bytes)
                if adopted is None:
                    self._republish()
                else:
                    self._handle.close()
                    self._handle = adopted
            if _digest(self._handle, self._max_bytes) != self.sha256:
                raise AdmittedEngineLost(
                    f'the admitted storage engine copy {self._copy} no longer holds '
                    f'sha256 {self.sha256}')
            return str(self._copy)

    def _republish(self) -> None:
        """Rebuild the copy from the held descriptor after it was removed."""
        root = self._copy.parent.parent
        ensure_private_directory(root)
        self._handle.seek(0)
        staged, sha256 = _stage(self._handle, root, self._max_bytes)
        try:
            if sha256 != self.sha256:
                raise AdmittedEngineLost(
                    f'the admitted storage engine bytes changed in place (sha256 '
                    f'{sha256}, admitted {self.sha256})')
            os.replace(staged, self._copy)
        finally:
            Path(staged).unlink(missing_ok=True)
        self._handle.close()
        self._handle = open(self._copy, 'rb')

    def close(self) -> None:
        with self._lock:
            self._handle.close()
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None
