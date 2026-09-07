"""Bounded host-local bytecode cache for managed server workers.

Responsibility: select and maintain one reconstructible ``PYTHONPYCACHEPREFIX``
when the application checkout is on a network/userspace filesystem and the
manager can prove that the cache is local.  The cache is isolated by project
and interpreter identity, bounded at every launch, and never owns source or
durable application data.

Entry point: :func:`prepare_server_python_cache`.  This module intentionally
uses only the Python standard library so lifecycle recovery remains available
when application dependencies are broken.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Mapping


_MIB = 1024 * 1024
_DEFAULT_MAX_MIB = 64
_HARD_MAX_MIB = 512
_MAX_FILES = 100_000
_MAX_NAMESPACES = 64
_TTL_SECONDS = 7 * 24 * 60 * 60
_FREE_SPACE_RESERVE_BYTES = 256 * _MIB
_MAX_ROOT_ENTRIES_SCANNED = 128
_NAMESPACE_NAME = re.compile(r'^ns-[0-9a-f]{16}-[0-9a-f]{16}$')
_OFF_VALUES = frozenset({'0', 'false', 'no', 'off', 'disabled'})
_ON_VALUES = frozenset({'1', 'true', 'yes', 'on', 'enabled', 'force'})
_NETWORK_FILESYSTEMS = frozenset({
    '9p', 'afs', 'beegfs', 'bgfuse', 'ceph', 'cifs', 'davfs', 'davfs2',
    'gfs2', 'glusterfs', 'gpfs', 'lustre', 'ncpfs', 'nfs', 'nfs4', 'ocfs2',
    'smb', 'smb2', 'smb3', 'sshfs',
})
_LOCAL_BLOCK_FILESYSTEMS = frozenset({
    'apfs', 'bcachefs', 'btrfs', 'exfat', 'ext2', 'ext3', 'ext4', 'f2fs',
    'hfs', 'hfsplus', 'jfs', 'nilfs2', 'ntfs', 'ntfs3', 'reiserfs', 'ufs',
    'vfat', 'xfs', 'zfs',
})
_MEMORY_FILESYSTEMS = frozenset({'ramfs', 'tmpfs'})
_OVERLAY_FILESYSTEMS = frozenset({'aufs', 'overlay', 'overlayfs'})
_REMOTE_SOURCE_CLASSES = frozenset({
    'network-filesystem', 'userspace-filesystem',
})
_LOCAL_CACHE_CLASSES = frozenset({
    'local-block', 'container-overlay', 'memory-filesystem',
})


@dataclass(frozen=True, slots=True)
class _MountDescription:
    filesystem_type: str = 'unknown'
    mount_point: str = ''
    storage_class: str = 'unknown'


@dataclass(frozen=True, slots=True)
class _NamespaceUsage:
    path: Path
    last_used: float
    total_bytes: int
    file_count: int
    scan_exhausted: bool


@dataclass(frozen=True, slots=True)
class ServerPythonCacheActivation:
    """One cache decision plus an optional child-owned namespace lease."""

    selected: bool
    managed: bool
    reason: str
    mode: str
    max_bytes: int
    source_filesystem: str = 'unknown'
    cache_filesystem: str = 'unknown'
    namespace: str = ''
    pycache_prefix: str = ''
    cache_root: str = ''
    lock_fd: int | None = None

    def as_status(self) -> dict[str, object]:
        return {
            'selected': self.selected,
            'managed': self.managed,
            'reason': self.reason,
            'mode': self.mode,
            'maxMiB': self.max_bytes // _MIB,
            'sourceFilesystem': self.source_filesystem,
            'cacheFilesystem': self.cache_filesystem,
            'namespace': self.namespace,
            'cacheRoot': self.cache_root,
        }

    def close_parent_lock(self) -> None:
        if self.lock_fd is None:
            return
        try:
            os.close(self.lock_fd)
        except OSError:
            pass


def _process_user_id() -> int:
    getuid = getattr(os, 'getuid', None)
    return int(getuid()) if getuid is not None else 0


def _mode(environment: Mapping[str, str]) -> str:
    value = str(environment.get('TOFU_SERVER_PYTHON_CACHE', 'auto')).strip().lower()
    if value in _OFF_VALUES:
        return 'off'
    if value in _ON_VALUES:
        return 'on'
    return 'auto'


def _max_bytes(environment: Mapping[str, str]) -> int:
    raw = str(environment.get(
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB', _DEFAULT_MAX_MIB)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = _DEFAULT_MAX_MIB
    return max(8, min(_HARD_MAX_MIB, value)) * _MIB


def _decode_mount_path(value: str) -> str:
    for encoded, decoded in (
        ('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\'),
    ):
        value = value.replace(encoded, decoded)
    return value


def _classify_filesystem(filesystem_type: str) -> str:
    normalized = filesystem_type.lower()
    if normalized in _NETWORK_FILESYSTEMS or normalized.startswith((
        'fuse.beegfs', 'fuse.bgfuse', 'fuse.ceph', 'fuse.glusterfs',
        'fuse.sshfs',
    )):
        return 'network-filesystem'
    if normalized in _MEMORY_FILESYSTEMS:
        return 'memory-filesystem'
    if normalized in _OVERLAY_FILESYSTEMS:
        return 'container-overlay'
    if normalized in _LOCAL_BLOCK_FILESYSTEMS:
        return 'local-block'
    if normalized.startswith('fuse'):
        return 'userspace-filesystem'
    return 'unknown'


def _read_mountinfo() -> str:
    try:
        return Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def _describe_mount(
    path: str | Path,
    *,
    mountinfo_text: str | None = None,
) -> _MountDescription:
    try:
        resolved = str(Path(path).resolve(strict=False))
    except OSError:
        resolved = os.path.abspath(os.fspath(path))
    text = _read_mountinfo() if mountinfo_text is None else mountinfo_text
    best_mount = ''
    best_filesystem = ''
    for line in text.splitlines():
        before, separator, after = line.partition(' - ')
        if not separator:
            continue
        mount_fields = before.split()
        filesystem_fields = after.split()
        if len(mount_fields) < 5 or not filesystem_fields:
            continue
        mount_point = _decode_mount_path(mount_fields[4])
        normalized_mount = mount_point.rstrip(os.sep) or os.sep
        if not (
            resolved == normalized_mount
            or resolved.startswith(normalized_mount.rstrip(os.sep) + os.sep)
        ):
            continue
        if len(normalized_mount) >= len(best_mount):
            best_mount = normalized_mount
            best_filesystem = filesystem_fields[0]
    return _MountDescription(
        filesystem_type=best_filesystem or 'unknown',
        mount_point=best_mount,
        storage_class=_classify_filesystem(best_filesystem),
    )


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _resolve_cache_root(
    project_path: str,
    environment: Mapping[str, str],
) -> Path | None:
    configured = str(environment.get(
        'TOFU_SERVER_PYTHON_CACHE_DIR', '')).strip()
    default = Path(tempfile.gettempdir()) / (
        f'tofu-server-pycache-{_process_user_id()}')
    candidate = Path(configured) if configured else default
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=False)
        broad_roots = {
            Path(resolved.anchor),
            Path(tempfile.gettempdir()).resolve(strict=False),
            Path.home().resolve(strict=False),
            Path(project_path).resolve(strict=False),
        }
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved in broad_roots or resolved.parent == resolved:
        return None
    return resolved


def _ensure_private_directory(path: Path) -> None:
    existed = path.exists() or path.is_symlink()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _process_user_id()
        or (existed and stat.S_IMODE(metadata.st_mode) & 0o077)
    ):
        raise ValueError('server bytecode cache directory is not privately owned')
    if not existed:
        os.chmod(path, 0o700)


def _is_private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == _process_user_id()
        and not stat.S_IMODE(metadata.st_mode) & 0o077
    )


def _open_lock(path: Path, *, exclusive: bool) -> int | None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - managed server is Unix-only today
        return None
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _process_user_id()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise OSError('unsafe bytecode cache lock')
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        return descriptor
    except (OSError, BlockingIOError):
        if descriptor is not None:
            os.close(descriptor)
        return None


def _close_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _touch_private_marker(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != _process_user_id()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise OSError('unsafe bytecode cache marker')
        os.utime(descriptor, None)
    finally:
        os.close(descriptor)


def _short_hash(value: str) -> str:
    return hashlib.sha256(
        value.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]


def _namespace_name(project_path: str, python_executable: str) -> str | None:
    try:
        project = os.path.realpath(project_path)
        interpreter = os.path.realpath(python_executable)
        project_stat = os.stat(project)
        interpreter_stat = os.stat(interpreter)
    except OSError:
        return None
    project_key = _short_hash(
        f'{project}\0{project_stat.st_dev}\0{project_stat.st_ino}')
    interpreter_key = _short_hash('\0'.join((
        interpreter,
        str(interpreter_stat.st_dev),
        str(interpreter_stat.st_ino),
        str(interpreter_stat.st_size),
        str(interpreter_stat.st_mtime_ns),
        getattr(sys.implementation, 'cache_tag', '') or '',
    )))
    return f'ns-{project_key}-{interpreter_key}'


def _namespace_usage(
    namespace: Path,
    *,
    remaining_files: int,
    remaining_bytes: int,
) -> _NamespaceUsage:
    total_bytes = 0
    file_count = 0
    exhausted = False
    marker = namespace / '.last-used'
    try:
        marker_metadata = marker.lstat()
        last_used = (
            marker_metadata.st_mtime
            if stat.S_ISREG(marker_metadata.st_mode)
            and not stat.S_ISLNK(marker_metadata.st_mode)
            else 0.0
        )
    except OSError:
        try:
            last_used = namespace.stat().st_mtime
        except OSError:
            last_used = 0.0
    for directory, directory_names, file_names in os.walk(
        namespace, topdown=True, followlinks=False,
    ):
        directory_path = Path(directory)
        safe_directories: list[str] = []
        for name in directory_names:
            try:
                metadata = (directory_path / name).lstat()
            except OSError:
                continue
            file_count += 1
            total_bytes += max(0, metadata.st_size)
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                safe_directories.append(name)
            if file_count > remaining_files or total_bytes > remaining_bytes:
                exhausted = True
                safe_directories = []
                break
        directory_names[:] = safe_directories
        if exhausted:
            break
        for name in file_names:
            try:
                metadata = (directory_path / name).lstat()
            except OSError:
                continue
            file_count += 1
            total_bytes += max(0, metadata.st_size)
            if file_count > remaining_files or total_bytes > remaining_bytes:
                exhausted = True
                directory_names[:] = []
                break
        if exhausted:
            break
    return _NamespaceUsage(
        path=namespace,
        last_used=last_used,
        total_bytes=total_bytes,
        file_count=file_count,
        scan_exhausted=exhausted,
    )


def _scan_namespaces(
    root: Path,
    *,
    max_bytes: int,
) -> tuple[list[_NamespaceUsage], bool]:
    records: list[_NamespaceUsage] = []
    total_bytes = 0
    total_files = 0
    try:
        iterator = os.scandir(root)
    except OSError:
        return records, True
    exhausted = False
    with iterator:
        for entry_index, entry in enumerate(iterator):
            if entry_index >= _MAX_ROOT_ENTRIES_SCANNED:
                exhausted = True
                break
            if not _NAMESPACE_NAME.fullmatch(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            usage = _namespace_usage(
                Path(entry.path),
                remaining_files=max(0, _MAX_FILES - total_files),
                remaining_bytes=max(0, max_bytes - total_bytes),
            )
            records.append(usage)
            total_files += usage.file_count
            total_bytes += usage.total_bytes
    return records, exhausted


def _safe_remove_namespace(root: Path, namespace: Path) -> bool:
    descriptor = None
    try:
        if namespace.parent != root or not _NAMESPACE_NAME.fullmatch(namespace.name):
            return False
        metadata = namespace.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return False
        descriptor = _open_lock(namespace / '.active.lock', exclusive=True)
        if descriptor is None:
            return False
        shutil.rmtree(namespace)
        return True
    except OSError:
        return False
    finally:
        _close_lock(descriptor)


def _prune_cache(
    root: Path,
    *,
    max_bytes: int,
    required_headroom: int,
    reserve_namespace_slot: bool,
) -> bool:
    records, root_scan_exhausted = _scan_namespaces(
        root, max_bytes=max_bytes)
    now = time.time()
    retained: list[_NamespaceUsage] = []
    for record in records:
        if now - record.last_used > _TTL_SECONDS:
            if _safe_remove_namespace(root, record.path):
                continue
        retained.append(record)
    retained.sort(key=lambda item: item.last_used)
    total_bytes = sum(item.total_bytes for item in retained)
    total_files = sum(item.file_count for item in retained)
    target_bytes = max(0, max_bytes - required_headroom)
    namespace_limit = max(
        0, _MAX_NAMESPACES - (1 if reserve_namespace_slot else 0))
    while retained and (
        len(retained) > namespace_limit
        or total_bytes > target_bytes
        or total_files > _MAX_FILES
        or any(item.scan_exhausted for item in retained)
    ):
        removed = False
        for index, victim in enumerate(retained):
            if not _safe_remove_namespace(root, victim.path):
                continue
            retained.pop(index)
            total_bytes -= victim.total_bytes
            total_files -= victim.file_count
            removed = True
            break
        if not removed:
            return False
    return (
        not root_scan_exhausted
        and len(retained) <= namespace_limit
        and total_bytes <= target_bytes
        and total_files <= _MAX_FILES
        and not any(item.scan_exhausted for item in retained)
    )


def _disabled(
    reason: str,
    *,
    mode: str,
    max_bytes: int,
    source: _MountDescription = _MountDescription(),
    cache: _MountDescription = _MountDescription(),
    selected: bool = False,
) -> ServerPythonCacheActivation:
    return ServerPythonCacheActivation(
        selected=selected,
        managed=False,
        reason=reason,
        mode=mode,
        max_bytes=max_bytes,
        source_filesystem=source.filesystem_type,
        cache_filesystem=cache.filesystem_type,
    )


def prepare_server_python_cache(
    project_path: str,
    python_executable: str,
    environment: Mapping[str, str],
    *,
    mountinfo_text: str | None = None,
) -> ServerPythonCacheActivation:
    """Choose a safe local prefix without changing the supplied environment."""
    mode = _mode(environment)
    max_bytes = _max_bytes(environment)
    dont_write = str(environment.get(
        'PYTHONDONTWRITEBYTECODE', '')).strip()
    if dont_write:
        return _disabled(
            'python-bytecode-writes-disabled', mode=mode, max_bytes=max_bytes)
    if str(environment.get('PYTHONPYCACHEPREFIX', '')).strip():
        return _disabled(
            'operator-python-prefix', mode=mode, max_bytes=max_bytes,
            selected=True)
    if mode == 'off':
        return _disabled('disabled', mode=mode, max_bytes=max_bytes)

    cache_root = _resolve_cache_root(project_path, environment)
    if cache_root is None:
        return _disabled('unsafe-cache-root', mode=mode, max_bytes=max_bytes)
    source_mount = _describe_mount(
        project_path, mountinfo_text=mountinfo_text)
    cache_mount = _describe_mount(
        _nearest_existing_path(cache_root), mountinfo_text=mountinfo_text)
    if cache_mount.storage_class not in _LOCAL_CACHE_CLASSES:
        return _disabled(
            'cache-not-local', mode=mode, max_bytes=max_bytes,
            source=source_mount, cache=cache_mount)
    if (
        mode == 'auto'
        and source_mount.storage_class not in _REMOTE_SOURCE_CLASSES
    ):
        return _disabled(
            'source-not-remote', mode=mode, max_bytes=max_bytes,
            source=source_mount, cache=cache_mount)

    namespace_name = _namespace_name(project_path, python_executable)
    if namespace_name is None:
        return _disabled(
            'identity-unavailable', mode=mode, max_bytes=max_bytes,
            source=source_mount, cache=cache_mount)
    namespace = cache_root / namespace_name
    maintenance_lock = None
    namespace_lock = None
    try:
        _ensure_private_directory(cache_root)
        maintenance_lock = _open_lock(
            cache_root / '.maintenance.lock', exclusive=True)
        if maintenance_lock is None:
            return _disabled(
                'maintenance-busy', mode=mode, max_bytes=max_bytes,
                source=source_mount, cache=cache_mount)
        available = shutil.disk_usage(cache_root).free
        headroom = min(16 * _MIB, max(2 * _MIB, max_bytes // 4))
        if available < _FREE_SPACE_RESERVE_BYTES + headroom:
            return _disabled(
                'insufficient-cache-space', mode=mode, max_bytes=max_bytes,
                source=source_mount, cache=cache_mount)
        if not _prune_cache(
            cache_root,
            max_bytes=max_bytes,
            required_headroom=headroom,
            reserve_namespace_slot=not namespace.exists(),
        ):
            return _disabled(
                'cache-budget-unavailable', mode=mode, max_bytes=max_bytes,
                source=source_mount, cache=cache_mount)
        _ensure_private_directory(namespace)
        namespace_lock = _open_lock(
            namespace / '.active.lock', exclusive=False)
        if namespace_lock is None:
            return _disabled(
                'namespace-busy', mode=mode, max_bytes=max_bytes,
                source=source_mount, cache=cache_mount)
        prefix = namespace / 'pycache'
        _ensure_private_directory(prefix)
        _touch_private_marker(namespace / '.last-used')
    except (OSError, RuntimeError, ValueError):
        _close_lock(namespace_lock)
        return _disabled(
            'cache-setup-failed', mode=mode, max_bytes=max_bytes,
            source=source_mount, cache=cache_mount)
    finally:
        _close_lock(maintenance_lock)

    return ServerPythonCacheActivation(
        selected=True,
        managed=True,
        reason='remote-source-local-cache' if mode == 'auto' else 'forced',
        mode=mode,
        max_bytes=max_bytes,
        source_filesystem=source_mount.filesystem_type,
        cache_filesystem=cache_mount.filesystem_type,
        namespace=namespace_name,
        pycache_prefix=str(prefix),
        cache_root=str(cache_root),
        lock_fd=namespace_lock,
    )


def reacquire_server_python_cache_lease(
    project_path: str,
    python_executable: str,
    cache_root: str,
    namespace: str,
    *,
    mountinfo_text: str | None = None,
) -> int | None:
    """Reacquire one exact live-worker lease after a manager replacement."""
    expected_namespace = _namespace_name(project_path, python_executable)
    if expected_namespace is None or namespace != expected_namespace:
        return None
    try:
        root = Path(cache_root)
        if not root.is_absolute() or root.is_symlink():
            return None
        root = root.resolve(strict=True)
        candidate = root / namespace
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        root.parent == root
        or not _is_private_directory(root)
        or not _is_private_directory(candidate)
        or _describe_mount(
            root, mountinfo_text=mountinfo_text,
        ).storage_class not in _LOCAL_CACHE_CLASSES
    ):
        return None
    return _open_lock(candidate / '.active.lock', exclusive=False)
