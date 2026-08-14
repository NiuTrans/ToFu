"""Project-filesystem durability and ownership preflight."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import time
import uuid

from lib.storage.errors import StorageError
from lib.log import get_logger


logger = get_logger('tofu.storage.sidecar.preflight')


@dataclass(slots=True)
class PreflightReport:
    filesystem: str
    free_bytes: int
    fsync_ms: float
    atomic_replace: bool
    file_lock: bool

    def as_dict(self) -> dict[str, object]:
        return {
            'filesystem': self.filesystem,
            'free_bytes': self.free_bytes,
            'fsync_ms': round(self.fsync_ms, 3),
            'atomic_replace': self.atomic_replace,
            'file_lock': self.file_lock,
        }


class ProjectLease:
    """Process-held lock plus an auditable project-local ownership stamp."""

    def __init__(self, data_dir: Path) -> None:
        self._lock_path = data_dir / '.storage-sidecar.lock'
        self._lease_path = data_dir / '.storage-sidecar-lease.json'
        self._handle = None
        self._stamp: dict[str, object] | None = None

    def _write_stamp(self, stamp: dict[str, object]) -> None:
        replacement = self._lease_path.with_suffix('.new')
        with replacement.open('w', encoding='utf-8') as stream:
            json.dump(stamp, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(replacement, self._lease_path)

    def acquire(self) -> None:
        self._lock_path.touch(mode=0o600, exist_ok=True)
        handle = self._lock_path.open('r+b')
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise StorageError(
                'database_unavailable',
                'Another storage sidecar holds the project lease',
                retryable=False,
            ) from exc
        self._handle = handle
        stamp = {
            'host': socket.gethostname(),
            'pid': os.getpid(),
            'started_unix_ms': int(time.time() * 1000),
            'lease_id': uuid.uuid4().hex,
            'status': 'running',
        }
        self._write_stamp(stamp)
        self._stamp = stamp

    def release(self) -> None:
        handle, self._handle = self._handle, None
        stamp, self._stamp = self._stamp, None
        if handle is None:
            return
        try:
            if stamp is not None:
                released = {
                    **stamp,
                    'status': 'stopped',
                    'stopped_unix_ms': int(time.time() * 1000),
                }
                try:
                    self._write_stamp(released)
                except OSError as exc:
                    # The OS lock remains authoritative.  A failed audit-stamp
                    # update must not prevent it from being released.
                    logger.warning('Storage lease stop stamp failed: %s', exc)
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def run_filesystem_preflight(data_dir: Path) -> PreflightReport:
    data_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(data_dir).free
    minimum = int(os.environ.get('TOFU_STORAGE_MIN_FREE_BYTES') or 256 * 1024 * 1024)
    if free_bytes < minimum:
        raise StorageError(
            'database_unavailable', 'Insufficient project storage space')
    stem = f'.storage-preflight-{uuid.uuid4().hex}'
    source = data_dir / f'{stem}.new'
    target = data_dir / f'{stem}.ready'
    lock_path = data_dir / f'{stem}.lock'
    started = time.monotonic()
    locked = False
    try:
        payload = os.urandom(4096)
        with source.open('xb', buffering=0) as stream:
            written = stream.write(payload)
            if written != len(payload):
                raise OSError('short write')
            os.fsync(stream.fileno())
        os.replace(source, target)
        if target.read_bytes() != payload:
            raise OSError('atomic replacement content mismatch')
        if hasattr(os, 'O_DIRECTORY'):
            directory_fd = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        lock_path.touch()
        with lock_path.open('r+b') as lock:
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                with lock_path.open('r+b') as contender:
                    try:
                        msvcrt.locking(contender.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        locked = True
                        logger.debug('Windows lock contender correctly excluded')
                    else:
                        msvcrt.locking(contender.fileno(), msvcrt.LK_UNLCK, 1)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with lock_path.open('r+b') as contender:
                    try:
                        fcntl.flock(
                            contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except (OSError, BlockingIOError):
                        locked = True
                        logger.debug('POSIX lock contender correctly excluded')
                    else:
                        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        if not locked:
            raise OSError('filesystem lock exclusion is not enforced')
    except OSError as exc:
        raise StorageError(
            'database_unavailable',
            'Project filesystem failed storage durability preflight',
        ) from exc
    finally:
        for path in (source, target, lock_path):
            path.unlink(missing_ok=True)
    fsync_ms = (time.monotonic() - started) * 1000
    maximum_ms = float(os.environ.get('TOFU_STORAGE_PREFLIGHT_MAX_MS') or 5000)
    if fsync_ms > maximum_ms:
        raise StorageError(
            'database_unavailable',
            'Project filesystem latency exceeds the configured safety bound',
        )
    return PreflightReport(
        filesystem=str(data_dir), free_bytes=free_bytes, fsync_ms=fsync_ms,
        atomic_replace=True, file_lock=locked,
    )


__all__ = ['PreflightReport', 'ProjectLease', 'run_filesystem_preflight']
